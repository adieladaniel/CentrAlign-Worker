"""Set up the database from scratch, idempotently: create it if missing, migrate, seed the demo orgs and users, and
(``--provision-sim``) give each demo org its own simulated company.

``python scripts/bootstrap.py`` is the command line; ``bootstrap(url, ...)`` is the same thing as a function (the tests
call it). Creating and dropping databases are done over a plain asyncpg connection to the ``postgres`` maintenance
database, because ``CREATE DATABASE`` cannot run inside a transaction. Only the databases in ``DROPPABLE`` can be
dropped (the two named ones, plus the throwaway ``oneforall_test_*`` and ``oneforall_load*`` databases of parallel
test runs and ``scripts/loadtest.py``), so a mistyped URL cannot destroy anything else.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import asyncpg
import httpx
from sqlalchemy import URL, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.config import Settings, get_settings
from worker.db import migrate
from worker.db.models import Org
from worker.db.seed import DEMO_ORGS, SeedResult, generate_password, seed_demo
from worker.db.session import make_engine, make_sessionmaker, mask_url, normalize_url
from worker.provisioning import ProvisioningError, provision_org_environment
from worker.vault_pg import open_pg_vault

DROPPABLE = frozenset({"oneforall", "oneforall_test"})
THROWAWAY_PREFIXES = ("oneforall_test_", "oneforall_load")  # parallel test runs; the load test's own database


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def _admin_connection(url: URL) -> asyncpg.Connection:
    return await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username, password=url.password,
                                 database="postgres")


async def database_exists(url: str | URL) -> bool:
    url = normalize_url(url)
    conn = await _admin_connection(url)
    try:
        return bool(await conn.fetchval("select 1 from pg_database where datname = $1", url.database))
    finally:
        await conn.close()


async def create_database(url: str | URL) -> bool:
    """Create the database named by ``url`` if it does not exist. Returns True when it was created."""
    url = normalize_url(url)
    conn = await _admin_connection(url)
    try:
        if await conn.fetchval("select 1 from pg_database where datname = $1", url.database):
            return False
        await conn.execute(f"create database {_quote(url.database)}")
        return True
    finally:
        await conn.close()


async def drop_database(url: str | URL) -> bool:
    """Drop the database named by ``url`` (open connections are terminated). Only ``oneforall``, ``oneforall_test`` and
    the throwaway ``oneforall_test_*`` / ``oneforall_load*`` databases are allowed. Returns True when something was dropped."""
    url = normalize_url(url)
    if url.database not in DROPPABLE and not url.database.startswith(THROWAWAY_PREFIXES):
        raise ValueError(f"refusing to drop database '{url.database}': only {', '.join(sorted(DROPPABLE))} "
                         f"and {' / '.join(p + '*' for p in THROWAWAY_PREFIXES)} may be dropped")
    conn = await _admin_connection(url)
    try:
        if not await conn.fetchval("select 1 from pg_database where datname = $1", url.database):
            return False
        await conn.execute(f"drop database {_quote(url.database)} with (force)")
        return True
    finally:
        await conn.close()


@dataclass
class BootstrapResult:
    """What ``bootstrap`` did. ``password`` is the demo password when users were created in this call, else None."""

    created_database: bool
    reset: bool
    revision: str | None
    seed: SeedResult
    password: str | None
    provisioned: dict[str, str] = field(default_factory=dict)  # demo org slug -> simulated-company tenant id
    provision_note: str | None = None  # why provisioning was skipped (e.g. the simulator is not running)


async def provision_demo_environments(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, client: httpx.AsyncClient | None = None
) -> tuple[dict[str, str], str | None]:
    """Provision the simulated company of every demo org (idempotent). Returns ``(slug -> tenant id, note)``; when the
    simulator is unreachable it stops with a note instead of failing, so ``bootstrap`` still succeeds."""
    vault = open_pg_vault(sessionmaker, settings)
    async with sessionmaker() as session:
        orgs = (await session.scalars(select(Org).where(Org.slug.in_([slug for slug, _, _ in DEMO_ORGS])).order_by(Org.slug))).all()
    done: dict[str, str] = {}
    for org in orgs:
        try:
            result = await provision_org_environment(org, sessionmaker=sessionmaker, vault=vault, settings=settings, client=client)
        except ProvisioningError as err:
            return done, f"environments not provisioned: {err}. Start the simulator and run bootstrap again with --provision-sim."
        done[org.slug] = result["tenant_id"]
    return done, None


async def bootstrap(url: str | URL, *, demo_password: str | None = None, reset: bool = False, provision_sim: bool = False,
                    settings: Settings | None = None, sim_client: httpx.AsyncClient | None = None) -> BootstrapResult:
    """Create the database if missing (after dropping it first when ``reset``), apply all migrations and seed the demo
    orgs. Safe to run repeatedly: nothing that exists is changed. The caller confirms ``reset`` with the user.
    ``provision_sim`` also creates each demo org's own simulated company (needs the simulator running)."""
    url = normalize_url(url)
    dropped = await drop_database(url) if reset else False
    created = await create_database(url)
    engine = make_engine(url)
    try:
        await migrate.upgrade(engine)
        password = demo_password or generate_password()
        sessionmaker = make_sessionmaker(engine)
        seed = await seed_demo(sessionmaker, password)
        revision = await migrate.current_revision(engine)
        provisioned, note = ({}, None)
        if provision_sim:
            provisioned, note = await provision_demo_environments(sessionmaker, settings or get_settings(), sim_client)
    finally:
        await engine.dispose()
    return BootstrapResult(created_database=created, reset=dropped, revision=revision, seed=seed,
                           password=password if seed.users_created else None, provisioned=provisioned,
                           provision_note=note)


def describe(result: BootstrapResult, url: str | URL) -> str:
    """Human-readable summary (never contains the database password; the demo password is shown once, if new)."""
    lines = [f"database:  {mask_url(url)} ({'created' if result.created_database else 'already existed'}"
             f"{', after reset' if result.reset else ''})", f"revision:  {result.revision}"]
    seed = result.seed
    lines.append(f"orgs:      {', '.join(seed.orgs_created) or 'none new'}; users created: {len(seed.users_created)}; "
                 f"memberships created: {seed.memberships_created}")
    if result.password:
        lines += ["", "Demo users (all share one password; it is shown only now):"]
        lines += [f"  {email:<22} {role}" for email, role in seed.users_created.items()]
        lines += [f"  password: {result.password}"]
    else:
        lines.append("demo users already existed: their passwords were not changed.")
    if result.provisioned:
        lines.append("environments: " + ", ".join(f"{slug} -> {tenant}" for slug, tenant in result.provisioned.items()))
    if result.provision_note:
        lines.append(result.provision_note)
    return "\n".join(lines)
