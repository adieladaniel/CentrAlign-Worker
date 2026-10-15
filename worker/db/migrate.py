"""Run Alembic migrations from async code (the CLI, ``scripts/bootstrap.py`` and the tests) and inspect the database."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import URL, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from worker.db.session import make_engine, normalize_url

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(connection: Connection | None = None) -> Config:
    """A programmatic Alembic config (no ini file needed); ``connection`` is the sync face of an async connection."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revision() -> str | None:
    """The newest revision in the migration scripts."""
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


async def upgrade(engine: AsyncEngine, revision: str = "head") -> None:
    """Apply migrations up to ``revision`` (default: all)."""
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_conn: command.upgrade(alembic_config(sync_conn), revision))


async def downgrade(engine: AsyncEngine, revision: str = "base") -> None:
    """Roll migrations back to ``revision`` (default: an empty schema)."""
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_conn: command.downgrade(alembic_config(sync_conn), revision))


async def current_revision(engine: AsyncEngine) -> str | None:
    """The revision the database is at, or None when it has not been migrated."""
    async with engine.connect() as connection:
        return await connection.run_sync(lambda sync_conn: MigrationContext.configure(sync_conn).get_current_revision())


async def server_version(engine: AsyncEngine) -> str:
    """The PostgreSQL server version string, e.g. ``18.6``."""
    async with engine.connect() as connection:
        return (await connection.execute(text("show server_version"))).scalar_one()


async def upgrade_url(url: str | URL, revision: str = "head") -> None:
    """``upgrade`` for a URL, with a short-lived engine."""
    engine = make_engine(normalize_url(url))
    try:
        await upgrade(engine, revision)
    finally:
        await engine.dispose()
