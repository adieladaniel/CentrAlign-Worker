"""Alembic environment (async). The URL comes from, in order: ``config.attributes["url"]``, DATABASE_URL in the settings.

``config.attributes["connection"]`` lets ``worker.db.migrate`` run migrations on a connection it already holds (inside
a running event loop); the ``alembic`` command line has none and starts its own loop."""
from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from worker.config import get_settings
from worker.db.models import Base
from worker.db.session import make_engine

config = context.config
target_metadata = Base.metadata


def _configure(**kwargs) -> None:
    context.configure(target_metadata=target_metadata, compare_type=True, compare_server_default=True, **kwargs)


def _run(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    url = config.attributes.get("url") or get_settings().DATABASE_URL
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    engine = make_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline (--sql) migrations are not supported: the schema uses PostgreSQL-specific features")

connection = config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    asyncio.run(_run_async())
