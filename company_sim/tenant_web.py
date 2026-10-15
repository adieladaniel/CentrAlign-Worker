"""Web side of tenancy: pick the tenant of every request, and the ``/t/<token>/`` entry route.

Resolution order (the tenant id itself is never read from the client):

1. a ``sim_tenant`` cookie holding a tenant's secret entry token (set by the entry route);
2. on the ERP REST API (``/erp/api``), the ``X-API-Key`` header: every tenant has its own key;
3. otherwise the ``default`` tenant.

An unknown or stale cookie is ignored (rather than answered with an error): browsers share cookies between ports
on ``localhost``, so a cookie from another simulator must not break the default one. ``/admin`` is never resolved
from a cookie or key; its handlers name the tenant explicitly.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from starlette.datastructures import Headers
from starlette.requests import cookie_parser

from . import tenants

router = APIRouter(tags=["tenant"])
UNTENANTED_PREFIXES = ("/admin", "/t/")


def resolve_tenant(headers: Headers, path: str) -> str:
    """Tenant id for a request with these headers and path."""
    if path.startswith(UNTENANTED_PREFIXES):
        return tenants.DEFAULT
    tenant = tenants.find_by_token(cookie_parser(headers.get("cookie", "")).get(tenants.COOKIE, ""))
    if tenant is None and path.startswith("/erp/api"):
        tenant = tenants.find_by_api_key(headers.get("x-api-key", ""))
    return tenant.id if tenant else tenants.DEFAULT


class TenantMiddleware:
    """Pure ASGI middleware: sets ``current_tenant`` for the whole request (threadpool work and other middleware too)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        marker = tenants.current_tenant.set(resolve_tenant(Headers(scope=scope), scope["path"]))
        try:
            await self.app(scope, receive, send)
        finally:
            tenants.current_tenant.reset(marker)


def safe_next(value: str) -> str:
    """Only same-site absolute paths are valid redirect targets."""
    ok = value.startswith("/") and not value.startswith("//") and "\\" not in value and "\n" not in value
    return value if ok else "/"


@router.get("/t/{token}", include_in_schema=False)
@router.get("/t/{token}/", include_in_schema=False)
async def enter(token: str, next: str = "/") -> RedirectResponse:
    """Entry link of a tenant: remember it in a cookie and go to the intranet landing page (or ``?next=/some/path``)."""
    tenant = tenants.find_by_token(token)
    if tenant is None:
        raise HTTPException(404, "Not found")
    response = RedirectResponse(safe_next(next), status_code=303)
    response.set_cookie(tenants.COOKIE, tenant.token, httponly=True, samesite="lax", path="/")
    return response
