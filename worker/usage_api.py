"""Usage endpoints (database mode): ``GET /api/admin/usage`` (admin and up) and ``GET /api/usage/me`` (any member).

Both are scoped to the caller's active organisation; the numbers come from the ``usage_events`` ledger
(``worker/usage.py``)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.auth import OrgContext, require_db_role
from worker.usage import usage_report, user_usage_today

MAX_DAYS = 90


def build_usage_router(*, sessionmaker: async_sessionmaker[AsyncSession]) -> APIRouter:
    router = APIRouter(tags=["usage"])
    admin = require_db_role("admin")
    member = require_db_role("viewer")

    @router.get("/api/admin/usage")
    async def org_usage(days: int = Query(7, ge=1, le=MAX_DAYS), ctx: OrgContext = Depends(admin)) -> dict:
        """Per-day totals, per-user and per-model breakdowns of the last ``days`` days, and today against the org's budget."""
        return await usage_report(sessionmaker, ctx.org_id, days=days)

    @router.get("/api/usage/me")
    async def my_usage(ctx: OrgContext = Depends(member)) -> dict:
        """What the caller's own runs used today."""
        return await user_usage_today(sessionmaker, ctx.org_id, ctx.user_id)

    return router
