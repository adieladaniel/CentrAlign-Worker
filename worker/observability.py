"""The request-level observability middleware: request ids, the request log line, HTTP and WebSocket metrics.

A pure ASGI middleware (it also sees WebSockets and does not buffer responses). For every HTTP request it

* takes ``X-Request-ID`` from the client when it is a sane token (letters, digits, ``._-``, at most 64 characters), else
  makes one up, binds it as the ``request_id`` log context and echoes it in the response header;
* counts the request and its duration under the **route template** (``/api/runs/{run_id}``), never the raw path, so ids
  cannot blow up the label cardinality; requests that match no route share the label ``unmatched``;
* writes one log line (``GET /api/runs -> 200 in 12 ms``; health checks and /metrics only at DEBUG).

For WebSockets it keeps the ``oneforall_ws_connections`` gauge (from accept to close) and binds a request id.
Add it last in ``create_app`` so it is the outermost layer and also sees what the other middleware rejects.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid

from worker import metrics
from worker.logging_setup import bind_context, isolate_identity, reset_context

log = logging.getLogger("worker.http")

REQUEST_ID_HEADER = "x-request-id"
_VALID_ID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
QUIET_PATHS = frozenset({"/healthz", "/readyz", "/metrics", "/api/health"})


def route_template(scope: dict) -> str:
    """The path template the request was routed to (``/api/runs/{run_id}``), or a fixed label when nothing matched."""
    path = getattr(scope.get("route"), "path", None)
    if isinstance(path, str) and path:
        return path
    raw = scope.get("path", "")
    if raw.startswith("/ui/"):
        return "/ui/{file}"
    return "/" if raw == "/" else "unmatched"


def new_request_id(headers: list[tuple[bytes, bytes]]) -> str:
    for key, value in headers:
        if key == b"x-request-id":
            candidate = value.decode("latin-1").strip()
            if _VALID_ID.match(candidate):
                return candidate
    return uuid.uuid4().hex[:16]


class ObservabilityMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind == "http":
            await self._http(scope, receive, send)
        elif kind == "websocket":
            await self._websocket(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    async def _http(self, scope, receive, send) -> None:
        request_id = new_request_id(scope.get("headers", []))
        tokens = {**isolate_identity(), **bind_context(request_id=request_id)}
        status, started = 500, time.perf_counter()

        async def send_with_id(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except asyncio.CancelledError:
            status = 499  # the client went away
            raise
        finally:
            elapsed = time.perf_counter() - started
            route, method = route_template(scope), scope.get("method", "GET")
            metrics.HTTP_REQUESTS.labels(route, method, str(status)).inc()
            metrics.HTTP_DURATION.labels(route).observe(elapsed)
            path = scope.get("path", "")
            log.log(logging.DEBUG if path in QUIET_PATHS else logging.INFO, "%s %s -> %s in %d ms", method, path, status,
                    round(elapsed * 1000), extra={"method": method, "route": route, "status": status,
                                                   "duration_ms": round(elapsed * 1000)})
            reset_context(tokens)

    async def _websocket(self, scope, receive, send) -> None:
        tokens = {**isolate_identity(), **bind_context(request_id=new_request_id(scope.get("headers", [])))}
        accepted = False

        async def send_counting(message) -> None:
            nonlocal accepted
            if message["type"] == "websocket.accept" and not accepted:
                accepted = True
                metrics.WS_CONNECTIONS.inc()
            await send(message)

        try:
            await self.app(scope, receive, send_counting)
        finally:
            if accepted:
                metrics.WS_CONNECTIONS.dec()
            reset_context(tokens)
