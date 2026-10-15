"""Admin endpoints for test harnesses and the platform: chaos config, reset, ground-truth state, tenants.

If the ``SIM_ADMIN_TOKEN`` environment variable is set, every ``/admin`` request needs the matching ``X-Admin-Token``
header; when it is unset the endpoints stay open (local development and tests). ``/admin/chaos``, ``/admin/reset``
and ``/admin/state`` operate on the default tenant; ``/admin/tenants/...`` manages the others.
"""
from __future__ import annotations

import os
import secrets
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import chaos, tenants

ADMIN_TOKEN_ENV = "SIM_ADMIN_TOKEN"


def require_admin(request: Request) -> None:
    expected = os.environ.get(ADMIN_TOKEN_ENV, "")
    if expected and not secrets.compare_digest(request.headers.get("X-Admin-Token", "").encode(), expected.encode()):
        raise HTTPException(401, "Admin token required (X-Admin-Token)")


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def reseed() -> dict:
    """Wipe and reseed the current tenant using its active chaos flags; reset its counters and sessions."""
    return tenants.reset_tenant(tenants.current_tenant.get())


def _apply_chaos(body: dict):
    """Toggle (``{"flag": bool}``) or replace (``{"chaos": [...]}``) the current tenant's flags.

    Changing a data-affecting flag (duplicate_bill, ambiguous_vendor, portal_missing_invoice) reseeds the DB.
    """
    before = chaos.active()
    try:
        if "chaos" in body:
            chaos.set_flags(body["chaos"])
        else:
            chaos.update_flags({k: bool(v) for k, v in body.items()})
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)
    reseeded = bool((before ^ chaos.active()) & chaos.DATA_FLAGS)
    if reseeded:
        reseed()
    return {**chaos.snapshot(), "reseeded": reseeded}


def _reset(body: dict):
    """Wipe and reseed the current tenant. Optional body {"chaos": [...]} replaces the active flags first."""
    if "chaos" in body:
        try:
            chaos.set_flags(body["chaos"])
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
    return {"status": "reset", **reseed(), "chaos": chaos.snapshot()["active"]}


# --------------------------------------------------------------------------- default tenant (legacy endpoints)


@router.get("/chaos")
async def get_chaos() -> dict:
    return chaos.snapshot()


@router.post("/chaos")
async def set_chaos(request: Request):
    """Body: {"flag": true/false, ...} to toggle flags, or {"chaos": ["flag", ...]} to replace the set."""
    return _apply_chaos(await _json_body(request))


@router.post("/reset")
async def reset(request: Request):
    return _reset(await _json_body(request))


@router.get("/state")
async def get_state() -> dict:
    return tenants.tenant_state(tenants.DEFAULT)


# --------------------------------------------------------------------------- tenants


@contextmanager
def scoped(tenant_id: str):
    """Select a tenant for the enclosed handler code, or answer 404."""
    try:
        with tenants.use_tenant(tenant_id) as tenant:
            yield tenant
    except tenants.UnknownTenant:
        raise HTTPException(404, f"Unknown tenant '{tenant_id}'") from None


def _summary(tenant: tenants.Tenant) -> dict:
    return {"tenant_id": tenant.id, "created_at": tenant.created_at, "chaos": list(tenant.chaos),
            "seed_salt": tenant.seed_salt}


@router.post("/tenants")
async def create_tenant(request: Request):
    """Create (or re-provision) a tenant and return how to reach it.

    Body: {"tenant_id"?: str, "chaos"?: [flags], "seed_salt"?: str}. Returns ``tenant_id``, ``entry_url`` (open it to
    select the tenant in a browser; add ``?next=/path`` to land elsewhere), ``token`` and ``credentials``
    ``{mail, portal, erp, erp_api_key}``. An existing id is returned as is (200 instead of 201), or reset when
    ``chaos`` is given.
    """
    body = await _json_body(request)
    tenant_id = str(body["tenant_id"]) if body.get("tenant_id") else None
    existed = bool(tenant_id) and tenants.get_tenant(tenant_id) is not None
    try:
        tenant = await run_in_threadpool(tenants.provision_tenant, tenant_id, body.get("chaos"), body.get("seed_salt"))
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)
    return JSONResponse(tenant.provisioning_info(str(request.base_url)), status_code=200 if existed else 201)


@router.get("/tenants")
async def list_tenants() -> dict:
    return {"tenants": [{**_summary(tenants.default_tenant()), "default": True},
                        *(_summary(t) for t in tenants.list_tenants())]}


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: str, request: Request) -> dict:
    with scoped(tenant_id) as tenant:
        return tenant.provisioning_info(str(request.base_url))


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(tenant_id: str) -> dict:
    if tenant_id == tenants.DEFAULT:
        raise HTTPException(400, "The default tenant cannot be deleted")
    if not tenants.delete_tenant(tenant_id):
        raise HTTPException(404, f"Unknown tenant '{tenant_id}'")
    return {"status": "deleted", "tenant_id": tenant_id}


@router.post("/tenants/{tenant_id}/reset")
async def reset_tenant(tenant_id: str, request: Request):
    body = await _json_body(request)
    with scoped(tenant_id):
        return await run_in_threadpool(_reset, body)


@router.get("/tenants/{tenant_id}/state")
async def get_tenant_state(tenant_id: str) -> dict:
    with scoped(tenant_id):
        return tenants.tenant_state(tenant_id)


@router.get("/tenants/{tenant_id}/chaos")
async def get_tenant_chaos(tenant_id: str) -> dict:
    with scoped(tenant_id):
        return chaos.snapshot()


@router.post("/tenants/{tenant_id}/chaos")
async def set_tenant_chaos(tenant_id: str, request: Request):
    body = await _json_body(request)
    with scoped(tenant_id):
        return await run_in_threadpool(_apply_chaos, body)
