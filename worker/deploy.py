"""Opt-in deployment helpers: access password gate, reverse proxy to the simulated company, daily run cap.

Everything here is switched on by environment variables (see ``Settings``): ``ACCESS_PASSWORD``, ``PUBLIC_SIM_PROXY``,
``DEPLOY_MODE`` and ``MAX_RUNS_PER_DAY``. With none of them set the worker behaves exactly as before.
"""
from __future__ import annotations

import asyncio
import html
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.background import BackgroundTask

from worker.config import Settings

AUTH_COOKIE = "ca_auth"
AUTH_MAX_AGE_S = 7 * 24 * 3600
# Paths reachable without a session (health check for the platform, the login flow itself).
PUBLIC_PATHS = frozenset({"/login", "/logout", "/api/health"})
# Sim paths exposed through the proxy. /company is an alias for the sim's landing page "/".
PROXY_PREFIXES = ("mail", "portal", "erp", "static")
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
                        "transfer-encoding", "upgrade"})
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


# --------------------------------------------------------------------------- run cap


class DailyRunCap:
    """Counts runs started per UTC day (in memory). ``limit <= 0`` means unlimited."""

    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))
        self._day = self._today()
        self._count = 0

    @staticmethod
    def _today():
        return datetime.now(timezone.utc).date()

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._count = today, 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def remaining(self) -> int | None:
        if not self.enabled:
            return None
        self._roll()
        return max(0, self.limit - self._count)

    def allow(self) -> bool:
        return not self.enabled or bool(self.remaining())

    def record(self) -> None:
        self._roll()
        self._count += 1

    @staticmethod
    def seconds_until_reset() -> int:
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, int((tomorrow - now).total_seconds()))


# --------------------------------------------------------------------------- access control


def safe_next(value: str | None) -> str:
    """Only same-site absolute paths are valid redirect targets after login."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value and "\n" not in value:
        return value
    return "/"


def _client_ip(scope_or_request) -> str:
    headers = dict(getattr(scope_or_request, "headers", {}) or {})
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(scope_or_request, "client", None)
    return client.host if client else "?"


class AccessControl:
    """Signed-cookie session gate plus a brute-force slowdown for the login form."""

    WINDOW_S = 600
    PER_IP_LOCKOUT = 10
    GLOBAL_LOCKOUT = 60

    def __init__(self, password: str, secret: str = "") -> None:
        self.password = password
        self._serializer = URLSafeTimedSerializer(secret or secrets.token_hex(32), salt="centralign-access")
        self._failures: dict[str, list[float]] = {}
        self._all_failures: list[float] = []

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    # -- cookie
    def issue_token(self) -> str:
        return self._serializer.dumps({"ok": 1})

    def is_authenticated(self, cookies: dict[str, str]) -> bool:
        token = cookies.get(AUTH_COOKIE)
        if not token:
            return False
        try:
            data = self._serializer.loads(token, max_age=AUTH_MAX_AGE_S)
        except (BadSignature, SignatureExpired):
            return False
        return isinstance(data, dict) and data.get("ok") == 1

    # -- password check with slowdown
    def _recent(self, stamps: list[float]) -> list[float]:
        cutoff = time.monotonic() - self.WINDOW_S
        return [t for t in stamps if t >= cutoff]

    def locked_out(self, ip: str) -> bool:
        self._all_failures = self._recent(self._all_failures)
        mine = self._failures[ip] = self._recent(self._failures.get(ip, []))
        return len(mine) >= self.PER_IP_LOCKOUT or len(self._all_failures) >= self.GLOBAL_LOCKOUT

    def check_password(self, candidate: str) -> bool:
        return secrets.compare_digest(candidate.encode("utf-8"), self.password.encode("utf-8"))

    def failure_delay(self, ip: str) -> float:
        """Record a failed attempt and return how long to stall the response (grows with repeated failures)."""
        now = time.monotonic()
        self._failures.setdefault(ip, []).append(now)
        self._all_failures.append(now)
        return min(0.5 * 2 ** (len(self._failures[ip]) - 1), 8.0)


def _secure_request(scope_headers: dict[str, str], scheme: str) -> bool:
    return scheme in ("https", "wss") or scope_headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def _cookie_kwargs(request: Request) -> dict:
    """HTTPS (e.g. a Space, which may be embedded in an iframe) needs SameSite=None; Secure. Local HTTP uses Lax."""
    secure = _secure_request(dict(request.headers), request.url.scheme)
    return {"httponly": True, "secure": secure, "samesite": "none" if secure else "lax",
            "max_age": AUTH_MAX_AGE_S, "path": "/"}


LOGIN_CSS = """
:root{--bg:#f3f5fa;--panel:#fff;--ink:#17203a;--muted:#667085;--line:#e3e7ef;--brand:#4f46e5;--red:#c02b2b;--red-bg:#fdecec;--red-line:#f3b9b9}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-size:14.5px;color:var(--ink);background:var(--bg);display:flex;flex-direction:column}
.topbar{flex:none;height:58px;display:flex;align-items:center;padding:0 20px;color:#fff;background:linear-gradient(100deg,#0e1530 0%,#1b2554 60%,#2b2f78 100%);box-shadow:0 2px 10px rgba(14,21,48,.35)}
.logo{width:26px;height:26px;border-radius:8px;background:var(--brand);margin-right:10px;display:inline-block;position:relative}
.logo:after{content:"";position:absolute;left:8px;top:6px;width:6px;height:11px;border:solid #fff;border-width:0 3px 3px 0;transform:rotate(40deg)}
.brand{font-weight:700;font-size:16px}.sub{margin-left:10px;color:#aab2e0;font-size:12.5px}
main{flex:1;display:flex;align-items:center;justify-content:center;padding:20px}
.card{width:100%;max-width:380px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:26px;box-shadow:0 1px 2px rgba(23,32,58,.06),0 4px 14px rgba(23,32,58,.05)}
h1{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 14px;font-weight:700}
p{margin:0 0 16px;color:var(--muted)}label{display:block;font-size:12px;font-weight:600;color:var(--muted);margin-bottom:6px}
input[type=password]{width:100%;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:#f7f8fc;color:var(--ink)}
input[type=password]:focus{outline:2px solid #c7c3f8;border-color:var(--brand);background:#fff}
button{margin-top:16px;width:100%;font:inherit;font-size:15px;font-weight:600;padding:11px 16px;border:0;border-radius:8px;background:var(--brand);color:#fff;cursor:pointer}
button:hover{background:#4338ca}
.err{margin-bottom:14px;padding:9px 12px;border-radius:8px;border:1px solid var(--red-line);background:var(--red-bg);color:var(--red);font-size:13px}
"""


def login_page(next_url: str, error: str = "", status_code: int = 200) -> HTMLResponse:
    err = f'<div class="err" role="alert">{html.escape(error)}</div>' if error else ""
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in - CentrAlign Worker</title><style>{LOGIN_CSS}</style></head>
<body><header class="topbar"><span class="logo" aria-hidden="true"></span><span class="brand">CentrAlign Worker</span><span class="sub">autonomous task agent</span></header>
<main><form class="card" method="post" action="/login">
<h1>Private demo</h1><p>Enter the access password to open the live demo.</p>{err}
<input type="hidden" name="next" value="{html.escape(next_url, quote=True)}">
<label for="pw">Password</label><input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<button type="submit">Sign in</button></form></main></body></html>"""
    return HTMLResponse(body, status_code=status_code, headers={"Cache-Control": "no-store"})


class AccessGate:
    """Pure ASGI middleware (so it also guards WebSockets): everything except ``PUBLIC_PATHS`` needs the cookie."""

    def __init__(self, app, access: AccessControl) -> None:
        self.app, self.access = app, access

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        cookies = _parse_cookies(headers.get("cookie", ""))
        if self.access.is_authenticated(cookies):
            if kind == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS") and headers.get("sec-fetch-site") == "cross-site":
                await JSONResponse({"detail": "Cross-site requests are not allowed"}, status_code=403)(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if path.startswith("/api/") or scope["method"] not in ("GET", "HEAD"):
            response: Response = JSONResponse({"detail": "Authentication required"}, status_code=401)
        else:
            target = path + (("?" + scope["query_string"].decode("latin-1")) if scope.get("query_string") else "")
            response = RedirectResponse(f"/login?next={quote(safe_next(target), safe='')}", status_code=303)
        await response(scope, receive, send)


def _parse_cookies(header: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            jar[name] = value
    return jar


def install_access_control(app: FastAPI, access: AccessControl) -> None:
    """Add /login, /logout and the gate. Call after all other routes are registered (order is irrelevant for the gate)."""

    @app.get("/login", include_in_schema=False)
    async def login_form(request: Request, next: str = "/"):
        if access.is_authenticated(dict(request.cookies)):
            return RedirectResponse(safe_next(next), status_code=303)
        return login_page(safe_next(next))

    @app.post("/login", include_in_schema=False)
    async def login_submit(request: Request, password: str = Form(""), next: str = Form("/")):
        target = safe_next(next)
        ip = _client_ip(request)
        if access.locked_out(ip):
            return login_page(target, "Too many failed attempts. Please wait a few minutes and try again.", 429)
        if not access.check_password(password):
            await asyncio.sleep(access.failure_delay(ip))
            return login_page(target, "Wrong password.", 401)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(AUTH_COOKIE, access.issue_token(), **_cookie_kwargs(request))
        return response

    @app.api_route("/logout", methods=["GET", "POST"], include_in_schema=False)
    async def logout(request: Request):
        response = RedirectResponse("/login", status_code=303)
        kwargs = _cookie_kwargs(request)
        response.delete_cookie(AUTH_COOKIE, path="/", secure=kwargs["secure"], httponly=True, samesite=kwargs["samesite"])
        return response

    app.add_middleware(AccessGate, access=access)


# --------------------------------------------------------------------------- reverse proxy to the simulated company


def rewrite_location(location: str, upstream: str) -> str:
    """Turn a redirect that points at the internal sim origin into a relative one (the browser stays on this host)."""
    parts = urlsplit(location)
    if not parts.scheme and not parts.netloc:
        return location
    up = urlsplit(upstream)
    internal = {up.netloc.lower()}
    if up.hostname in ("127.0.0.1", "localhost", "0.0.0.0"):
        port = f":{up.port}" if up.port else ""
        internal |= {f"127.0.0.1{port}", f"localhost{port}", f"0.0.0.0{port}"}
    if parts.netloc.lower() in internal:
        path = parts.path or "/"
        return path + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")
    return location


def install_sim_proxy(app: FastAPI, settings: Settings, access: AccessControl | None = None) -> None:
    """Forward /mail, /portal, /erp, /static and the /company alias to the internal sim; never /admin.

    ``app.state.sim_transport`` may be set (before the first request) to route through a test transport.
    """
    upstream = settings.SIM_BASE_URL.rstrip("/")
    app.state.sim_transport = None
    app.state.sim_client = None

    def client() -> httpx.AsyncClient:
        if app.state.sim_client is None:
            app.state.sim_client = httpx.AsyncClient(
                base_url=upstream, transport=app.state.sim_transport, follow_redirects=False,
                timeout=httpx.Timeout(30.0, connect=5.0))
        return app.state.sim_client

    async def forward(request: Request, upstream_path: str) -> Response:
        fwd_headers = []
        for key, value in request.headers.items():
            low = key.lower()
            if low in HOP_BY_HOP or low in ("host", "cookie"):
                continue
            fwd_headers.append((key, value))
        cookies = [c.strip() for c in request.headers.get("cookie", "").split(";") if c.strip()]
        cookies = [c for c in cookies if not c.startswith(f"{AUTH_COOKIE}=")]  # never leak the demo session to the sim
        if cookies:
            fwd_headers.append(("cookie", "; ".join(cookies)))
        if request.method in ("GET", "HEAD", "OPTIONS", "DELETE") and "content-length" not in request.headers \
                and "transfer-encoding" not in request.headers:
            content = None
        else:
            content = request.stream()
        try:
            target = upstream_path + (f"?{request.url.query}" if request.url.query else "")
            req = client().build_request(request.method, target, headers=fwd_headers, content=content)
            resp = await client().send(req, stream=True)
        except httpx.HTTPError as err:
            return JSONResponse({"detail": f"Simulated company is not reachable: {err.__class__.__name__}"}, status_code=502)
        out = []
        for key, value in resp.headers.multi_items():
            low = key.lower()
            if low in HOP_BY_HOP:
                continue
            if low == "location":
                value = rewrite_location(value, upstream)
            out.append((key.lower().encode("latin-1"), value.encode("latin-1")))
        response = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, background=BackgroundTask(resp.aclose))
        response.raw_headers = out  # keeps repeated Set-Cookie headers, content-length and content-encoding untouched
        return response

    def raw_path(request: Request) -> str:
        raw = request.scope.get("raw_path")
        return raw.decode("latin-1") if raw else quote(request.url.path)

    def register(path: str, to_upstream) -> None:
        async def handler(request: Request, rest: str = "") -> Response:
            return await forward(request, to_upstream(request))
        app.add_api_route(path, handler, methods=PROXY_METHODS, include_in_schema=False)

    for prefix in PROXY_PREFIXES:
        register(f"/{prefix}", raw_path)
        register(f"/{prefix}/{{rest:path}}", raw_path)
    register("/company", lambda request: "/")
    register("/company/", lambda request: "/")

    async def admin_blocked(request: Request, rest: str = "") -> Response:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    app.add_api_route("/admin", admin_blocked, methods=PROXY_METHODS, include_in_schema=False)
    app.add_api_route("/admin/{rest:path}", admin_blocked, methods=PROXY_METHODS, include_in_schema=False)

    async def close_client() -> None:
        if app.state.sim_client is not None:
            await app.state.sim_client.aclose()
            app.state.sim_client = None
    app.state.close_sim_client = close_client
