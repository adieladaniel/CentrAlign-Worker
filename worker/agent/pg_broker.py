"""``PgBroker``: the human-in-the-loop channel of a run executing in a worker process (database mode).

Instead of an in-memory future, a request is a row of ``approvals``. The API process of the person who answers
(``POST /api/runs/<id>/respond``, any copy of it) updates the row and sends ``NOTIFY approval_responses``; the worker
that runs the agent hears it through its process-wide listener (``ApprovalSignals``) and returns the stored answer. A
slow poll of the row backs up a missed NOTIFY, so a lost notification only delays the run by ``poll_s``.

Because the request is durable, it survives crashes on both sides:

* the API can restart while a card is open: the answer is still written to the row;
* the worker can die while waiting: the run is re-queued, and the broker of the worker that resumes it **re-attaches** to
  the same open request (same kind, tool and arguments), or takes the answer that arrived while nobody waited. If the
  resumed agent asks for something different, the leftover request is cancelled as superseded.

Outcomes returned to the runner: the stored response dict, or ``None`` when the request expired (``last_expired`` is
set and ``no_response_reason`` says "approval expired after N min") or was cancelled with the run.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.agent.broker import InteractionBroker, OnCreated
from worker.approvals_pg import ANSWERED, ApprovalStore, minutes_text
from worker.pg_listen import CHANNEL_APPROVAL_RESPONSES, PgListener

log = logging.getLogger("worker.pg_broker")


class ApprovalSignals:
    """Wakes the brokers waiting for an answer. One per worker process, fed by the shared listener."""

    def __init__(self, listener: PgListener | None):
        self._waiting: dict[str, set[asyncio.Event]] = {}
        if listener is not None:
            listener.subscribe(CHANNEL_APPROVAL_RESPONSES, self._on_notify)
            listener.on_resync(self.wake_all)  # answers may have arrived while the connection was down

    def _on_notify(self, payload: str) -> None:
        for event in self._waiting.get(payload.rpartition(":")[2], ()):
            event.set()

    def wake_all(self) -> None:
        for events in self._waiting.values():
            for event in events:
                event.set()

    def watch(self, approval_id: uuid.UUID) -> asyncio.Event:
        event = asyncio.Event()
        self._waiting.setdefault(str(approval_id), set()).add(event)
        return event

    def unwatch(self, approval_id: uuid.UUID, event: asyncio.Event) -> None:
        events = self._waiting.get(str(approval_id))
        if events is not None:
            events.discard(event)
            if not events:
                del self._waiting[str(approval_id)]


class PgBroker(InteractionBroker):
    """Approvals and questions of one run, stored in PostgreSQL. ``timeout_s`` is the org's approval timeout
    (None = never expires)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        org_id: str | uuid.UUID,
        run_id: str,
        signals: ApprovalSignals,
        *,
        timeout_s: float | None = None,
        poll_s: float = 3.0,
    ):
        super().__init__(default_timeout=timeout_s)
        self.approvals = ApprovalStore(sessionmaker, org_id)
        self.run_id = run_id
        self.signals = signals
        self.poll_s = poll_s
        self.keep_pending = False  # set when the run is being handed over: its open requests must survive the cancel

    def pending(self) -> list[dict]:
        """Not available in-process: the open requests of a run are rows (``ApprovalStore.pending_for_run``)."""
        return []

    def cancel_all(self) -> None:
        """Nothing in memory to cancel; the runner cancels the waiting task, which cancels the row (see ``request``)."""

    async def request(self, kind: str, payload: dict, *, timeout: float | None = None,
                      on_created: OnCreated | None = None) -> Any:
        self.last_expired, self.no_response_reason = False, ""
        timeout_s = timeout if timeout is not None else self.default_timeout
        existing = await self.approvals.find_reusable(self.run_id, kind, payload)
        if existing is not None:
            approval = existing
            await self.approvals.cancel_for_run(self.run_id, "superseded after the run resumed", keep=uuid.UUID(existing["id"]))
        else:
            await self.approvals.cancel_for_run(self.run_id, "superseded after the run resumed")  # leftovers of a crash
            approval = await self.approvals.create(self.run_id, kind, payload, timeout_s)
        approval_id = uuid.UUID(approval["id"])
        event = self.signals.watch(approval_id)
        try:
            if on_created is not None:
                await on_created(str(approval_id))
            return await self._wait(approval_id, event)
        except asyncio.CancelledError:
            if not self.keep_pending:
                await asyncio.shield(self._cancel_quietly(approval_id))
            raise
        finally:
            self.signals.unwatch(approval_id, event)

    async def _wait(self, approval_id: uuid.UUID, event: asyncio.Event) -> Any:
        while True:
            state = await self.approvals.state(approval_id)
            if state is None:  # the row is gone (run deleted)
                return None
            status = state["status"]
            if status in ANSWERED:
                await self.approvals.consume(approval_id)
                return state["response"]
            if status == "expired" or (status == "pending" and state["past_deadline"]):
                await self.approvals.expire(approval_id)
                self.last_expired = True
                self.no_response_reason = f"approval expired after {minutes_text(state['open_s'] or 0)}"
                return None
            if status == "cancelled":
                self.no_response_reason = "the request was cancelled"
                return None
            wait = self.poll_s if state["seconds_left"] is None else max(0.05, min(self.poll_s, state["seconds_left"] + 0.05))
            try:
                await asyncio.wait_for(event.wait(), wait)
            except asyncio.TimeoutError:
                pass
            event.clear()

    async def _cancel_quietly(self, approval_id: uuid.UUID) -> None:
        try:
            await self.approvals.cancel(approval_id, "the run was cancelled")
        except Exception:  # noqa: BLE001 - e.g. the database is gone; the reaper expires it later
            log.warning("could not cancel approval %s", approval_id, exc_info=True)
