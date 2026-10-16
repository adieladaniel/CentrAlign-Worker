"""Shared pytest fixtures.

PostgreSQL tests use a throwaway database, ``oneforall_test``, on the server named by ``DATABASE_URL`` (from the
environment or ``.env``): it is dropped and recreated, migrated once per pytest session, emptied between tests and
dropped at the end. Without a reachable server the tests that need it are skipped, never failed.
"""
from __future__ import annotations

import inspect
import os

# The developer's .env may set DATABASE_URL, which switches the server to multi-user (database) auth. Tests that build
# the app default to the v1 file mode; the multi-user tests ask for AUTH_MODE="db" explicitly.
os.environ.setdefault("AUTH_MODE", "file")

import pytest
import pytest_asyncio
from sqlalchemy import text

from worker.config import Settings
from worker.db import migrate
from worker.db.bootstrap import create_database, drop_database
from worker.db.models import Base, Org, User
from worker.db.session import make_engine, make_sessionmaker, normalize_url
from worker.store import FileRunStore
from worker.store_pg import PgRunStore
from worker.tools.browser_pool import BrowserPool

# ONEFORALL_TEST_DB=oneforall_test_<suffix> lets two pytest runs (e.g. two developers or agents) use separate databases.
TEST_DATABASE = os.environ.get("ONEFORALL_TEST_DB", "oneforall_test")
# Fixtures that bind a test to the session event loop: Playwright objects and asyncpg pools belong to the loop that
# made them (see ``pytest_collection_modifyitems``).
SESSION_LOOP_FIXTURES = ("browser_pool", "pg_engine", "store_kind")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser_pool():
    """One Chromium for every browser test (launched lazily on first use), shut down when the session ends so no
    browser or driver process outlives pytest. Tests using it run on the session event loop
    (see ``pytest_collection_modifyitems``) because Playwright objects belong to the loop that made them."""
    pool = BrowserPool(max_contexts=8, acquire_timeout_s=30)
    yield pool
    await pool.shutdown()


def pytest_collection_modifyitems(items):
    """Async tests that use the shared browser pool or the PostgreSQL engine run on the session event loop."""
    for item in items:
        if any(name in item.fixturenames for name in SESSION_LOOP_FIXTURES) and inspect.iscoroutinefunction(
                getattr(item, "function", None)):
            item.add_marker(pytest.mark.asyncio(loop_scope="session"), append=False)


# ---------------------------------------------------------------- PostgreSQL


def database_url_for_tests():
    """URL of the test database (same server and credentials as ``DATABASE_URL``), or None when none is configured."""
    url = Settings().DATABASE_URL.strip()
    return normalize_url(url).set(database=TEST_DATABASE) if url else None


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pg_engine():
    """An engine on a fresh, migrated ``oneforall_test`` database; None when PostgreSQL is not available."""
    url = database_url_for_tests()
    if url is None:
        yield None
        return
    try:
        await drop_database(url)
        await create_database(url)
    except Exception:  # noqa: BLE001 - no server, wrong credentials, ...: the DB tests are skipped
        yield None
        return
    engine = make_engine(url, pool_size=5, max_overflow=30)
    try:
        await migrate.upgrade(engine)  # a failing migration is a real error, not a reason to skip
        yield engine
    finally:
        await engine.dispose()
        await drop_database(url)


@pytest_asyncio.fixture(loop_scope="session")
async def maybe_pg_sessionmaker(pg_engine):
    """A sessionmaker on an emptied test database, or None when PostgreSQL is not available."""
    if pg_engine is None:
        yield None
        return
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with pg_engine.begin() as conn:
        await conn.execute(text(f"truncate {tables} restart identity cascade"))
    yield make_sessionmaker(pg_engine)


@pytest.fixture
def pg_sessionmaker(maybe_pg_sessionmaker):
    """Like ``maybe_pg_sessionmaker``, but skips the test when there is no database."""
    if maybe_pg_sessionmaker is None:
        pytest.skip("PostgreSQL is not available (set DATABASE_URL, see .env.example)")
    return maybe_pg_sessionmaker


@pytest.fixture
def make_org(pg_sessionmaker):
    """``await make_org("acme")`` -> ``(org_id, user_id)``: an org with one user, for the stores that need them."""

    async def make(slug: str = "acme") -> tuple:
        async with pg_sessionmaker() as session, session.begin():
            org, user = Org(slug=slug, name=slug.title()), User(email=f"u@{slug}.test", password_hash="x")
            session.add_all([org, user])
        return org.id, user.id

    return make


@pytest.fixture(params=["file", "pg"])
def store_kind(request):
    return request.param


@pytest_asyncio.fixture(loop_scope="session")
async def store_factory(store_kind, maybe_pg_sessionmaker, tmp_path):
    """``store_factory()`` -> a new ``RunStore`` over the same backing storage every time (like another process),
    for FileRunStore and, when a database is available, PgRunStore (one org)."""
    root = tmp_path / "store"
    if store_kind == "file":
        return lambda: FileRunStore(root)
    if maybe_pg_sessionmaker is None:
        pytest.skip("PostgreSQL is not available (set DATABASE_URL, see .env.example)")
    async with maybe_pg_sessionmaker() as session, session.begin():
        org, user = Org(slug="contract", name="Contract"), User(email="u@contract.test", password_hash="x")
        session.add_all([org, user])
    return lambda: PgRunStore(maybe_pg_sessionmaker, org.id, user.id, artifacts_root=root)


@pytest.fixture
def pg_required_engine(pg_engine):
    """The test database's engine; skips the test when PostgreSQL is not available."""
    if pg_engine is None:
        pytest.skip("PostgreSQL is not available (set DATABASE_URL, see .env.example)")
    return pg_engine


# ---------------------------------------------------------------- multi-user server (database mode)


@pytest_asyncio.fixture(loop_scope="session")
async def world(pg_sessionmaker):
    """Two seeded orgs (acme, globex) with users in every role; see ``tests/dbapp.py``."""
    from dbapp import seed_world, shutdown_all

    yield await seed_world(pg_sessionmaker)
    await shutdown_all()


@pytest.fixture
def db_app(world, tmp_path):
    """``db_app(**settings)`` -> ``(app, planner)``: the server in database mode over the seeded test database."""
    from dbapp import make_app

    def factory(**settings_overrides):
        return make_app(world, tmp_path, **settings_overrides)

    return factory
