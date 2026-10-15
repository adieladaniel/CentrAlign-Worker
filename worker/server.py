"""Worker API + dashboard server (FastAPI, port 8000).

Run it with ``python -m worker.server``. Runs, events and reports are read from a ``RunStore``; a run's events are stored
by the runner and fanned out to WebSocket subscribers (``/api/runs/<id>/events``), which first receive a replay of
everything that happened so far. Approvals and questions raised by the agent are answered through
``POST /api/runs/<id>/respond``.

Two modes, chosen by ``Settings.db_auth`` (``AUTH_MODE``; ``auto`` = database mode when ``DATABASE_URL`` is set):

* **file mode** (v1): one shared password (``ACCESS_PASSWORD``) or no auth, runs in files, one run at a time.
* **database mode**: users, orgs and roles in PostgreSQL (``worker/auth.py``). Every endpoint is scoped to the caller's
  active organisation; another org's runs are indistinguishable from unknown ones (404). One run per org at a time;
  runs still execute in this process (``launch_run`` is the single place that creates one; the job queue replaces it).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.admin_api import build_admin_router
from worker.agent.broker import InteractionBroker
from worker.agent.runner import AgentRunner
from worker.agent.state import utcnow
from worker.agent.toolset import build_registry
from worker.auth import (
    SESSION_COOKIE, CsrfGuard, AuthService, OrgContext, SessionGate, build_auth_router, require_role, websocket_context,
)
from worker.config import Settings, get_settings
from worker.db import migrate
from worker.db.models import Org
from worker.db.session import dispose_engine, get_sessionmaker
from worker.deploy import (
    PROXY_PREFIXES, AccessControl, DailyRunCap, install_access_control, install_sim_proxy,
)
from worker.llm.base import LLMClient, LLMError
from worker.llm.factory import make_llm
from worker.llm.failover import DEFAULT_HEALTH, active_model
from worker.org_settings import approval_block, can_approve, effective, stricter_mode
from worker.provisioning import ProvisioningError, load_org_environment, org_chaos
from worker.store import RUN_ID_RE, FileRunStore, RunStore
from worker.store_pg import PgRunStore
from worker.tools.registry import ToolRegistry
from worker.vault import Vault, VaultError, default_scope
from worker.vault_pg import open_pg_vault

log = logging.getLogger("worker.server")

UI_DIR = Path(__file__).parent / "ui"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = PROJECT_ROOT / "evals" / "scenarios"
SIM_TIMEOUT_S = 5.0

# Used when evals/scenarios/*.yaml is missing. ``chaos`` lists the sim flags the preset is meant to demonstrate.
BUILTIN_SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "invoice_entry",
        "title": "Latest invoice to ERP",
        "task": "Find the latest invoice from Company X, extract the amount and due date, enter it into our internal "
                "system, and tell me once it is done.",
        "chaos": [],
    },
    {
        "id": "invoice_entry_chaos",
        "title": "Invoice to ERP (server error + session expiry)",
        "task": "Find the latest invoice from Company X, extract the amount and due date, enter it into our internal "
                "system, and tell me once it is done.",
        "chaos": ["erp_submit_500_once", "erp_session_expiry"],
    },
    {
        "id": "invoice_duplicate",
        "title": "Invoice that is already in the ERP",
        "task": "Find the latest invoice from Company X, extract the amount and due date, enter it into our internal "
                "system, and tell me once it is done.",
        "chaos": ["duplicate_bill"],
    },
    {
        "id": "vendor_email_reply",
        "title": "Reply to a vendor payment question",
        "task": "Initech emailed asking when invoice INI-20502 will be paid. Check our records and reply to them with "
                "the expected payment date (the invoice due date).",
        "chaos": [],
    },
    {
        "id": "month_due_bills",
        "title": "Record every invoice due this month",
        "task": "Make sure every open vendor invoice due this month is recorded in our accounting system.",
        "chaos": [],
    },
]


# --------------------------------------------------------------------------- request models


class RunRequest(BaseModel):
    task: str = Field(min_length=1)
    approval_mode: Literal["auto", "confirm_writes", "confirm_all"] | None = None
    headed: bool = False


class RespondRequest(BaseModel):
    request_id: str
    approved: bool | None = None
    note: str | None = None
    answer: str | None = None
    remember: bool | None = None


class ResetRequest(BaseModel):
    chaos: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- run sessions


@dataclass
class RunSession:
    """One agent run and everything the dashboard needs to follow it."""

    run_id: str
    task: str
    runner: AgentRunner
    broker: InteractionBroker
    store: RunStore
    org_id: str | None = None  # database mode: the org that owns the run (other orgs cannot see it)
    user_id: str | None = None  # database mode: who started it (separation of duties)
    started_by: dict | None = None  # database mode: {id, name, email, role} of the starter, for the UI
    last_seq: int = 0  # the newest stored event already fanned out to subscribers
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task_handle: asyncio.Task | None = None
    finished: bool = False

    @property
    def active(self) -> bool:
        return self.task_handle is not None and not self.task_handle.done()

    async def publish(self) -> None:
        """Push the events the runner has stored since the last call to every subscriber."""
        if not self.subscribers:
            return  # late subscribers replay from the store and skip what they already have by ``seq``
        for event in await self.store.read_events(self.run_id, after_seq=self.last_seq):
            self.last_seq = event["seq"]
            for queue in list(self.subscribers):
                queue.put_nowait(event)

    def close_subscribers(self) -> None:
        for queue in list(self.subscribers):
            queue.put_nowait(None)


# --------------------------------------------------------------------------- helpers


def _valid_run_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail="Invalid run id")
    return run_id


def load_scenarios(directory: Path = SCENARIOS_DIR) -> list[dict[str, Any]]:
    """Preset tasks from ``evals/scenarios/*.yaml`` (fields: id, title, task, chaos), else the built-in list."""
    found: list[dict[str, Any]] = []
    if directory.is_dir():
        try:
            import yaml
        except ImportError:  # pragma: no cover - pyyaml is a project dependency
            yaml = None  # type: ignore[assignment]
        for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
            if yaml is None:
                break
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, yaml.YAMLError):
                continue
            if not isinstance(data, dict) or not str(data.get("task") or "").strip():
                continue
            chaos = data.get("chaos") or []
            if isinstance(chaos, dict):
                chaos = [name for name, on in chaos.items() if on]
            found.append({
                "id": str(data.get("id") or path.stem),
                "title": str(data.get("title") or data.get("name") or path.stem.replace("_", " ")),
                "task": str(data["task"]).strip(),
                "chaos": [str(c) for c in chaos] if isinstance(chaos, list) else [],
            })
    return found or [dict(s) for s in BUILTIN_SCENARIOS]


def synthesize_events(state: dict) -> list[dict]:
    """Rebuild an event log from a stored run state (for runs that have no event log, e.g. stored before event logging)."""
    events: list[dict] = []
    run_id = state.get("run_id", "")

    def add(event_type: str, payload: dict, ts: str = "") -> None:
        events.append({"seq": len(events) + 1, "type": event_type, "ts": ts or state.get("started_at", ""),
                       "payload": {"run_id": run_id, **payload}})

    add("run_started", {"task": state.get("task", ""), "approval_mode": "unknown"})
    if state.get("plan"):
        add("plan_updated", {"plan": state["plan"], "goal_summary": state.get("goal_summary", ""),
                             "success_criteria": state.get("success_criteria", [])})
    for fact in (state.get("facts") or {}).values():
        add("memory_updated", {"key": fact.get("key"), "value": fact.get("value"), "source": fact.get("source"),
                               "step": fact.get("step", 0)})
    report = state.get("final_report") or {}
    verification = report.get("verification") or {}
    verifying = False
    for step in state.get("steps") or []:
        kind, ts = step.get("kind"), step.get("timestamp", "")
        if kind == "verify" and not verifying:
            verifying = True
            add("verification_started", {"criteria": state.get("success_criteria", [])}, ts)
        if kind != "verify" and verifying:
            verifying = False
            add("verification_finished", {"passed": verification.get("passed", False),
                                          "inconclusive": verification.get("inconclusive", False),
                                          "checks": verification.get("checks", []),
                                          "summary": verification.get("summary", "")}, ts)
        if kind in ("tool", "ask_user", "verify"):
            add("tool_finished", {**step, "phase": "verify" if kind == "verify" else "work"}, ts)
        elif kind == "approval":
            action = str(step.get("observation", "")).partition(": ")[2] or step.get("tool", "")
            add("approval_requested", {"request_id": f"hist-{step.get('index')}", "tool": step.get("tool"), "action": action,
                                       "args": step.get("args", {}), "risk": step.get("risk", "write"), "context": {}}, ts)
            add("approval_resolved", {"response": {"approved": bool(step.get("ok")), "note": step.get("thought", "")},
                                      "answered": True}, ts)
        elif kind == "system" and step.get("error") == "resumed":
            add("run_resumed", {"message": step.get("thought", "")}, ts)
        elif kind == "system":
            add("guard_triggered", {"guard": step.get("error", ""), "level": "nudge", "message": step.get("thought", "")}, ts)
    if verifying or (verification.get("ran") and not any(e["type"] == "verification_finished" for e in events)):
        if not verifying:
            add("verification_started", {"criteria": state.get("success_criteria", [])})
        add("verification_finished", {"passed": verification.get("passed", False),
                                      "inconclusive": verification.get("inconclusive", False),
                                      "checks": verification.get("checks", []), "summary": verification.get("summary", "")})
    usage = state.get("usage") or {}
    if usage:
        add("usage", {**usage, "llm_retries": 0})
    if report:
        add("run_finished", {"status": state.get("status"), "report": report}, state.get("finished_at") or "")
    return events


# --------------------------------------------------------------------------- app factory


def create_app(
    settings: Settings | None = None,
    llm_factory: Callable[[Settings], LLMClient] | None = None,
    registry_factory: Callable[[Settings], ToolRegistry] | None = None,
    store: RunStore | None = None,
    vault: Vault | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    """Build the FastAPI app. ``llm_factory`` / ``registry_factory`` / ``store`` / ``vault`` / ``sessionmaker`` are seams for
    tests and deployments.

    File mode (``Settings.db_auth`` false): without ``vault``, the file vault in ``VAULT_DIR`` is used if it exists; runs get
    the credentials of the default owner. Database mode: ``sessionmaker`` defaults to the process-wide engine built from
    ``DATABASE_URL``; ``store`` (optional) must be a ``PgRunStore`` and is scoped per request to the caller's org;
    ``vault`` defaults to the PostgreSQL vault; each run gets the vault scope of its own org."""
    base_settings = settings or get_settings()
    db_mode = base_settings.AUTH_MODE != "file" and (base_settings.db_auth or sessionmaker is not None)
    owns_engine = db_mode and sessionmaker is None
    sm = (sessionmaker or get_sessionmaker(base_settings)) if db_mode else None
    if db_mode and store is not None and not isinstance(store, PgRunStore):
        raise ValueError("database mode needs a PgRunStore (or none)")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if sm is not None:
            await check_schema(sm)
        yield
        close = getattr(app.state, "close_sim_client", None)
        if close is not None:
            await close()
        if owns_engine:
            await dispose_engine()
        try:
            from worker.tools.browser_pool import shutdown_pool
        except ImportError:
            return
        try:
            await shutdown_pool()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.exception("shutting down the browser pool failed")

    app = FastAPI(title="CentrAlign Worker", version="2.0", lifespan=lifespan)
    app.state.llm_factory = llm_factory or make_llm
    app.state.registry_factory = registry_factory or build_registry
    app.state.settings = base_settings
    app.state.sim_http = None  # optional httpx client for the simulator's /admin API (tests route it in-process)
    sessions: dict[str, RunSession] = {}
    app.state.sessions = sessions

    service: AuthService | None = AuthService(sm, base_settings) if sm is not None else None
    app.state.auth = service
    pg_base: PgRunStore | None = None
    if sm is not None:
        # The base store is only a template: ``for_org`` hands every request a store scoped to the caller's org.
        pg_base = store or PgRunStore(sm, uuid.UUID(int=0), artifacts_root=base_settings.RUNS_DIR)
    run_store: RunStore = pg_base if pg_base is not None else (store or FileRunStore(base_settings.RUNS_DIR))
    app.state.store = run_store

    pg_vault_cache: list[Vault] = []

    def org_vault() -> Vault:
        """The PostgreSQL vault, opened on first use (so building the app creates no key file)."""
        if vault is not None:
            return vault
        if not pg_vault_cache:
            pg_vault_cache.append(open_pg_vault(sm, base_settings))
        return pg_vault_cache[0]

    access = AccessControl("" if db_mode else base_settings.ACCESS_PASSWORD, base_settings.SESSION_SECRET)
    run_cap = DailyRunCap(0 if db_mode else base_settings.MAX_RUNS_PER_DAY)
    app.state.access, app.state.run_cap = access, run_cap
    if db_mode and base_settings.ACCESS_PASSWORD:
        log.warning("ACCESS_PASSWORD is ignored in database mode: users sign in with their own accounts.")
    launch_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ----------------------------------------------------------------- scoping helpers

    def store_for(ctx: OrgContext | None) -> RunStore:
        """The run store a request reads and writes: the caller's org only (database mode), else the one store."""
        return run_store if ctx is None else pg_base.for_org(ctx.org_id, ctx.user_id)  # type: ignore[union-attr]

    def get_session(run_id: str, ctx: OrgContext | None) -> RunSession | None:
        """The in-memory run, unless it belongs to another org (then it looks like an unknown run)."""
        found = sessions.get(run_id)
        if found is not None and ctx is not None and found.org_id != str(ctx.org_id):
            return None
        return found

    def active_session(ctx: OrgContext | None) -> RunSession | None:
        return next((s for s in sessions.values() if s.active and (ctx is None or s.org_id == str(ctx.org_id))), None)

    def org_is_busy(org_id: uuid.UUID) -> bool:
        return any(s.active and s.org_id == str(org_id) for s in sessions.values())

    async def load_org(org_id: uuid.UUID) -> Org:
        async with sm() as session:  # type: ignore[misc]
            org = await session.get(Org, org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Unknown organization")
        return org

    async def known_run(ctx: OrgContext | None, run_id: str) -> bool:
        """Is this run visible to the caller? (In database mode: in the caller's org.)"""
        if ctx is None:
            return True
        return get_session(run_id, ctx) is not None or await store_for(ctx).exists(run_id)  # type: ignore[attr-defined]

    async def runs_left(ctx: OrgContext | None, org_cfg: dict | None = None) -> tuple[int | None, int | None]:
        """(runs allowed per UTC day, runs still available today); None = unlimited."""
        if ctx is None:
            return (run_cap.limit or None), run_cap.remaining()
        if org_cfg is None:
            org_cfg = effective((await load_org(ctx.org_id)).settings, base_settings)
        limit = org_cfg["max_runs_per_day"]
        if not limit:
            return None, None
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        used = await store_for(ctx).count_runs_since(today)  # type: ignore[attr-defined]
        return limit, max(0, limit - used)

    async def public_config(ctx: OrgContext | None) -> dict:
        cfg: Settings = app.state.settings
        limit, left = await runs_left(ctx)
        return {"deploy_mode": cfg.DEPLOY_MODE, "sim_proxy": cfg.PUBLIC_SIM_PROXY,
                "auth_enabled": db_mode or access.enabled, "max_runs_per_day": limit, "runs_left_today": left,
                **({"auth_mode": "db"} if db_mode else {})}

    async def starters(ctx: OrgContext | None, run_ids: list[str]) -> dict[str, dict]:
        return await store_for(ctx).creators(run_ids) if ctx is not None else {}  # type: ignore[attr-defined]

    # ----------------------------------------------------------------- runs

    async def launch_run(ctx: OrgContext | None, req: RunRequest) -> RunSession:
        """Create and start a run for ``ctx`` (None in file mode): quota and concurrency checks, the org's policy and
        environment, the runner and its supervising task. The single place a run comes into being; the job-queue
        milestone replaces its tail (``create_task``) with 'insert a queued run' and moves execution into a worker."""
        task_text = req.task.strip()
        if not task_text:
            raise HTTPException(status_code=422, detail="Task is empty")
        async with launch_locks[str(ctx.org_id) if ctx else "-"]:  # two simultaneous requests must not both pass the checks
            busy = active_session(ctx)
            if busy is not None:
                raise HTTPException(status_code=409, detail=f"Run {busy.run_id} is still active; cancel it or wait for it to finish.")
            overrides: dict[str, Any] = {}
            environment = None
            org_cfg: dict[str, Any] = {}
            if ctx is None:
                if not run_cap.allow():
                    raise HTTPException(
                        status_code=429, headers={"Retry-After": str(run_cap.seconds_until_reset())},
                        detail=f"Daily demo limit reached: this deployment allows {run_cap.limit} runs per day (UTC) to "
                               "protect the shared Gemini quota. Please try again tomorrow.")
                if req.approval_mode:
                    overrides["APPROVAL_MODE"] = req.approval_mode
            else:
                org = await load_org(ctx.org_id)
                org_cfg = effective(org.settings, base_settings)
                limit, left = await runs_left(ctx, org_cfg)
                if limit and not left:
                    raise HTTPException(
                        status_code=429, headers={"Retry-After": str(run_cap.seconds_until_reset())},
                        detail=f"Your organization reached its limit of {limit} runs per day (UTC). "
                               "An admin can raise it in the organization settings.")
                try:
                    environment = await load_org_environment(org, org_vault())
                except VaultError as err:
                    raise HTTPException(status_code=500, detail=f"The credential vault cannot be opened: {err}") from err
                if environment is None:
                    raise HTTPException(
                        status_code=409, detail="This organization has no environment yet. An admin must provision it "
                                                "(Admin > Environment) before runs can start.")
                overrides.update(
                    APPROVAL_MODE=stricter_mode(org_cfg["approval_mode"], req.approval_mode),
                    MAX_STEPS=org_cfg["max_steps"], RUN_TIMEOUT_S=org_cfg["run_timeout_s"],
                    SIM_ENTRY_URL="")  # the agent is told the plain simulator URL; the tenant cookie is pre-seeded
            overrides["HEADLESS"] = not (req.headed and not app.state.settings.DEPLOY_MODE)  # server mode is always headless
            run_settings = app.state.settings.model_copy(update=overrides)
            try:
                llm = app.state.llm_factory(run_settings)
                registry = app.state.registry_factory(run_settings)
            except LLMError as err:
                raise HTTPException(status_code=400, detail=str(err)) from err
            if environment is not None:
                vault_scope = environment.scope
            else:
                try:
                    vault_scope = await default_scope(run_settings, vault=vault)
                except VaultError as err:
                    raise HTTPException(status_code=500, detail=f"The credential vault cannot be opened: {err}") from err
            # No timeout in file mode: the dashboard user answers when ready. Database mode: the org's approval timeout.
            broker = InteractionBroker(default_timeout=org_cfg.get("approval_timeout_s"))
            session_holder: dict[str, RunSession] = {}

            async def sink(event_type: str, payload: dict) -> None:
                await session_holder["s"].publish()

            run_store_for = store_for(ctx)
            runner = AgentRunner(task_text, llm, registry, run_settings, broker, event_sink=sink, store=run_store_for,
                                 vault_scope=vault_scope)
            if environment is not None:  # pin the run's browser to the org's own simulated company
                runner.ctx.extras["browser_init_cookies"] = environment.browser_cookies(run_settings.SIM_BASE_URL)
            run_id = runner.state.run_id
            session = RunSession(run_id=run_id, task=task_text, runner=runner, broker=broker, store=run_store_for,
                                 org_id=str(ctx.org_id) if ctx else None, user_id=str(ctx.user_id) if ctx else None,
                                 started_by=ctx.actor() if ctx else None)
            session_holder["s"] = session
            sessions[run_id] = session
            run_cap.record()
            if service is not None and ctx is not None:
                await service.audit("run.start", ctx=ctx, target_type="run", target_id=run_id,
                                    details={"approval_mode": run_settings.APPROVAL_MODE})
            session.task_handle = asyncio.create_task(_supervise(session), name=f"run-{run_id}")
            return session

    @app.post("/api/runs")
    async def start_run(req: RunRequest, ctx: OrgContext | None = Depends(require_role("operator"))) -> dict:
        session = await launch_run(ctx, req)
        return {"run_id": session.run_id, "started_by": session.started_by}

    async def _supervise(session: RunSession) -> None:
        try:
            await session.runner.run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the runner already converts its own errors; this is a last resort
            log.exception("run %s crashed", session.run_id)
        finally:
            session.finished = True
            session.close_subscribers()

    @app.get("/api/runs")
    async def list_runs(ctx: OrgContext | None = Depends(require_role("viewer"))) -> list[dict]:
        rows = {row["id"]: row for row in await store_for(ctx).list_runs(200 if ctx else None)}
        mine = [s for s in sessions.values() if ctx is None or s.org_id == str(ctx.org_id)]
        for session in mine:  # runs that have not been stored yet
            rows.setdefault(session.run_id, {"id": session.run_id, "task": session.task, "status": "pending",
                                             "started_at": session.runner.state.started_at, "finished_at": None})
            if session.active:
                rows[session.run_id]["status"] = session.runner.state.status
        creators = await starters(ctx, list(rows))
        for run_id, row in rows.items():
            live = sessions.get(run_id)
            row["started_by"] = creators.get(run_id) or (live.started_by if live and ctx else None)
        return sorted(rows.values(), key=lambda r: r["started_at"], reverse=True)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, ctx: OrgContext | None = Depends(require_role("viewer"))) -> dict:
        _valid_run_id(run_id)
        session = get_session(run_id, ctx)
        if session is not None:
            data = session.runner.state.to_dict()
            data["pending"] = session.broker.pending()
            data["active"] = session.active
            data["started_by"] = session.started_by
        else:
            stored = await store_for(ctx).load_state(run_id)
            if stored is None:
                raise HTTPException(status_code=404, detail="Unknown run")
            data = stored.to_dict()
            data["pending"], data["active"] = [], False
            data["started_by"] = (await starters(ctx, [run_id])).get(run_id)
        data["report"] = data.get("final_report")
        has_report = await store_for(ctx).load_report_markdown(run_id) is not None
        data["report_url"] = f"/runs/{run_id}/files/report.md" if has_report else None
        return json.loads(json.dumps(data, default=str))

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, ctx: OrgContext | None = Depends(require_role("operator"))) -> dict:
        _valid_run_id(run_id)
        session = get_session(run_id, ctx)
        if session is None and not await known_run(ctx, run_id):
            raise HTTPException(status_code=404, detail="Unknown run")
        if session is None or not session.active:
            raise HTTPException(status_code=409, detail="This run is not active")
        session.runner.cancel()
        if service is not None and ctx is not None:
            await service.audit("run.cancel", ctx=ctx, target_type="run", target_id=run_id)
        return {"status": "cancelling"}

    @app.post("/api/runs/{run_id}/respond")
    async def respond(run_id: str, req: RespondRequest, ctx: OrgContext | None = Depends(require_role("viewer"))) -> dict:
        _valid_run_id(run_id)
        session = get_session(run_id, ctx)
        if session is None and not await known_run(ctx, run_id):
            raise HTTPException(status_code=404, detail="Unknown run")
        if session is None or not session.active:
            raise HTTPException(status_code=409, detail="This run is not active")
        pending = {p["request_id"]: p for p in session.broker.pending()}
        request = pending.get(req.request_id)
        if request is None:
            raise HTTPException(status_code=404, detail="No such pending request (already answered?)")
        if ctx is not None:
            org_cfg = effective((await load_org(ctx.org_id)).settings, base_settings)
            if request["kind"] == "approval":
                if not can_approve(ctx.role, org_cfg):
                    raise HTTPException(status_code=403, detail=(
                        f"Your role ({ctx.role}) cannot answer approvals." + (
                            " An admin can allow operators to approve in the organization settings."
                            if ctx.role == "operator" else "")))
                if req.approved:
                    blocked = approval_block(org_cfg, str(request["payload"].get("risk", "")), str(ctx.user_id),
                                             session.user_id)
                    if blocked:
                        raise HTTPException(status_code=403, detail=blocked)
            elif not ctx.has_role("approver"):  # a viewer may watch but not steer the run
                raise HTTPException(status_code=403, detail="Viewers cannot answer the agent's questions.")
        if request["kind"] == "approval":
            response: dict[str, Any] = {"approved": bool(req.approved), "note": req.note or "", "remember": bool(req.remember)}
        else:
            response = {"answer": (req.answer if req.answer is not None else req.note) or ""}
        if ctx is not None:
            response["by"] = ctx.actor()  # recorded in the approval_resolved event and the run's step
        if not session.broker.resolve(req.request_id, response):
            raise HTTPException(status_code=409, detail="Request was already resolved")
        if service is not None and ctx is not None:
            await service.audit("approval.respond" if request["kind"] == "approval" else "question.answer", ctx=ctx,
                                target_type="run", target_id=run_id,
                                details={"request_id": req.request_id, "approved": response.get("approved"),
                                         "risk": request["payload"].get("risk"), "tool": request["payload"].get("tool"),
                                         "run_started_by": session.user_id})
        return {"status": "ok", "kind": request["kind"]}

    # ----------------------------------------------------------------- live events

    async def stored_events(st: RunStore, run_id: str) -> list[dict] | None:
        """Events of a run that is not active: its stored event log, else synthesized from the stored state."""
        events = await st.read_events(run_id)
        if events:
            return events
        state = await st.load_state(run_id)
        return synthesize_events(state.to_dict()) if state is not None else None

    @app.websocket("/api/runs/{run_id}/events")
    async def run_events(ws: WebSocket, run_id: str) -> None:
        ctx: OrgContext | None = None
        if service is not None:
            ctx = await websocket_context(service, ws)
            if ctx is None:  # not signed in (or a foreign page's handshake): refuse before accepting
                await ws.close(code=1008)
                return
        if not RUN_ID_RE.match(run_id):
            await ws.close(code=1008)
            return
        st = store_for(ctx)
        await ws.accept()
        session = get_session(run_id, ctx)
        queue: asyncio.Queue | None = None
        if session is not None:
            # Subscribe before reading the store; events that arrive on both paths are dropped by ``seq`` below.
            if session.active:
                queue = asyncio.Queue()
                session.subscribers.add(queue)
            replay = await st.read_events(run_id)
        else:
            replay = await stored_events(st, run_id)  # a foreign run finds nothing here: same answer as an unknown one
            if replay is None:
                await ws.send_json({"seq": 0, "type": "error", "ts": utcnow(), "payload": {"message": "Unknown run"}})
                await ws.close(code=1000)
                return
        try:
            for event in replay:
                await ws.send_json(event)
            sent = replay[-1]["seq"] if replay else 0
            await ws.send_json({"seq": -1, "type": "replay_done", "ts": utcnow(), "payload": {"live": queue is not None}})
            if queue is not None:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    if event["seq"] > sent:
                        await ws.send_json(event)
            await ws.close(code=1000)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if session is not None and queue is not None:
                session.subscribers.discard(queue)

    # ----------------------------------------------------------------- files

    @app.get("/runs/{run_id}/files/{file_path:path}")
    async def run_file(run_id: str, file_path: str, ctx: OrgContext | None = Depends(require_role("viewer"))):
        _valid_run_id(run_id)
        if not await known_run(ctx, run_id):  # the org check comes before anything on disk is looked at
            raise HTTPException(status_code=404, detail="Not found")
        st = store_for(ctx)
        if file_path == "report.md":
            report_md = await st.load_report_markdown(run_id)
            if report_md is not None:
                return Response(report_md, media_type="text/markdown; charset=utf-8")
        target = st.artifact_file(run_id, file_path)
        if target is None:
            raise HTTPException(status_code=404, detail="Not found")
        if target.suffix.lower() == ".md":
            return Response(target.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")
        return FileResponse(target)

    # ----------------------------------------------------------------- scenarios, sim proxy, health

    @app.get("/api/scenarios")
    async def scenarios(ctx: OrgContext | None = Depends(require_role("viewer"))) -> list[dict]:
        return load_scenarios()

    async def sim_call(method: str, path: str, body: dict | None = None) -> Any:
        url = app.state.settings.SIM_BASE_URL.rstrip("/") + path
        try:
            if app.state.sim_http is not None:
                resp = await app.state.sim_http.request(method, url, json=body, headers=app.state.settings.sim_admin_headers)
            else:
                async with httpx.AsyncClient(timeout=SIM_TIMEOUT_S) as client:
                    resp = await client.request(method, url, json=body, headers=app.state.settings.sim_admin_headers)
        except httpx.HTTPError as err:
            raise HTTPException(status_code=502, detail=f"Simulated company is not reachable at {url}: {err}") from err
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
        return resp.json()

    @app.post("/api/sim/reset")
    async def sim_reset(req: ResetRequest, ctx: OrgContext | None = Depends(require_role("admin"))) -> Any:
        if ctx is not None:  # database mode never touches the shared default tenant: reset the org's own
            return RedirectResponse("/api/admin/environment/reset", status_code=307)
        if active_session(None) is not None:
            raise HTTPException(status_code=409, detail="A run is active; resetting the environment now would corrupt it.")
        return await sim_call("POST", "/admin/reset", {"chaos": req.chaos})

    @app.get("/api/sim/chaos")
    async def sim_chaos(ctx: OrgContext | None = Depends(require_role("viewer"))) -> Any:
        if ctx is None:
            return await sim_call("GET", "/admin/chaos")
        tenant_id = (await load_org(ctx.org_id)).sim_tenant_id
        if not tenant_id:
            raise HTTPException(status_code=409, detail="This organization has no environment yet.")
        try:
            return await org_chaos(tenant_id, app.state.settings, app.state.sim_http)
        except ProvisioningError as err:
            raise HTTPException(status_code=502, detail=str(err)) from None

    @app.get("/api/environment/enter", include_in_schema=False)
    async def enter_environment(ctx: OrgContext | None = Depends(require_role("operator"))):
        """Open the org's own simulated company in the browser (via the proxy): sets the tenant cookie, then redirects."""
        if ctx is None or not base_settings.PUBLIC_SIM_PROXY:
            raise HTTPException(status_code=404, detail="Not found")
        environment = await load_org_environment(await load_org(ctx.org_id), org_vault())
        entry = environment.scope.internal("sim_entry") if environment else None
        if entry is None:
            raise HTTPException(status_code=409, detail="This organization has no environment yet.")
        return RedirectResponse(f"/t/{entry.fields['value']}/?next=/company", status_code=303)

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        current: Settings = app.state.settings
        ctx: OrgContext | None = None
        if service is not None:
            ctx = await service.resolve(request.cookies.get(SESSION_COOKIE))
            if ctx is None:
                return {"status": "ok"}  # public health check: nothing about the deployment
        elif access.enabled and not access.is_authenticated(dict(request.cookies)):
            return {"status": "ok"}
        busy = active_session(ctx)
        chain = current.gemini_models
        model = {"gemini": active_model(chain), "ollama": current.OLLAMA_MODEL}.get(current.LLM_PROVIDER, "scripted")
        in_use = getattr(busy.runner.llm, "active_model", None) if busy else None
        if in_use and current.LLM_PROVIDER == "gemini":
            model = in_use
        unavailable = DEFAULT_HEALTH.exhausted(chain) if current.LLM_PROVIDER == "gemini" else []
        try:
            await sim_call("GET", "/admin/chaos")
            sim_ok = True
        except HTTPException:
            sim_ok = False
        return {
            "status": "ok", "provider": current.LLM_PROVIDER, "model": model, "models_unavailable": unavailable,
            "approval_mode": current.APPROVAL_MODE, "sim_reachable": sim_ok, "sim_url": current.SIM_BASE_URL,
            "api_key_set": bool(current.GEMINI_API_KEY) or current.LLM_PROVIDER != "gemini",
            "headless": current.HEADLESS or current.DEPLOY_MODE, "active_run": busy.run_id if busy else None,
            **await public_config(ctx),
        }

    @app.get("/api/config")
    async def config(ctx: OrgContext | None = Depends(require_role("viewer"))) -> dict:
        return await public_config(ctx)

    # ----------------------------------------------------------------- auth and administration

    if service is not None:
        app.include_router(build_auth_router(service))
        app.include_router(build_admin_router(service=service, get_vault=org_vault, settings=base_settings,
                                              is_busy=org_is_busy, sim_client=lambda: app.state.sim_http))
    else:
        @app.get("/api/auth/me")
        async def auth_me() -> dict:
            """The dashboard asks this to learn which mode it is in."""
            return {"mode": "file"}

    # ----------------------------------------------------------------- dashboard

    @app.middleware("http")
    async def no_cache_ui(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/ui/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    async def index():
        index_file = UI_DIR / "index.html"
        if not index_file.is_file():
            return JSONResponse({"detail": "Dashboard files are missing (worker/ui/index.html)."}, status_code=404)
        return FileResponse(index_file)

    if UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")
    if base_settings.PUBLIC_SIM_PROXY:
        install_sim_proxy(app, base_settings)
    if service is not None:
        app.add_middleware(CsrfGuard)
        if base_settings.PUBLIC_SIM_PROXY:
            app.add_middleware(SessionGate, service=service, prefixes=tuple(f"/{p}" for p in PROXY_PREFIXES) + ("/company",))
    elif access.enabled:
        install_access_control(app, access)
    return app


async def check_schema(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Log a clear error at startup when the database is unreachable or not migrated to the current head."""
    try:
        async with sessionmaker() as session:
            current = (await session.execute(text("select version_num from alembic_version"))).scalar()
    except Exception as exc:  # noqa: BLE001 - the server still starts; requests will report the database problem
        log.error("The database is not usable (%s). Run: python scripts/bootstrap.py", type(exc).__name__)
        return
    if current != migrate.head_revision():
        log.error("The database schema is at revision %s but the code needs %s. Run: python -m worker.db migrate",
                  current, migrate.head_revision())


app = create_app()


def main() -> None:
    """Entry point: ``python -m worker.server``."""
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("worker.server:app", host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
