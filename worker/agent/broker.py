"""Async request/response channel between the runner and a human (UI, terminal) or an automatic responder."""
from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

OnCreated = Callable[[str], Awaitable[None]]


@dataclass
class PendingRequest:
    """A request waiting for a response."""

    id: str
    kind: str  # "approval" | "question"
    payload: dict
    future: asyncio.Future = field(repr=False, default=None)  # type: ignore[assignment]


class InteractionBroker:
    """Hands approval cards and questions to a UI and waits for the answer.

    ``request`` registers a pending request, calls ``on_created(request_id)`` so the caller can announce it
    (the UI then shows a card) and blocks until ``resolve`` is called with the same id.
    Responses: approvals are ``{"approved": bool, "note": str?, "remember": bool?}``; questions are ``{"answer": str}``.
    """

    def __init__(self, default_timeout: float | None = None):
        self.default_timeout = default_timeout
        self._pending: dict[str, PendingRequest] = {}
        # Why the last request got no answer (set by brokers that know, e.g. PgBroker): the runner puts it in the
        # denial note, and ends the run when an approval ``last_expired``.
        self.no_response_reason = ""
        self.last_expired = False

    def pending(self) -> list[dict]:
        """Open requests as plain dicts (for UIs that connect mid-run)."""
        return [{"request_id": r.id, "kind": r.kind, "payload": r.payload} for r in self._pending.values()]

    async def request(
        self, kind: str, payload: dict, *, timeout: float | None = None, on_created: OnCreated | None = None
    ) -> Any:
        """Wait for a human response. Raises ``asyncio.TimeoutError`` when ``timeout`` (or the default) elapses."""
        request = PendingRequest(id=uuid.uuid4().hex[:10], kind=kind, payload=payload)
        request.future = asyncio.get_running_loop().create_future()
        self._pending[request.id] = request
        try:
            if on_created is not None:
                await on_created(request.id)
            return await asyncio.wait_for(request.future, timeout if timeout is not None else self.default_timeout)
        finally:
            self._pending.pop(request.id, None)

    def resolve(self, request_id: str, response: Any) -> bool:
        """Deliver a response; returns False for an unknown or already-resolved request."""
        request = self._pending.get(request_id)
        if request is None or request.future.done():
            return False
        request.future.set_result(response)
        return True

    def cancel_all(self) -> None:
        """Abort every open request (the waiting callers see ``asyncio.CancelledError``)."""
        for request in list(self._pending.values()):
            request.future.cancel()


class AutoBroker(InteractionBroker):
    """Headless responder for evals and tests: ``policy_fn(kind, payload)`` returns the response (sync or async)."""

    def __init__(self, policy_fn: Callable[[str, dict], Any] | None = None):
        super().__init__()
        self._policy_fn = policy_fn or (lambda kind, payload: {"approved": True})
        self.requests: list[tuple[str, dict]] = []

    async def request(
        self, kind: str, payload: dict, *, timeout: float | None = None, on_created: OnCreated | None = None
    ) -> Any:
        self.requests.append((kind, payload))
        if on_created is not None:
            await on_created(uuid.uuid4().hex[:10])
        response = self._policy_fn(kind, payload)
        return await response if inspect.isawaitable(response) else response

    @classmethod
    def approve_all(cls, answer: str = "") -> AutoBroker:
        """Approve every action; answer every question with ``answer``."""
        return cls(lambda kind, payload: {"approved": True} if kind == "approval" else {"answer": answer})
