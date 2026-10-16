"""Async engine and sessions for PostgreSQL (SQLAlchemy 2.0 + asyncpg).

* ``make_engine`` / ``make_sessionmaker`` build an engine from a URL (tests and scripts use these directly).
* ``get_engine`` / ``get_sessionmaker`` / ``get_session`` use one process-wide engine built from ``DATABASE_URL``.
  An asyncpg pool belongs to the event loop that first used it, so a process must call ``dispose_engine`` before it
  starts another loop (the server does so on shutdown).
* ``mask_url`` hides the password; use it for every log line and message that mentions the database.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from worker.config import Settings, get_settings

ASYNC_DRIVER = "postgresql+asyncpg"


class DatabaseNotConfigured(RuntimeError):
    """``DATABASE_URL`` is empty: the worker is running in file mode."""


def normalize_url(url: str | URL) -> URL:
    """Parse ``url`` and make sure it names the asyncpg driver (``postgres://`` and ``postgresql://`` are accepted)."""
    parsed = make_url(url) if isinstance(url, str) else url
    if parsed.get_backend_name() not in ("postgresql", "postgres"):
        raise ValueError(f"DATABASE_URL must be a PostgreSQL URL, got scheme '{parsed.drivername}'")
    return parsed.set(drivername=ASYNC_DRIVER)


def mask_url(url: str | URL) -> str:
    """The URL with the password replaced by ``***``: safe to log or print."""
    return normalize_url(url).render_as_string(hide_password=True)


def _strip_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_strip_nul(k): _strip_nul(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strip_nul(v) for v in value]
    return value


def _json_dumps(value: Any) -> str:
    """JSON for JSONB columns: tolerant of non-JSON values (like the file store) and of NUL, which PostgreSQL rejects."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    return json.dumps(_strip_nul(value), ensure_ascii=False, default=str) if "\\u0000" in text else text


def make_engine(url: str | URL, **options: Any) -> AsyncEngine:
    """An async engine for ``url``. ``options`` are passed to ``create_async_engine`` (pool_size, max_overflow, ...)."""
    return create_async_engine(normalize_url(url), json_serializer=_json_dumps, pool_pre_ping=True, **options)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions that do not expire attributes on commit (rows stay readable after the transaction ends)."""
    return async_sessionmaker(engine, expire_on_commit=False)


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def database_configured(settings: Settings | None = None) -> bool:
    """Is ``DATABASE_URL`` set?"""
    return bool((settings or get_settings()).DATABASE_URL.strip())


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """The process-wide engine (created on first use from ``DATABASE_URL`` and the ``DB_*`` pool settings)."""
    global _engine, _sessionmaker
    if _engine is None:
        settings = settings or get_settings()
        if not database_configured(settings):
            raise DatabaseNotConfigured("DATABASE_URL is not set")
        _engine = make_engine(settings.DATABASE_URL.strip(), pool_size=settings.DB_POOL_SIZE,
                              max_overflow=settings.DB_MAX_OVERFLOW, pool_timeout=settings.DB_POOL_TIMEOUT_S)
        _sessionmaker = make_sessionmaker(_engine)
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """The process-wide sessionmaker (see ``get_engine``)."""
    get_engine(settings)
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """A session in a transaction: committed when the block ends normally, rolled back when it raises."""
    async with get_sessionmaker()() as session, session.begin():
        yield session


async def dispose_engine() -> None:
    """Close the pooled connections of the process-wide engine (a no-op when it was never created)."""
    global _engine, _sessionmaker
    engine, _engine, _sessionmaker = _engine, None, None
    if engine is not None:
        await engine.dispose()
