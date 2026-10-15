"""Worker API + dashboard server (FastAPI, port 8000).

Run it with ``python -m worker.server``. One agent run is active at a time; its events are logged in memory and to
``runs/<id>/events.jsonl`` and fanned out to WebSocket subscribers (``/api/runs/<id>/events``), which first receive
a replay of everything that happened so far. Approvals and questions raised by the agent are answered through
``POST /api/runs/<id>/respond``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from worker.agent.broker import InteractionBroker
from worker.agent.runner import AgentRunner
from worker.agent.state import TERMINAL_STATUSES, utcnow
from worker.agent.toolset import build_registry
from worker.config import Settings, get_settings
from worker.deploy import AccessControl, DailyRunCap, install_access_control, install_sim_proxy
from worker.llm.base import LLMClient, LLMError
from worker.llm.factory import make_llm
from worker.llm.failover import DEFAULT_HEALTH, active_model
from worker.tools.registry import ToolRegistry

log = logging.getLogger("worker.server")

UI_DIR = Path(__file__).parent / "ui"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = PROJECT_ROOT / "evals" / "scenarios"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
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
    log_path: Path
    events: list[dict] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task_handle: asyncio.Task | None = None
    finished: bool = False

    @property
    def active(self) -> bool:
        return self.task_handle is not None and not self.task_handle.done()

    async def publish(self, event_type: str, payload: dict) -> None:
        """Record an event (memory + jsonl) and push it to every subscriber."""
        event = json.loads(json.dumps({"seq": len(self.events), "type": event_type, "ts": utcnow(), "payload": payload}, default=str))
        self.events.append(event)
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("could not append to %s", self.log_path)
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


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


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
    """Rebuild an event log from a run.json (for runs that have no events.jsonl, e.g. started from the CLI)."""
    events: list[dict] = []
    run_id = state.get("run_id", "")

    def add(event_type: str, payload: dict, ts: str = "") -> None:
        events.append({"seq": len(events), "type": event_type, "ts": ts or state.get("started_at", ""),
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
) -> FastAPI:
    """Build the FastAPI app. ``llm_factory`` / ``registry_factory`` are seams for tests."""
    base_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        close = getattr(app.state, "close_sim_client", None)
        if close is not None:
            await close()

    app = FastAPI(title="CentrAlign Worker", version="1.0", lifespan=lifespan)
    app.state.llm_factory = llm_factory or make_llm
    app.state.registry_factory = registry_factory or build_registry
    app.state.settings = base_settings
    sessions: dict[str, RunSession] = {}
    app.state.sessions = sessions
    runs_dir = Path(base_settings.RUNS_DIR).resolve()
    access = AccessControl(base_settings.ACCESS_PASSWORD, base_settings.SESSION_SECRET)
    run_cap = DailyRunCap(base_settings.MAX_RUNS_PER_DAY)
    app.state.access, app.state.run_cap = access, run_cap

    def public_config() -> dict:
        cfg: Settings = app.state.settings
        return {"deploy_mode": cfg.DEPLOY_MODE, "sim_proxy": cfg.PUBLIC_SIM_PROXY, "auth_enabled": access.enabled,
                "max_runs_per_day": run_cap.limit or None, "runs_left_today": run_cap.remaining()}

    def active_session() -> RunSession | None:
        return next((s for s in sessions.values() if s.active), None)

    def get_session(run_id: str) -> RunSession | None:
        return sessions.get(run_id)

    # ----------------------------------------------------------------- runs

    @app.post("/api/runs")
    async def start_run(req: RunRequest) -> dict:
        busy = active_session()
        if busy is not None:
            raise HTTPException(status_code=409, detail=f"Run {busy.run_id} is still active; cancel it or wait for it to finish.")
        task_text = req.task.strip()
        if not task_text:
            raise HTTPException(status_code=422, detail="Task is empty")
        if not run_cap.allow():
            raise HTTPException(
                status_code=429, headers={"Retry-After": str(run_cap.seconds_until_reset())},
                detail=f"Daily demo limit reached: this deployment allows {run_cap.limit} runs per day (UTC) to protect the "
                       "shared Gemini quota. Please try again tomorrow.")
        headed = req.headed and not app.state.settings.DEPLOY_MODE  # server mode is always headless
        overrides: dict[str, Any] = {"HEADLESS": not headed}
        if req.approval_mode:
            overrides["APPROVAL_MODE"] = req.approval_mode
        run_settings = app.state.settings.model_copy(update=overrides)
        try:
            llm = app.state.llm_factory(run_settings)
            registry = app.state.registry_factory(run_settings)
        except LLMError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        run_id = uuid.uuid4().hex[:8]
        broker = InteractionBroker()  # no timeout: the dashboard user answers when ready
        session_holder: dict[str, RunSession] = {}

        async def sink(event_type: str, payload: dict) -> None:
            await session_holder["s"].publish(event_type, payload)

        runner = AgentRunner(task_text, llm, registry, run_settings, broker, run_id=run_id, event_sink=sink)
        runner.run_dir.mkdir(parents=True, exist_ok=True)
        session = RunSession(run_id=run_id, task=task_text, runner=runner, broker=broker,
                             log_path=runner.run_dir / "events.jsonl")
        session_holder["s"] = session
        sessions[run_id] = session
        run_cap.record()
        session.task_handle = asyncio.create_task(_supervise(session), name=f"run-{run_id}")
        return {"run_id": run_id}

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
    async def list_runs() -> list[dict]:
        rows: dict[str, dict] = {}
        if runs_dir.is_dir():
            for path in runs_dir.glob("*/run.json"):
                data = _read_json(path)
                if data is None:
                    continue
                rows[data.get("run_id", path.parent.name)] = {
                    "id": data.get("run_id", path.parent.name), "task": data.get("task", ""),
                    "status": data.get("status", "unknown"), "started_at": data.get("started_at", ""),
                    "finished_at": data.get("finished_at"),
                }
        for session in sessions.values():  # runs that have not written run.json yet
            rows.setdefault(session.run_id, {"id": session.run_id, "task": session.task, "status": "pending",
                                             "started_at": session.runner.state.started_at, "finished_at": None})
            if session.active:
                rows[session.run_id]["status"] = session.runner.state.status
        return sorted(rows.values(), key=lambda r: r["started_at"], reverse=True)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        _valid_run_id(run_id)
        session = get_session(run_id)
        if session is not None:
            data = session.runner.state.to_dict()
            data["pending"] = session.broker.pending()
            data["active"] = session.active
        else:
            data = _read_json(runs_dir / run_id / "run.json")
            if data is None:
                raise HTTPException(status_code=404, detail="Unknown run")
            data["pending"], data["active"] = [], False
        data["report"] = data.get("final_report")
        report_md = runs_dir / run_id / "report.md"
        data["report_url"] = f"/runs/{run_id}/files/report.md" if report_md.is_file() else None
        return json.loads(json.dumps(data, default=str))

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict:
        _valid_run_id(run_id)
        session = get_session(run_id)
        if session is None or not session.active:
            raise HTTPException(status_code=409, detail="This run is not active")
        session.runner.cancel()
        return {"status": "cancelling"}

    @app.post("/api/runs/{run_id}/respond")
    async def respond(run_id: str, req: RespondRequest) -> dict:
        _valid_run_id(run_id)
        session = get_session(run_id)
        if session is None or not session.active:
            raise HTTPException(status_code=409, detail="This run is not active")
        pending = {p["request_id"]: p for p in session.broker.pending()}
        request = pending.get(req.request_id)
        if request is None:
            raise HTTPException(status_code=404, detail="No such pending request (already answered?)")
        if request["kind"] == "approval":
            response: dict[str, Any] = {"approved": bool(req.approved), "note": req.note or "", "remember": bool(req.remember)}
        else:
            response = {"answer": (req.answer if req.answer is not None else req.note) or ""}
        if not session.broker.resolve(req.request_id, response):
            raise HTTPException(status_code=409, detail="Request was already resolved")
        return {"status": "ok", "kind": request["kind"]}

    # ----------------------------------------------------------------- live events

    def stored_events(run_id: str) -> list[dict] | None:
        """Events of a run that is not in memory: events.jsonl, else synthesized from run.json."""
        run_dir = runs_dir / run_id
        log_path = run_dir / "events.jsonl"
        if log_path.is_file():
            events = []
            for line in log_path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
            return events
        state = _read_json(run_dir / "run.json")
        return synthesize_events(state) if state is not None else None

    @app.websocket("/api/runs/{run_id}/events")
    async def run_events(ws: WebSocket, run_id: str) -> None:
        if not RUN_ID_RE.match(run_id):
            await ws.close(code=1008)
            return
        await ws.accept()
        session = get_session(run_id)
        queue: asyncio.Queue | None = None
        if session is not None:
            # Snapshot and subscribe without awaiting in between, so no event is missed or duplicated.
            replay = list(session.events)
            if session.active:
                queue = asyncio.Queue()
                session.subscribers.add(queue)
        else:
            replay = stored_events(run_id)
            if replay is None:
                await ws.send_json({"seq": 0, "type": "error", "ts": utcnow(), "payload": {"message": "Unknown run"}})
                await ws.close(code=1000)
                return
        try:
            for event in replay:
                await ws.send_json(event)
            await ws.send_json({"seq": -1, "type": "replay_done", "ts": utcnow(), "payload": {"live": queue is not None}})
            if queue is not None:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    await ws.send_json(event)
            await ws.close(code=1000)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if session is not None and queue is not None:
                session.subscribers.discard(queue)

    # ----------------------------------------------------------------- files

    @app.get("/runs/{run_id}/files/{file_path:path}")
    async def run_file(run_id: str, file_path: str):
        _valid_run_id(run_id)
        root = (runs_dir / run_id).resolve()
        try:
            target = (root / file_path).resolve()
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="Not found") from None
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        if target.suffix.lower() == ".md":
            return Response(target.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")
        return FileResponse(target)

    # ----------------------------------------------------------------- scenarios, sim proxy, health

    @app.get("/api/scenarios")
    async def scenarios() -> list[dict]:
        return load_scenarios()

    async def sim_call(method: str, path: str, body: dict | None = None) -> Any:
        url = app.state.settings.SIM_BASE_URL.rstrip("/") + path
        try:
            async with httpx.AsyncClient(timeout=SIM_TIMEOUT_S) as client:
                resp = await client.request(method, url, json=body)
        except httpx.HTTPError as err:
            raise HTTPException(status_code=502, detail=f"Simulated company is not reachable at {url}: {err}") from err
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
        return resp.json()

    @app.post("/api/sim/reset")
    async def sim_reset(req: ResetRequest) -> Any:
        if active_session() is not None:
            raise HTTPException(status_code=409, detail="A run is active; resetting the environment now would corrupt it.")
        return await sim_call("POST", "/admin/reset", {"chaos": req.chaos})

    @app.get("/api/sim/chaos")
    async def sim_chaos() -> Any:
        return await sim_call("GET", "/admin/chaos")

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        current: Settings = app.state.settings
        if access.enabled and not access.is_authenticated(dict(request.cookies)):
            return {"status": "ok"}  # public health check: nothing about the deployment
        busy = active_session()
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
            **public_config(),
        }

    @app.get("/api/config")
    async def config() -> dict:
        return public_config()

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
    if access.enabled:
        install_access_control(app, access)
    return app


app = create_app()


def main() -> None:
    """Entry point: ``python -m worker.server``."""
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("worker.server:app", host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
