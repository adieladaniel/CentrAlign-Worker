"""Organisation administration API (``/api/admin/*``, admin role or higher; database mode only).

* members: list, invite or create (with a one-time initial password), change role, deactivate / reactivate
* settings: read and update the org policy (``worker.org_settings``)
* credentials: the org's vault entries as metadata only; secret fields are write-only
* environment: provision / reset the org's own simulated company
* audit: the most recent audit-log rows of the org

Rules for who may change whom: nobody can change their own role or deactivate themselves; an admin can neither assign
a role above admin nor touch an owner; only an owner can create or demote owners. Every change is audited and a
deactivation ends that user's sessions in this org.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from worker.auth import AuthService, OrgContext, rank_of, require_db_role
from worker.config import Settings
from worker.db.models import AuditLog, Membership, Org, User
from worker.db.seed import generate_password, hash_password
from worker.org_settings import effective, validate_update
from worker.provisioning import ProvisioningError, provision_org_environment, reset_org_environment
from worker.vault import Vault, VaultError, is_hidden

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


class NewMember(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field("", max_length=120)
    role: Literal["viewer", "approver", "operator", "admin", "owner"]
    password: str | None = Field(None, min_length=10, max_length=256, description="optional; generated when omitted")


class MemberPatch(BaseModel):
    role: Literal["viewer", "approver", "operator", "admin", "owner"] | None = None
    is_active: bool | None = None


class CredentialBody(BaseModel):
    kind: Literal["login", "api_key", "header"]
    fields: dict[str, str] = Field(default_factory=dict, description="values are write-only; blank = keep the stored one")
    description: str = Field("", max_length=300)
    url: str = Field("", max_length=300)
    host: str = Field("", max_length=253)


class ResetBody(BaseModel):
    chaos: list[str] = Field(default_factory=list)


def _sim_http_error(err: ProvisioningError) -> HTTPException:
    return HTTPException(status_code=422 if err.status == 422 else 502, detail=str(err))


def build_admin_router(
    *,
    service: AuthService,
    get_vault: Callable[[], Vault],
    settings: Settings,
    is_busy: Callable[[uuid.UUID], bool | Awaitable[bool]],
    sim_client: Callable[[], httpx.AsyncClient | None] = lambda: None,
) -> APIRouter:
    """The admin router. ``get_vault()`` opens the vault on first use. ``is_busy(org_id)`` says whether the org has a
    run in progress (an environment reset would corrupt it); ``sim_client()`` optionally supplies the HTTP client used
    to reach the simulator (tests)."""
    router = APIRouter(prefix="/api/admin", tags=["admin"])
    admin = require_db_role("admin")
    sm = service.sm

    async def org_of(ctx: OrgContext) -> Org:
        async with sm() as session:
            org = await session.get(Org, ctx.org_id)
        if org is None:  # cannot happen for a resolved context; defensive
            raise HTTPException(status_code=404, detail="Unknown organization")
        return org

    # ----------------------------------------------------------------- members

    def member_row(m: Membership, u: User) -> dict[str, Any]:
        return {"user_id": str(u.id), "email": u.email, "display_name": u.display_name, "role": m.role,
                "is_active": m.is_active and u.is_active, "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
                "joined_at": m.created_at.isoformat() if m.created_at else None}

    async def load_member(session, ctx: OrgContext, user_id: uuid.UUID) -> tuple[Membership, User]:
        row = (await session.execute(select(Membership, User).join(User, User.id == Membership.user_id).where(
            Membership.org_id == ctx.org_id, Membership.user_id == user_id))).first()
        if row is None:
            raise HTTPException(status_code=404, detail="No such member in this organization")
        return row[0], row[1]

    def check_assignable(ctx: OrgContext, role: str) -> None:
        if rank_of(role) > rank_of(ctx.role):
            raise HTTPException(status_code=403, detail=f"You cannot assign the {role} role (you are {ctx.role}).")

    @router.get("/members")
    async def list_members(ctx: OrgContext = Depends(admin)) -> list[dict]:
        async with sm() as session:
            rows = (await session.execute(select(Membership, User).join(User, User.id == Membership.user_id).where(
                Membership.org_id == ctx.org_id).order_by(User.display_name, User.email))).all()
        return sorted((member_row(m, u) for m, u in rows), key=lambda r: (-rank_of(r["role"]), r["email"]))

    @router.post("/members", status_code=201)
    async def add_member(body: NewMember, ctx: OrgContext = Depends(admin)) -> dict:
        email = body.email.strip().lower()
        if not _EMAIL_RE.match(email):
            raise HTTPException(status_code=422, detail="Enter a valid e-mail address.")
        check_assignable(ctx, body.role)
        generated = body.password is None
        async with sm() as session, session.begin():
            user = await session.scalar(select(User).where(func.lower(User.email) == email))
            created_user = user is None
            password = body.password or generate_password()
            if user is None:
                user = User(email=email, display_name=body.display_name.strip(),
                            password_hash=await asyncio.to_thread(hash_password, password))
                session.add(user)
                await session.flush()
            elif await session.get(Membership, (ctx.org_id, user.id)) is not None:
                raise HTTPException(status_code=409, detail="That person is already a member of this organization "
                                                            "(use the member's row to change the role or reactivate).")
            session.add(Membership(org_id=ctx.org_id, user_id=user.id, role=body.role))
            await service.audit("member.add", ctx=ctx, target_type="user", target_id=str(user.id), session=session,
                                details={"email": email, "role": body.role, "created_user": created_user})
        initial_password = password if created_user and generated else None  # shown once, only when we made it up
        return {"user_id": str(user.id), "email": email, "role": body.role, "created_user": created_user,
                "initial_password": initial_password}

    @router.patch("/members/{user_id}")
    async def patch_member(user_id: uuid.UUID, body: MemberPatch, ctx: OrgContext = Depends(admin)) -> dict:
        if body.role is None and body.is_active is None:
            raise HTTPException(status_code=422, detail="Send a role and/or is_active.")
        if user_id == ctx.user_id:
            raise HTTPException(status_code=400, detail="You cannot change your own role or deactivate yourself.")
        revoke = False
        async with sm() as session, session.begin():
            membership, user = await load_member(session, ctx, user_id)
            if rank_of(membership.role) > rank_of(ctx.role):
                raise HTTPException(status_code=403, detail=f"You cannot change a member whose role is {membership.role}.")
            changes: dict[str, Any] = {}
            if body.role is not None and body.role != membership.role:
                check_assignable(ctx, body.role)
                changes["role"] = [membership.role, body.role]
                membership.role = body.role
            if body.is_active is not None and body.is_active != membership.is_active:
                changes["is_active"] = [membership.is_active, body.is_active]
                membership.is_active = body.is_active
                revoke = not body.is_active
            if changes:
                action = "member.deactivate" if revoke else "member.reactivate" if "is_active" in changes else "member.role_change"
                await service.audit(action, ctx=ctx, target_type="user", target_id=str(user_id), session=session,
                                    details={"email": user.email, "changes": changes})
            row = member_row(membership, user)
        if revoke:
            await service.revoke_user_sessions(user_id, ctx.org_id)
        return row

    # ----------------------------------------------------------------- settings

    @router.get("/settings")
    async def get_org_settings(ctx: OrgContext = Depends(admin)) -> dict:
        org = await org_of(ctx)
        return {"settings": effective(org.settings, settings), "stored": sorted((org.settings or {}).keys())}

    @router.put("/settings")
    async def update_org_settings(updates: dict[str, Any], ctx: OrgContext = Depends(admin)) -> dict:
        async with sm() as session, session.begin():
            org = await session.scalar(select(Org).where(Org.id == ctx.org_id).with_for_update())
            before = effective(org.settings, settings)
            try:
                clean = validate_update(updates, before)
            except ValueError as err:
                raise HTTPException(status_code=422, detail=str(err)) from None
            org.settings = {**(org.settings or {}), **clean}  # a new dict: SQLAlchemy sees the change
            changed = {key: [before[key], value] for key, value in clean.items() if before[key] != value}
            if changed:
                await service.audit("org.settings_update", ctx=ctx, target_type="org", target_id=str(ctx.org_id),
                                    details={"changed": changed}, session=session)
            result = effective(org.settings, settings)
        return {"settings": result, "changed": sorted(changed)}

    # ----------------------------------------------------------------- credentials (metadata only)

    def public(info: dict) -> dict:
        return {"name": info["name"], "kind": info["kind"], "description": info["description"], "url": info["url"],
                "host": info["host"], "fields": info["fields"], "created_at": info["created_at"],
                "updated_at": info["updated_at"]}

    @router.get("/credentials")
    async def list_credentials(ctx: OrgContext = Depends(admin)) -> list[dict]:
        return [public(c) for c in await get_vault().list_public(str(ctx.org_id)) if not is_hidden(c["name"])]

    @router.put("/credentials/{name}")
    async def put_credential(name: str, body: CredentialBody, ctx: OrgContext = Depends(admin)) -> dict:
        owner, vault = str(ctx.org_id), get_vault()
        if is_hidden(name):
            raise HTTPException(status_code=422, detail=f"'{name}' is a reserved name.")
        if any(len(v) > 4096 for v in body.fields.values()):
            raise HTTPException(status_code=422, detail="A field value is too long.")
        try:
            existing = await vault.get(owner, name)
            kept = existing.fields if existing and existing.kind == body.kind else {}
            fields = {**kept, **{k: v for k, v in body.fields.items() if v != ""}}  # blank = keep the stored secret
            info = await vault.put(owner, name, body.kind, fields, description=body.description, url=body.url,
                                   host=body.host)
        except VaultError as err:
            raise HTTPException(status_code=422, detail=str(err)) from None
        await service.audit("credential.put", ctx=ctx, target_type="credential", target_id=name,
                            details={"kind": body.kind, "fields": info["fields"], "created": existing is None})
        return {**public(info), "created": existing is None}

    @router.delete("/credentials/{name}")
    async def delete_credential(name: str, ctx: OrgContext = Depends(admin)) -> dict:
        if is_hidden(name) or not await get_vault().delete(str(ctx.org_id), name):
            raise HTTPException(status_code=404, detail="No such credential")
        await service.audit("credential.delete", ctx=ctx, target_type="credential", target_id=name)
        return {"status": "deleted", "name": name}

    # ----------------------------------------------------------------- environment

    @router.get("/environment")
    async def environment(ctx: OrgContext = Depends(admin)) -> dict:
        org = await org_of(ctx)
        return {"provisioned": bool(org.sim_tenant_id), "tenant_id": org.sim_tenant_id, "sim_base_url": settings.SIM_BASE_URL}

    @router.post("/environment/provision")
    async def provision(ctx: OrgContext = Depends(admin)) -> dict:
        org = await org_of(ctx)
        try:
            result = await provision_org_environment(org, sessionmaker=sm, vault=get_vault(), settings=settings,
                                                     client=sim_client())
        except ProvisioningError as err:
            raise _sim_http_error(err) from None
        await service.audit("environment.provision", ctx=ctx, target_type="org", target_id=str(ctx.org_id),
                            details={"tenant_id": result["tenant_id"], "created": result["created"]})
        return result

    @router.post("/environment/reset")
    async def reset(body: ResetBody, ctx: OrgContext = Depends(admin)) -> dict:
        org = await org_of(ctx)
        if not org.sim_tenant_id:
            raise HTTPException(status_code=409, detail="This organization has no environment yet: provision it first.")
        busy = is_busy(ctx.org_id)
        if await busy if inspect.isawaitable(busy) else busy:
            raise HTTPException(status_code=409, detail="A run is active; resetting the environment now would corrupt it.")
        try:
            result = await reset_org_environment(org.sim_tenant_id, settings, body.chaos, sim_client())
        except ProvisioningError as err:
            raise _sim_http_error(err) from None
        await service.audit("environment.reset", ctx=ctx, target_type="org", target_id=str(ctx.org_id),
                            details={"tenant_id": org.sim_tenant_id, "chaos": body.chaos})
        return result

    # ----------------------------------------------------------------- audit

    @router.get("/audit")
    async def audit_log(limit: int = Query(50, ge=1, le=500), ctx: OrgContext = Depends(admin)) -> list[dict]:
        async with sm() as session:
            rows = (await session.execute(
                select(AuditLog, User.email).join(User, User.id == AuditLog.user_id, isouter=True)
                .where(AuditLog.org_id == ctx.org_id).order_by(AuditLog.id.desc()).limit(limit))).all()
        return [{"id": r.id, "ts": r.ts.isoformat(), "action": r.action, "user": email, "target_type": r.target_type,
                 "target_id": r.target_id, "details": r.details} for r, email in rows]

    return router
