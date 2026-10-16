"""Multi-user authentication and org context for the API (database mode).

Design
------
* **Sign-in** is e-mail + password (argon2). The password check always runs, also for unknown or deactivated accounts
  (against a dummy hash), so the response and its timing do not reveal whether an e-mail exists.
* **Sessions** are server-side rows in ``auth_sessions``. The cookie ``ca_session`` carries a random 256-bit token
  (HttpOnly, SameSite=Lax, Secure in DEPLOY_MODE); only its SHA-256 hash is stored, so a database leak cannot be replayed.
  The idle timeout slides (``SESSION_IDLE_TTL_HOURS``, extended at most once a minute), the absolute limit does not
  (``SESSION_ABSOLUTE_TTL_DAYS``). Logging out, switching to a deactivated org or deactivating a user revokes sessions.
* **Throttling**: failed sign-ins are counted per e-mail and per client address in ``login_attempts`` (fixed windows of
  ``LOGIN_WINDOW_S``). Over the limit the answer is 429 with ``Retry-After``; every failure is also answered after an
  exponentially growing pause. Unknown e-mails are counted exactly like known ones.
* **CSRF**: besides SameSite=Lax, state-changing requests that the browser marks ``Sec-Fetch-Site: cross-site`` get 403.
* **OrgContext** (org, user, role) is resolved from the session on every request and is what every endpoint scopes by.
  The role is read from the membership each time, so a role change or deactivation applies immediately.
* **Roles** are ordered viewer < approver < operator < admin < owner (``require_role``). *Approval rights* are a
  separate capability (``worker.org_settings.can_approve``): approver, admin and owner can approve; an operator only if the
  org allows it.
* **Audit**: ``AuthService.audit`` appends to ``audit_log`` (sign-in, sign-out, org switch, run start, approvals, admin).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.config import Settings
from worker.db.models import AuditLog, AuthSession, LoginAttempt, Membership, Org, User
from worker.db.seed import hash_password, verify_password
from worker.logging_setup import bind_context
from worker.org_settings import can_approve, effective, policy_view

log = logging.getLogger("worker.auth")

SESSION_COOKIE = "ca_session"
ROLE_ORDER = ("viewer", "approver", "operator", "admin", "owner")
RANK = {role: rank for rank, role in enumerate(ROLE_ORDER)}
TOUCH_INTERVAL = timedelta(seconds=60)  # the sliding expiry is extended at most this often
MAX_BACKOFF_S = 8.0
GENERIC_LOGIN_ERROR = "Invalid email or password."
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def token_hash(token: str) -> str:
    """The session id stored for a cookie token."""
    return hashlib.sha256(token.encode()).hexdigest()


def rank_of(role: str) -> int:
    return RANK.get(role, -1)


# --------------------------------------------------------------------------- the caller


@dataclass(frozen=True)
class OrgContext:
    """Who is calling and in which organisation: resolved from the session cookie for every request."""

    org_id: uuid.UUID
    org_slug: str
    user_id: uuid.UUID
    email: str
    role: str
    display_name: str = ""
    org_name: str = ""
    session_id: str = ""

    def has_role(self, minimum: str) -> bool:
        return rank_of(self.role) >= rank_of(minimum)

    @property
    def name(self) -> str:
        return self.display_name or self.email

    def actor(self) -> dict[str, str]:
        """The caller as recorded on approvals and shown in the UI."""
        return {"id": str(self.user_id), "name": self.name, "email": self.email, "role": self.role}


class LoginError(Exception):
    """Sign-in refused. ``status`` is the HTTP status; ``retry_after`` (seconds) is set when throttled."""

    def __init__(self, status: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


@dataclass
class LoginResult:
    token: str
    context: OrgContext


_dummy_hash: str | None = None


def _dummy_password_hash() -> str:
    """A valid argon2 hash of a random password: verified against when the e-mail is unknown, to equalise timing."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    return _dummy_hash


# --------------------------------------------------------------------------- the service


class AuthService:
    """Sessions, sign-in, org switching and the audit trail, over one sessionmaker. One per app (``app.state.auth``)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings,
                 clock: Callable[[], datetime] = utc_now, sleep: Callable[[float], Any] = asyncio.sleep):
        self.sm = sessionmaker
        self.settings = settings
        self.clock = clock  # injectable, like ``sleep``, so tests can move time without waiting
        self.sleep = sleep
        self.idle_ttl = timedelta(hours=settings.SESSION_IDLE_TTL_HOURS)
        self.absolute_ttl = timedelta(days=settings.SESSION_ABSOLUTE_TTL_DAYS)
        _dummy_password_hash()  # computed now, not during the first failed sign-in (that would be measurable)

    # ----------------------------------------------------------------- audit

    async def audit(self, action: str, *, ctx: OrgContext | None = None, org_id: uuid.UUID | None = None,
                    user_id: uuid.UUID | None = None, target_type: str = "", target_id: str = "",
                    details: dict[str, Any] | None = None, session: AsyncSession | None = None) -> None:
        """Append a row to ``audit_log``. Pass ``session`` to make it part of the caller's transaction."""
        row = AuditLog(org_id=org_id or (ctx.org_id if ctx else None), user_id=user_id or (ctx.user_id if ctx else None),
                       action=action, target_type=target_type, target_id=target_id, details=details or {},
                       ts=self.clock())
        if session is not None:
            session.add(row)
            return
        async with self.sm() as own, own.begin():
            own.add(row)

    # ----------------------------------------------------------------- throttling

    def _window(self, now: datetime) -> tuple[datetime, int]:
        """(start of the current window, seconds left in it)."""
        size = max(1, self.settings.LOGIN_WINDOW_S)
        start = int(now.timestamp()) // size * size
        return datetime.fromtimestamp(start, timezone.utc), max(1, start + size - int(now.timestamp()))

    @staticmethod
    def _email_key(email: str) -> str:
        return "email:" + hashlib.sha256(email.encode()).hexdigest()[:32]  # the attempted address is not stored

    async def _blocked_for(self, keys: dict[str, int], window: datetime) -> bool:
        async with self.sm() as session:
            rows = (await session.execute(select(LoginAttempt.key, LoginAttempt.count).where(
                LoginAttempt.window_start == window, LoginAttempt.key.in_(list(keys))))).all()
        return any(count >= keys[key] for key, count in rows)

    async def _count_failure(self, keys: list[str], window: datetime) -> dict[str, int]:
        counts: dict[str, int] = {}
        async with self.sm() as session, session.begin():
            for key in keys:
                counts[key] = await session.scalar(
                    pg_insert(LoginAttempt).values(key=key, window_start=window, count=1)
                    .on_conflict_do_update(index_elements=[LoginAttempt.key, LoginAttempt.window_start],
                                           set_={"count": LoginAttempt.count + 1}).returning(LoginAttempt.count))
        return counts

    # ----------------------------------------------------------------- sign-in

    async def login(self, email: str, password: str, ip: str, user_agent: str = "") -> LoginResult:
        """Check the credentials and open a session in the user's first active organisation. Raises ``LoginError``."""
        email = email.strip().lower()
        now = self.clock()
        window, seconds_left = self._window(now)
        email_key, ip_key = self._email_key(email), f"ip:{ip}"
        limits = {email_key: self.settings.LOGIN_MAX_FAILURES_PER_EMAIL, ip_key: self.settings.LOGIN_MAX_FAILURES_PER_IP}
        if await self._blocked_for(limits, window):
            minutes = math.ceil(seconds_left / 60)
            raise LoginError(429, f"Too many failed sign-in attempts. Please try again in {minutes} minute"
                                  f"{'' if minutes == 1 else 's'}.", retry_after=seconds_left)

        async with self.sm() as session:
            user = await session.scalar(select(User).where(func.lower(User.email) == email))
        # Always verify, against a dummy hash for unknown e-mails: same work, same answer, whatever the reason.
        valid = await asyncio.to_thread(verify_password, user.password_hash if user else _dummy_password_hash(), password)
        if user is None or not user.is_active or not valid:
            reason = "unknown_user" if user is None else "inactive" if not user.is_active else "bad_password"
            counts = await self._count_failure([email_key, ip_key], window)
            await self.audit("auth.login_failed", user_id=user.id if user else None,
                             details={"ip": ip, "reason": reason, "attempt": counts[email_key]})
            delay = min(self.settings.LOGIN_BACKOFF_BASE_S * 2 ** max(counts[email_key] - 1, 0), MAX_BACKOFF_S)
            if delay > 0:
                await self.sleep(delay)
            raise LoginError(401, GENERIC_LOGIN_ERROR)

        token = secrets.token_urlsafe(32)
        async with self.sm() as session, session.begin():
            member = await self._first_membership(session, user.id)
            if member is None:
                raise LoginError(403, "Your account is not an active member of any organization. Ask an administrator.")
            membership, org = member
            session.add(AuthSession(
                id=token_hash(token), user_id=user.id, org_id=org.id, created_at=now, last_seen_at=now,
                expires_at=min(now + self.idle_ttl, now + self.absolute_ttl), absolute_expires_at=now + self.absolute_ttl,
                ip=ip[:100], user_agent=user_agent[:300]))
            await session.execute(update(User).where(User.id == user.id).values(last_login_at=now))
            await session.execute(delete(LoginAttempt).where(LoginAttempt.key == email_key))
            cutoff = now - timedelta(days=1)  # housekeeping: old windows and long-dead sessions
            await session.execute(delete(LoginAttempt).where(LoginAttempt.window_start < cutoff))
            await session.execute(delete(AuthSession).where(AuthSession.absolute_expires_at < cutoff))
            await self.audit("auth.login", org_id=org.id, user_id=user.id, details={"ip": ip, "role": membership.role},
                             session=session)
        ctx = OrgContext(org_id=org.id, org_slug=org.slug, user_id=user.id, email=user.email, role=membership.role,
                         display_name=user.display_name, org_name=org.name, session_id=token_hash(token))
        return LoginResult(token=token, context=ctx)

    @staticmethod
    async def _first_membership(session: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID | None = None):
        """(Membership, Org) of the user: the one in ``org_id`` if given, else the oldest active one."""
        query = (select(Membership, Org).join(Org, Org.id == Membership.org_id)
                 .where(Membership.user_id == user_id, Membership.is_active.is_(True))
                 .order_by(Membership.created_at, Org.slug).limit(1))
        if org_id is not None:
            query = query.where(Membership.org_id == org_id)
        row = (await session.execute(query)).first()
        return (row[0], row[1]) if row else None

    # ----------------------------------------------------------------- sessions

    async def resolve(self, token: str | None) -> OrgContext | None:
        """The caller for a cookie token, or None (unknown, revoked or expired session, deactivated user, no org).

        Slides the idle expiry. If the session's org is no longer available to the user it moves to their first active
        org, so a deactivation in one org does not strand a multi-org user."""
        if not token or len(token) > 200:
            return None
        sid, now = token_hash(token), self.clock()
        async with self.sm() as session, session.begin():
            row = (await session.execute(
                select(AuthSession, User).join(User, User.id == AuthSession.user_id).where(
                    AuthSession.id == sid, AuthSession.revoked_at.is_(None), AuthSession.expires_at > now,
                    AuthSession.absolute_expires_at > now, User.is_active.is_(True)))).first()
            if row is None:
                return None
            auth_session, user = row
            member = await self._first_membership(session, user.id, auth_session.org_id)
            if member is None:
                member = await self._first_membership(session, user.id)
            if member is None:
                return None
            membership, org = member
            if now - auth_session.last_seen_at >= TOUCH_INTERVAL or auth_session.org_id != org.id:
                await session.execute(update(AuthSession).where(AuthSession.id == sid).values(
                    last_seen_at=now, org_id=org.id,
                    expires_at=min(now + self.idle_ttl, auth_session.absolute_expires_at)))
        return OrgContext(org_id=org.id, org_slug=org.slug, user_id=user.id, email=user.email, role=membership.role,
                          display_name=user.display_name, org_name=org.name, session_id=sid)

    async def revoke(self, session_id: str) -> bool:
        """End a session (by its stored id). True if it was live."""
        async with self.sm() as session, session.begin():
            result = await session.execute(update(AuthSession).where(
                AuthSession.id == session_id, AuthSession.revoked_at.is_(None)).values(revoked_at=self.clock()))
        return result.rowcount > 0

    async def revoke_user_sessions(self, user_id: uuid.UUID, org_id: uuid.UUID | None = None) -> int:
        """End every live session of a user (only those in ``org_id`` when given). Returns how many."""
        query = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        if org_id is not None:
            query = query.where(AuthSession.org_id == org_id)
        async with self.sm() as session, session.begin():
            return (await session.execute(query.values(revoked_at=self.clock()))).rowcount

    async def switch_org(self, ctx: OrgContext, org_id: uuid.UUID) -> OrgContext | None:
        """Make ``org_id`` the session's active org. None when the user has no active membership there."""
        async with self.sm() as session, session.begin():
            if await self._first_membership(session, ctx.user_id, org_id) is None:
                return None
            await session.execute(update(AuthSession).where(AuthSession.id == ctx.session_id).values(org_id=org_id))
        return await self._reload(ctx, org_id)

    async def _reload(self, ctx: OrgContext, org_id: uuid.UUID) -> OrgContext | None:
        async with self.sm() as session:
            member = await self._first_membership(session, ctx.user_id, org_id)
        if member is None:
            return None
        membership, org = member
        return OrgContext(org_id=org.id, org_slug=org.slug, user_id=ctx.user_id, email=ctx.email, role=membership.role,
                          display_name=ctx.display_name, org_name=org.name, session_id=ctx.session_id)

    # ----------------------------------------------------------------- profile

    async def me(self, ctx: OrgContext) -> dict[str, Any]:
        """The ``GET /api/auth/me`` payload: user, active org (with the approval policy), all orgs, capabilities."""
        async with self.sm() as session:
            org = await session.get(Org, ctx.org_id)
            rows = (await session.execute(
                select(Org.id, Org.slug, Org.name, Membership.role).join(Membership, Membership.org_id == Org.id)
                .where(Membership.user_id == ctx.user_id, Membership.is_active.is_(True)).order_by(Org.name))).all()
        cfg = effective(org.settings if org else {}, self.settings)
        approve = can_approve(ctx.role, cfg)
        return {
            "mode": "db",
            "user": {"id": str(ctx.user_id), "email": ctx.email, "display_name": ctx.display_name, "name": ctx.name},
            "org": {"id": str(ctx.org_id), "slug": ctx.org_slug, "name": ctx.org_name, "role": ctx.role,
                    "policy": policy_view(cfg), "environment_ready": bool(org and org.sim_tenant_id)},
            "orgs": [{"id": str(r[0]), "slug": r[1], "name": r[2], "role": r[3]} for r in rows],
            "capabilities": {"start_runs": ctx.has_role("operator"), "approve": approve, "answer": ctx.has_role("approver"),
                             "admin": ctx.has_role("admin"), "credentials": ctx.has_role("admin")},
        }

    # ----------------------------------------------------------------- cookie

    def set_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(SESSION_COOKIE, token, max_age=int(self.absolute_ttl.total_seconds()), httponly=True,
                            samesite="lax", secure=bool(self.settings.DEPLOY_MODE), path="/")

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=bool(self.settings.DEPLOY_MODE))

    def client_ip(self, request: Request) -> str:
        """The client address: the first X-Forwarded-For hop behind a proxy (DEPLOY_MODE), else the socket peer."""
        if self.settings.DEPLOY_MODE:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()[:100]
        return request.client.host if request.client else "?"


# --------------------------------------------------------------------------- request guards


class CsrfGuard:
    """Pure ASGI middleware: a state-changing request that the browser itself marks ``Sec-Fetch-Site: cross-site`` is
    refused. (Requests without the header, e.g. from scripts and tests, pass; they carry no ambient browser cookie.)"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            if headers.get("sec-fetch-site") == "cross-site":
                await JSONResponse({"detail": "Cross-site requests are not allowed"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class SessionGate:
    """Pure ASGI middleware for the proxied simulated company: paths under ``prefixes`` need a signed-in user."""

    def __init__(self, app, service: AuthService, prefixes: tuple[str, ...]) -> None:
        self.app, self.service, self.prefixes = app, service, prefixes

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope["type"] in ("http", "websocket") and any(path == p or path.startswith(p + "/") for p in self.prefixes):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            cookies = dict(part.strip().partition("=")[::2] for part in headers.get("cookie", "").split(";") if "=" in part)
            if await self.service.resolve(cookies.get(SESSION_COOKIE)) is None:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"detail": "Sign in to the dashboard first."}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def current_context(request: Request) -> OrgContext | None:
    """FastAPI dependency: the signed-in caller. 401 when there is no valid session. Returns None in file mode (there
    is no per-user auth then; shared endpoints use this so they serve both modes)."""
    service: AuthService | None = request.app.state.auth
    if service is None:
        return None
    ctx = await service.resolve(request.cookies.get(SESSION_COOKIE))
    if ctx is None:
        raise HTTPException(status_code=401, detail="Not signed in", headers={"WWW-Authenticate": "Session"})
    bind_context(org_id=ctx.org_id, user_id=ctx.user_id)  # log lines of this request carry who it is for
    return ctx


def require_role(minimum: str) -> Callable[..., Any]:
    """Dependency factory: the caller must hold at least role ``minimum`` (viewer < approver < operator < admin <
    owner), else 403. In file mode it yields None (single shared user)."""
    if minimum not in RANK:
        raise ValueError(f"unknown role {minimum!r}")

    async def dependency(ctx: OrgContext | None = Depends(current_context)) -> OrgContext | None:
        if ctx is not None and not ctx.has_role(minimum):
            raise HTTPException(status_code=403, detail=f"This needs the {minimum} role or higher (you are {ctx.role}).")
        return ctx

    return dependency


def require_db_role(minimum: str) -> Callable[..., Any]:
    """Like ``require_role`` for endpoints that exist only in database mode: always yields an ``OrgContext``."""
    inner = require_role(minimum)

    async def dependency(ctx: OrgContext | None = Depends(inner)) -> OrgContext:
        assert ctx is not None, "database-mode endpoint registered without auth"
        return ctx

    return dependency


async def websocket_context(service: AuthService, ws: WebSocket) -> OrgContext | None:
    """The caller of a WebSocket handshake (cookie auth). A browser Origin that is not this host is refused."""
    origin = ws.headers.get("origin")
    if origin and urlsplit(origin).netloc != ws.headers.get("host", ""):
        return None
    ctx = await service.resolve(ws.cookies.get(SESSION_COOKIE))
    if ctx is not None:
        bind_context(org_id=ctx.org_id, user_id=ctx.user_id)
    return ctx


# --------------------------------------------------------------------------- routes


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class SwitchOrgRequest(BaseModel):
    org_id: uuid.UUID


def build_auth_router(service: AuthService) -> APIRouter:
    """``/api/auth/login|logout|me|switch-org``."""
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post("/login")
    async def login(req: LoginRequest, request: Request, response: Response) -> dict:
        try:
            result = await service.login(req.email, req.password, service.client_ip(request),
                                         request.headers.get("user-agent", ""))
        except LoginError as err:
            headers = {"Retry-After": str(err.retry_after)} if err.retry_after else None
            raise HTTPException(status_code=err.status, detail=err.message, headers=headers) from None
        service.set_cookie(response, result.token)
        return await service.me(result.context)

    @router.post("/logout")
    async def logout(request: Request, response: Response) -> dict:
        ctx = await service.resolve(request.cookies.get(SESSION_COOKIE))
        if ctx is not None:
            await service.revoke(ctx.session_id)
            await service.audit("auth.logout", ctx=ctx)
        service.clear_cookie(response)
        return {"status": "signed_out"}

    @router.get("/me")
    async def me(ctx: OrgContext = Depends(require_db_role("viewer"))) -> dict:
        return await service.me(ctx)

    @router.post("/switch-org")
    async def switch_org(req: SwitchOrgRequest, ctx: OrgContext = Depends(require_db_role("viewer"))) -> dict:
        fresh = await service.switch_org(ctx, req.org_id)
        if fresh is None:
            raise HTTPException(status_code=403, detail="You are not a member of that organization.")
        await service.audit("auth.switch_org", ctx=fresh, target_type="org", target_id=str(fresh.org_id),
                            details={"from": str(ctx.org_id)})
        return await service.me(fresh)

    return router
