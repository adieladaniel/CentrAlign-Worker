"""Organization memory API (``/api/memory``; database mode only).

* ``GET /api/memory?q=&scope=``: list or search the org's notes (any member). Searching here is not a "use".
* ``PATCH /api/memory/{id}``: edit content, scope or expiry (admin).
* ``DELETE /api/memory/{id}``: delete a note (admin).
* ``POST /api/memory/expire``: expire the notes unused for the org's ``memory_ttl_days`` now (admin).

Every change is audited (``memory.update`` / ``memory.delete`` / ``memory.expire``). Everything is scoped to the caller's
active organization; another org's note id is indistinguishable from an unknown one (404). Notes are *written* only by
verified runs (``worker/memory_store.py``), never through this API.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.agent.state import truncate
from worker.auth import AuthService, OrgContext, require_db_role
from worker.config import Settings
from worker.db.models import Org
from worker.memory_store import MAX_CONTENT_CHARS, MAX_SCOPE_CHARS, OrgMemoryStore
from worker.org_settings import effective

AUDIT_TEXT_LIMIT = 200


class MemoryPatch(BaseModel):
    content: str | None = Field(None, max_length=MAX_CONTENT_CHARS * 2)
    scope: str | None = Field(None, max_length=MAX_SCOPE_CHARS * 2)
    expires_at: datetime | None = Field(None, description="send null to clear the expiry")


def build_memory_router(*, service: AuthService, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/memory", tags=["memory"])
    viewer, admin = require_db_role("viewer"), require_db_role("admin")

    def store_for(ctx: OrgContext) -> OrgMemoryStore:
        return OrgMemoryStore(sessionmaker, ctx.org_id)

    async def org_cfg(ctx: OrgContext) -> dict:
        async with sessionmaker() as session:
            org = await session.get(Org, ctx.org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Unknown organization")
        return effective(org.settings, settings)

    @router.get("")
    async def list_memory(q: str = Query("", max_length=200), scope: str = Query("", max_length=80),
                          limit: int = Query(200, ge=1, le=500), ctx: OrgContext = Depends(viewer)) -> dict:
        cfg = await org_cfg(ctx)
        items = await store_for(ctx).list(q.strip() or None, scope.strip() or None, limit)
        return {"items": items, "can_edit": ctx.has_role("admin"),
                "enabled": cfg["memory_recall_limit"] > 0, "recall_limit": cfg["memory_recall_limit"],
                "ttl_days": cfg["memory_ttl_days"]}

    @router.patch("/{memory_id}")
    async def update_memory(memory_id: str, body: MemoryPatch, ctx: OrgContext = Depends(admin)) -> dict:
        fields = {k: getattr(body, k) for k in ("content", "scope", "expires_at") if k in body.model_fields_set}
        if "expires_at" in fields and fields["expires_at"] is not None and fields["expires_at"].tzinfo is None:
            fields["expires_at"] = fields["expires_at"].replace(tzinfo=timezone.utc)
        try:
            changed = await store_for(ctx).update(memory_id, **fields)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from None
        if changed is None:
            raise HTTPException(status_code=404, detail="No such memory")
        before, after = changed
        diff = {k: [before[k], after[k]] for k in ("content", "scope", "expires_at") if before[k] != after[k]}
        await service.audit("memory.update", ctx=ctx, target_type="memory", target_id=before["id"],
                            details={"key": before["key"], "changed": {k: [_audit_text(v[0]), _audit_text(v[1])] for k, v in diff.items()}})
        return after

    @router.delete("/{memory_id}")
    async def delete_memory(memory_id: str, ctx: OrgContext = Depends(admin)) -> dict:
        gone = await store_for(ctx).delete(memory_id)
        if gone is None:
            raise HTTPException(status_code=404, detail="No such memory")
        await service.audit("memory.delete", ctx=ctx, target_type="memory", target_id=gone["id"],
                            details={"key": gone["key"], "scope": gone["scope"], "content": _audit_text(gone["content"]),
                                     "source_run_id": gone["source_run_id"]})
        return {"status": "deleted", "key": gone["key"]}

    @router.post("/expire")
    async def expire_memory(ctx: OrgContext = Depends(admin)) -> dict:
        ttl = (await org_cfg(ctx))["memory_ttl_days"]
        keys = await store_for(ctx).expire_stale(ttl)
        await service.audit("memory.expire", ctx=ctx, target_type="org", target_id=str(ctx.org_id),
                            details={"ttl_days": ttl, "expired": len(keys), "keys": keys[:50]})
        return {"expired": len(keys), "keys": keys, "ttl_days": ttl}

    return router


def _audit_text(value: object) -> object:
    return truncate(value, AUDIT_TEXT_LIMIT) if isinstance(value, str) else value
