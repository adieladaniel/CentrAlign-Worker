"""The run and approval endpoints of database mode.

Everything here is stateless: any number of API processes can serve it. Runs are not executed here; ``POST /api/runs``
only inserts a ``queued`` run (``worker.jobqueue``) that a worker process claims (``worker.jobs``). Approvals are rows
(``worker.approvals_pg``): ``POST .../respond`` writes the answer and the waiting worker hears it through NOTIFY. Live
events reach WebSockets through the process's ``EventHub`` (``worker.live``).

Every endpoint is scoped to the caller's active organisation; another org's runs and approvals are indistinguishable
from unknown ones (404).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker import jobqueue
from worker.agent.state import utcnow
from worker.approvals_pg import ApprovalStore
from worker.auth import AuthService, OrgContext, require_db_role, websocket_context
from worker.config import Settings
from worker.db.util import maybe_uuid, parse_ts
from worker.event_log import synthesize_events
from worker.live import EventHub
from worker.db.models import Org
from worker.org_settings import answer_rights, effective, stricter_mode
from worker.redaction import Redactor
from worker.run_context import headless_for, load_org_run_context
from worker.store import RUN_ID_RE
from worker.store_pg import PgRunStore
from worker.vault import Vault, VaultError

log = logging.getLogger("worker.runs_api")


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


def seconds_until_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    return max(1, int((now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1) - now).total_seconds()))


def build_runs_router(
    *,
    service: AuthService,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    hub: EventHub,
    get_vault: Callable[[], Vault],
    base_store: PgRunStore,
) -> APIRouter:
    router = APIRouter(tags=["runs"])
    viewer, operator = require_db_role("viewer"), require_db_role("operator")

    def store_for(ctx: OrgContext) -> PgRunStore:
        return base_store.for_org(ctx.org_id, ctx.user_id)

    def valid_run_id(run_id: str) -> str:
        if not RUN_ID_RE.match(run_id):
            raise HTTPException(status_code=400, detail="Invalid run id")
        return run_id

    async def org_context(ctx: OrgContext):
        try:
            return await load_org_run_context(sessionmaker, get_vault(), settings, ctx.org_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="Unknown organization") from None
        except VaultError as err:
            raise HTTPException(status_code=500, detail=f"The credential vault cannot be opened: {err}") from err

    async def org_cfg(ctx: OrgContext) -> dict:
        """The org's effective settings (no vault needed)."""
        async with sessionmaker() as session:
            org = await session.get(Org, ctx.org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Unknown organization")
        return effective(org.settings, settings)

    # ----------------------------------------------------------------- start, list, read, cancel

    @router.post("/api/runs")
    async def start_run(req: RunRequest, ctx: OrgContext = Depends(operator)) -> dict:
        """Queue a run. The checks that depend on the org (environment, daily quota) happen here; the run itself is
        built and executed by the worker that claims it."""
        task = req.task.strip()
        if not task:
            raise HTTPException(status_code=422, detail="Task is empty")
        org = await org_context(ctx)
        if org.environment is None:
            raise HTTPException(status_code=409, detail="This organization has no environment yet. An admin must provision "
                                                        "it (Admin > Environment) before runs can start.")
        approval_mode = stricter_mode(org.cfg["approval_mode"], req.approval_mode)
        redactor = Redactor.from_scope(org.environment.scope)
        if redactor:  # a secret typed into the task text must not be stored in clear
            task = redactor.redact(task)
        limit = org.cfg["max_runs_per_day"]
        try:
            run_id = await jobqueue.enqueue_run(
                sessionmaker, ctx.org_id, ctx.user_id, task=task, approval_mode=approval_mode,
                options={"headless": headless_for(settings, req.headed)}, daily_limit=limit or 0)
        except jobqueue.QuotaExceeded:
            raise HTTPException(
                status_code=429, headers={"Retry-After": str(seconds_until_utc_midnight())},
                detail=f"Your organization reached its limit of {limit} runs per day (UTC). "
                       "An admin can raise it in the organization settings.") from None
        await service.audit("run.start", ctx=ctx, target_type="run", target_id=run_id,
                            details={"approval_mode": approval_mode, "queued": True})
        return {"run_id": run_id, "status": "queued", "started_by": ctx.actor()}

    @router.get("/api/runs")
    async def list_runs(ctx: OrgContext = Depends(viewer)) -> list[dict]:
        rows = await jobqueue.run_rows(sessionmaker, ctx.org_id, with_worker=ctx.has_role("admin"))
        creators = await store_for(ctx).creators([r["id"] for r in rows])
        for row in rows:
            row["started_by"] = creators.get(row["id"])
        return rows

    @router.get("/api/runs/{run_id}")
    async def get_run(run_id: str, ctx: OrgContext = Depends(viewer)) -> dict:
        valid_run_id(run_id)
        store = store_for(ctx)
        # Read ``active`` before the state: if it says "finished", the state read next is final too (never the reverse).
        info = await jobqueue.run_queue_info(sessionmaker, ctx.org_id, run_id, with_worker=ctx.has_role("admin"))
        has_report = await store.load_report_markdown(run_id) is not None  # saved after the state's final_report: read first
        state = await store.load_state(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Unknown run")
        data = state.to_dict()
        pending = await ApprovalStore(sessionmaker, ctx.org_id).pending_for_run(run_id)
        data["pending"] = [{"request_id": p["id"], "kind": p["kind"], "payload": p["payload"],
                            "requested_at": p["requested_at"], "expires_at": p["expires_at"]} for p in pending]
        data.update(info)
        data["started_by"] = (await store.creators([run_id])).get(run_id)
        data["report"] = data.get("final_report")
        data["report_url"] = f"/runs/{run_id}/files/report.md" if has_report else None
        return json.loads(json.dumps(data, default=str))

    @router.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, ctx: OrgContext = Depends(operator)) -> dict:
        valid_run_id(run_id)
        store = store_for(ctx)
        if not await store.exists(run_id):
            raise HTTPException(status_code=404, detail="Unknown run")
        outcome = await jobqueue.request_cancel(sessionmaker, store, run_id)
        if outcome is None:
            raise HTTPException(status_code=409, detail="This run is not active")
        await service.audit("run.cancel", ctx=ctx, target_type="run", target_id=run_id, details={"outcome": outcome})
        return {"status": "cancelled" if outcome == "cancelled" else "cancelling"}

    # ----------------------------------------------------------------- answering

    @router.post("/api/runs/{run_id}/respond")
    async def respond(run_id: str, req: RespondRequest, ctx: OrgContext = Depends(viewer)) -> dict:
        """Answer an approval or question. Any API process can do it: the answer is written to the approval row (first
        writer wins) and the waiting worker is woken with NOTIFY."""
        valid_run_id(run_id)
        store = store_for(ctx)
        if not await store.exists(run_id):
            raise HTTPException(status_code=404, detail="Unknown run")
        approvals = ApprovalStore(sessionmaker, ctx.org_id)
        approval = await approvals.get(req.request_id, run_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="No such pending request")
        kind, payload = approval["kind"], approval["payload"]
        starter = await store.creator_id(run_id)
        can_answer, can_approve, reason = answer_rights(ctx.role, str(ctx.user_id), await org_cfg(ctx), kind,
                                                        str(payload.get("risk", "")), starter)
        if not can_answer or (kind == "approval" and req.approved and not can_approve):
            raise HTTPException(status_code=403, detail=reason)
        if kind == "approval":
            response: dict[str, Any] = {"approved": bool(req.approved), "note": req.note or "", "remember": bool(req.remember)}
            status = "approved" if req.approved else "denied"
        else:
            response = {"answer": (req.answer if req.answer is not None else req.note) or ""}
            status = "answered"
        response["by"] = ctx.actor()  # recorded in the approval_resolved event and the run's step
        done = await approvals.respond(uuid.UUID(approval["id"]), status, response, ctx.user_id)
        if done is None:
            now = await approvals.get(approval["id"], run_id)
            expired = now is not None and (now["status"] == "expired" or (now["status"] == "pending" and now["expires_at"]
                                                                          and parse_ts(now["expires_at"]) <= datetime.now(timezone.utc)))
            raise HTTPException(status_code=409, detail="This request has expired and can no longer be answered." if expired
                                else "Request was already resolved")
        await service.audit("approval.respond" if kind == "approval" else "question.answer", ctx=ctx, target_type="run",
                            target_id=run_id, details={"request_id": approval["id"], "approved": response.get("approved"),
                                                       "risk": payload.get("risk"), "tool": payload.get("tool"),
                                                       "run_started_by": starter})
        return {"status": "ok", "kind": kind}

    # ----------------------------------------------------------------- approvals inbox

    def inbox_item(item: dict, ctx: OrgContext, cfg: dict) -> dict:
        payload = item["payload"] or {}
        starter = (item.get("run_started_by") or {}).get("id")
        can_respond, can_approve, reason = answer_rights(ctx.role, str(ctx.user_id), cfg, item["kind"],
                                                         str(payload.get("risk", "")), starter)
        now = datetime.now(timezone.utc)
        requested, expires = parse_ts(item["requested_at"]), parse_ts(item["expires_at"])
        return {
            "id": item["id"], "request_id": item["id"], "run_id": item["run_id"], "run_task": item["run_task"],
            "kind": item["kind"], "status": item["status"], "tool": payload.get("tool"),
            "action": payload.get("action") or payload.get("question") or payload.get("tool"),
            "risk": payload.get("risk"), "payload": payload, "requested_by": item["run_started_by"],
            "requested_at": item["requested_at"], "expires_at": item["expires_at"],
            "age_s": max(0, int((now - requested).total_seconds())) if requested else 0,
            "expires_in_s": int((expires - now).total_seconds()) if expires else None,
            "can_respond": can_respond and item["status"] == "pending",
            "can_approve": can_approve and item["status"] == "pending", "reason": reason,
        }

    @router.get("/api/approvals")
    async def list_approvals(status: str = "pending", limit: int = 100, ctx: OrgContext = Depends(viewer)) -> list[dict]:
        """The org's approvals across all runs (default: the ones still waiting), with what the caller may do with each."""
        if status not in ("pending", "all", "approved", "denied", "answered", "expired", "cancelled"):
            raise HTTPException(status_code=422, detail="Unknown status")
        cfg = await org_cfg(ctx)
        items = await ApprovalStore(sessionmaker, ctx.org_id).list(status, limit)
        return [inbox_item(i, ctx, cfg) for i in items]

    @router.websocket("/api/approvals/stream")
    async def approvals_stream(ws: WebSocket) -> None:
        """A light push channel: ``{"type": "changed"}`` whenever an approval of the caller's org is raised or resolved.
        The dashboard refetches ``GET /api/approvals`` on it (no data travels over this socket)."""
        ctx = await websocket_context(service, ws)
        if ctx is None:
            await ws.close(code=1008)
            return
        await hub.ensure_started()
        await ws.accept()
        org_key = ctx.org_id.hex
        queue = hub.watch_org(org_key)
        try:
            await ws.send_json({"type": "hello", "ts": utcnow()})
            while True:
                await queue.get()
                await ws.send_json({"type": "changed", "ts": utcnow()})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.unwatch_org(org_key, queue)

    # ----------------------------------------------------------------- live events

    @router.websocket("/api/runs/{run_id}/events")
    async def run_events(ws: WebSocket, run_id: str) -> None:
        ctx = await websocket_context(service, ws)
        if ctx is None or not RUN_ID_RE.match(run_id):
            await ws.close(code=1008)
            return
        store = store_for(ctx)
        await ws.accept()
        sub = None
        try:
            if maybe_uuid(run_id) is not None and await store.exists(run_id):
                # Subscribe before reading the store; events that arrive on both paths are dropped by ``seq`` below.
                sub = await hub.subscribe(run_id, store)
            replay = await store.read_events(run_id)
            if not replay:
                state = await store.load_state(run_id)
                replay = synthesize_events(state.to_dict()) if state is not None else None
            if replay is None:
                await ws.send_json({"seq": 0, "type": "error", "ts": utcnow(), "payload": {"message": "Unknown run"}})
                await ws.close(code=1000)
                return
            for event in replay:
                await ws.send_json(event)
            sent = replay[-1]["seq"] if replay else 0
            live = sub is not None and not (replay and replay[-1]["type"] == "run_finished")
            await ws.send_json({"seq": -1, "type": "replay_done", "ts": utcnow(), "payload": {"live": live}})
            while live:
                event = await sub.queue.get()
                if event is None:
                    break
                if event["seq"] > sent:
                    sent = event["seq"]
                    await ws.send_json(event)
            await ws.close(code=1000)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if sub is not None:
                hub.unsubscribe(sub)

    return router
