"""Readiness: can this process do its job right now? Used by ``GET /readyz`` and logged by a worker at start.

Liveness (``/healthz``) only says the process answers. Readiness checks what it depends on:

* **database** (database mode): a connection works (``select 1``);
* **migrations**: the schema is at the Alembic head this code needs;
* **sim**: the simulated company answers its ``/healthz`` (skipped when ``READINESS_CHECK_SIM`` is off).

Failures are reported by kind only (``unreachable (ConnectionRefusedError)``), never with URLs or messages that could
carry credentials.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.config import Settings
from worker.db import migrate

log = logging.getLogger("worker.health")

CHECK_TIMEOUT_S = 3.0


async def check_database(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    checks: dict[str, str] = {}
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            async with sessionmaker() as session:
                await session.execute(text("select 1"))
                checks["database"] = "ok"
                try:
                    current = (await session.execute(text("select version_num from alembic_version"))).scalar()
                except Exception:  # noqa: BLE001 - no alembic table: never migrated
                    current = None
    except Exception as exc:  # noqa: BLE001 - TimeoutError, OSError, asyncpg and SQLAlchemy errors
        return {"database": f"unreachable ({type(exc).__name__})", "migrations": "unknown"}
    head = migrate.head_revision()
    checks["migrations"] = "ok" if current == head else f"schema at {current or 'none'}, code needs {head}"
    return checks


async def check_sim(settings: Settings, client: httpx.AsyncClient | None = None) -> str:
    url = settings.SIM_BASE_URL.rstrip("/") + "/healthz"
    try:
        if client is not None:
            resp = await asyncio.wait_for(client.get(url), CHECK_TIMEOUT_S)
        else:
            async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_S) as own:
                resp = await own.get(url)
    except Exception as exc:  # noqa: BLE001
        return f"unreachable ({type(exc).__name__})"
    return "ok" if resp.status_code == 200 else f"status {resp.status_code}"


async def check_ready(settings: Settings, sessionmaker: async_sessionmaker[AsyncSession] | None, *,
                      sim_client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """``{"status": "ready" | "not_ready", "checks": {name: "ok" | reason}}``."""
    checks: dict[str, str] = {}
    if sessionmaker is not None:
        checks.update(await check_database(sessionmaker))
    if settings.READINESS_CHECK_SIM and settings.SIM_BASE_URL:
        checks["sim"] = await check_sim(settings, sim_client)
    ready = all(value == "ok" for value in checks.values())
    return {"status": "ready" if ready else "not_ready", "checks": checks}
