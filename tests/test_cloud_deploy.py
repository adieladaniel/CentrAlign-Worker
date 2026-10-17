"""Cloud deployment pieces: managed-PostgreSQL (Neon) URLs, the start-up environment resync, the role-based container
supervisor's plan, and the bootstrap script's password from the environment.

Nothing here talks to a real cloud service. The resync tests need the test database (``DATABASE_URL``, skipped without one)
and run the simulated company in-process on a temporary data dir.
"""
from __future__ import annotations

import importlib.util
import logging
import re
import ssl
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from asyncpg import connect_utils
from sqlalchemy import select

from company_sim import chaos, config, tenants
from worker import provisioning
from worker.config import Settings
from worker.db import bootstrap as bootstrap_module
from worker.db import session as db_session
from worker.db.models import Org
from worker.db.session import make_engine, mask_url, normalize_url, warn_if_pooled
from worker.pg_listen import dsn_for
from worker.provisioning import SIM_ENTRY, resync_org_environments
from worker.vault import Vault, generate_key
from worker.vault_pg import PgVaultBackend

NEON = ("postgresql://app_user:p%40ss%2Fword@ep-quiet-sun-123456.ap-southeast-1.aws.neon.tech/neondb"
        "?sslmode=require&channel_binding=require")


def load_start() -> object:
    """deploy/start.py as a module (deploy/ is not a package)."""
    spec = importlib.util.spec_from_file_location("ofa_start", Path(__file__).resolve().parent.parent / "deploy" / "start.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- managed PostgreSQL URLs (no database needed)


def test_normalize_url_translates_sslmode_and_drops_channel_binding():
    url = normalize_url(NEON)
    assert url.drivername == "postgresql+asyncpg"
    assert dict(url.query) == {"ssl": "require"}
    assert url.host == "ep-quiet-sun-123456.ap-southeast-1.aws.neon.tech" and url.password == "p@ss/word"

    kept = normalize_url("postgres://u:p@h/db?sslmode=verify-full&application_name=ofa&channel_binding=prefer")
    assert dict(kept.query) == {"ssl": "verify-full", "application_name": "ofa"}

    assert dict(normalize_url("postgresql://u:p@h/db?ssl=require&sslmode=disable").query) == {"ssl": "require"}  # explicit ssl wins
    assert dict(normalize_url("postgresql://u:p@h/db").query) == {}
    assert normalize_url(normalize_url(NEON)) == normalize_url(NEON)  # idempotent


def test_mask_url_of_a_neon_url_hides_the_password():
    masked = mask_url(NEON)
    assert "p%40ss" not in masked and "p@ss" not in masked and ":***@" in masked and "channel_binding" not in masked


def test_engine_from_a_neon_url_hands_asyncpg_only_arguments_it_accepts():
    engine = make_engine(NEON)
    _, kwargs = engine.dialect.create_connect_args(engine.url)
    assert kwargs["ssl"] == "require" and "sslmode" not in kwargs and "channel_binding" not in kwargs
    assert kwargs["user"] == "app_user" and kwargs["password"] == "p@ss/word" and kwargs["database"] == "neondb"


def parse_with_asyncpg(dsn: str):
    """asyncpg's own DSN parser, the one ``asyncpg.connect(dsn)`` runs (no connection is made)."""
    none = dict(host=None, port=None, user=None, password=None, passfile=None, database=None, ssl=None, service=None,
                servicefile=None, direct_tls=None, server_settings=None, target_session_attrs=None, krbsrvname=None, gsslib=None)
    addrs, params = connect_utils._parse_connect_dsn_and_args(dsn=dsn, **none)
    return addrs, params


def test_the_listener_dsn_of_a_neon_url_parses_with_asyncpg():
    dsn = dsn_for(NEON)
    assert dsn.startswith("postgresql://") and "sslmode=require" in dsn and "channel_binding" not in dsn
    addrs, params = parse_with_asyncpg(dsn)
    assert addrs == [("ep-quiet-sun-123456.ap-southeast-1.aws.neon.tech", 5432)]
    assert params.user == "app_user" and params.password == "p@ss/word" and params.database == "neondb"
    assert isinstance(params.ssl, ssl.SSLContext)  # TLS is on
    assert not params.server_settings  # 'ssl' must not leak into the server settings (the server would reject it)


def test_the_listener_dsn_without_ssl_has_no_ssl_parameter():
    dsn = dsn_for("postgresql+asyncpg://postgres:pw@localhost:5432/oneforall")
    assert "ssl" not in dsn
    _, params = parse_with_asyncpg(dsn)
    assert params.database == "oneforall" and not params.server_settings


def test_bootstrap_connects_to_the_maintenance_database_with_the_same_tls_setting():
    args = bootstrap_module._connect_args(normalize_url(NEON), "postgres")
    assert args["ssl"] == "require" and args["database"] == "postgres" and args["host"].endswith("neon.tech")
    assert args["password"] == "p@ss/word" and args["port"] == 5432
    assert "ssl" not in bootstrap_module._connect_args(normalize_url("postgresql://u:p@localhost/x"), "postgres")


def test_a_pooler_host_logs_one_masked_warning(caplog):
    db_session._warned_pooled.clear()
    pooled = "postgresql://u:s3cretpw@ep-quiet-sun-123456-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
    with caplog.at_level(logging.WARNING, logger="worker.db"):
        assert warn_if_pooled(pooled) is True
        assert warn_if_pooled(pooled) is True  # still reports pooled, but logs once per host
        make_engine(pooled)
        assert warn_if_pooled(NEON) is False
    warnings = [r for r in caplog.records if r.name == "worker.db"]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "LISTEN/NOTIFY" in text and "-pooler" in text and "s3cretpw" not in text and ":***@" in text
    db_session._warned_pooled.clear()


# ---------------------------------------------------------------- environment resync (needs the test database)


@pytest.fixture
def sim_dirs(tmp_path, monkeypatch):
    """Point the in-process simulator at a data dir; call ``fresh()`` for a new, empty one (= a new container's disk)."""
    monkeypatch.delenv("SIM_ADMIN_TOKEN", raising=False)
    counter = iter(range(1, 100))

    def fresh() -> Path:
        data = tmp_path / f"simdata{next(counter)}"
        monkeypatch.setattr(config, "DATA_DIR", data)
        monkeypatch.setattr(config, "DB_PATH", data / "sim.db")
        monkeypatch.setattr(config, "PDF_DIR", data / "pdfs")
        monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path / "simworkspace")
        return data

    fresh()
    chaos.set_flags([])
    yield fresh
    chaos.set_flags([])


@pytest_asyncio.fixture
async def sim_client(sim_dirs):
    from company_sim.app import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sim.test") as client:
        yield client


@pytest.fixture
def settings():
    return Settings(SIM_BASE_URL="http://sim.test", AUTH_MODE="db", LLM_PROVIDER="fake")


async def make_orgs(sm, *slugs: str) -> dict[str, Org]:
    async with sm() as session, session.begin():
        orgs = {slug: Org(slug=slug, name=slug.title()) for slug in slugs}
        session.add_all(orgs.values())
    return orgs


async def tenant_of(sm, slug: str) -> str | None:
    async with sm() as session:
        return await session.scalar(select(Org.sim_tenant_id).where(Org.slug == slug))


async def test_resync_recreates_the_tenants_a_fresh_container_lost(pg_sessionmaker, sim_client, sim_dirs, settings):
    sm = pg_sessionmaker
    orgs = await make_orgs(sm, "acme", "globex", "initech")  # initech never had an environment
    vault = Vault(PgVaultBackend(sm), generate_key())

    first = await resync_org_environments(sm, vault, settings, sim_client)
    assert first == {"orgs": 3, "created": 3, "existing": 0, "failed": 0, "unreachable": False}
    old = {slug: tenants.get_tenant(f"org-{slug}").token for slug in orgs}
    for slug, org in orgs.items():
        assert await tenant_of(sm, slug) == f"org-{slug}"
        assert (await vault.get(str(org.id), SIM_ENTRY)).fields["value"] == old[slug]

    again = await resync_org_environments(sm, vault, settings, sim_client)  # nothing lost: nothing changes
    assert again["created"] == 0 and again["existing"] == 3 and again["failed"] == 0
    assert tenants.get_tenant("org-acme").token == old["acme"]

    sim_dirs()  # the container restarts: the simulator's SQLite data is gone, PostgreSQL and the vault are not
    assert tenants.get_tenant("org-acme") is None
    assert (await vault.get(str(orgs["acme"].id), SIM_ENTRY)).fields["value"] == old["acme"]  # stale token in the vault

    healed = await resync_org_environments(sm, vault, settings, sim_client)
    assert healed == {"orgs": 3, "created": 3, "existing": 0, "failed": 0, "unreachable": False}
    for slug, org in orgs.items():
        tenant = tenants.get_tenant(f"org-{slug}")
        assert tenant is not None and tenant.token != old[slug]
        assert (await vault.get(str(org.id), SIM_ENTRY)).fields["value"] == tenant.token  # the vault matches the new tenant
        assert (await vault.get(str(org.id), "erp")).fields == tenant.credentials["erp"]
        assert (await vault.get(str(org.id), "erp_api")).fields["api_key"] == tenant.api_key
        assert await tenant_of(sm, slug) == tenant.id


async def test_resync_goes_on_after_one_org_fails_and_reports_only_counts(pg_sessionmaker, sim_client, settings, caplog):
    sm = pg_sessionmaker
    await make_orgs(sm, "acme", "globex", "initech")
    vault = Vault(PgVaultBackend(sm), generate_key())
    asgi = sim_client._transport

    async def handler(request: httpx.Request) -> httpx.Response:
        if b"org-globex" in request.content:
            return httpx.Response(500, text="boom")
        return await asgi.handle_async_request(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sim.test")
    with caplog.at_level(logging.INFO, logger="worker.provisioning"):
        summary = await resync_org_environments(sm, vault, settings, client)
    await client.aclose()
    assert summary == {"orgs": 3, "created": 2, "existing": 0, "failed": 1, "unreachable": False}
    assert await tenant_of(sm, "acme") == "org-acme" and await tenant_of(sm, "initech") == "org-initech"
    assert await tenant_of(sm, "globex") is None
    assert any("globex" in r.getMessage() for r in caplog.records)
    assert "synchronized: 3 org(s), 2 created, 0 already there, 1 failed" in caplog.text


async def test_resync_reports_an_unreachable_simulator(pg_sessionmaker, settings):
    sm = pg_sessionmaker
    await make_orgs(sm, "acme", "globex")
    vault = Vault(PgVaultBackend(sm), generate_key())

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    summary = await resync_org_environments(sm, vault, settings, client)
    await client.aclose()
    assert summary == {"orgs": 2, "created": 0, "existing": 0, "failed": 2, "unreachable": True}


async def test_the_resync_command_needs_database_mode(monkeypatch, capsys):
    monkeypatch.setattr(provisioning, "get_settings", lambda: Settings(AUTH_MODE="file"))
    assert await provisioning._resync_command() == 2
    assert "database mode" in capsys.readouterr().err


# ---------------------------------------------------------------- the container supervisor's plan


def names(plan) -> list[str]:
    return [step.name for step in plan.steps]


def test_plan_all_in_multi_user_mode_runs_sim_resync_api_and_workers_in_order():
    start = load_start()
    plan = start.plan({"PORT": "8080", "SIM_PORT": "8001"}, multi_user=True)
    assert plan.role == "all" and names(plan) == ["company_sim", "resync environments", "api", "job worker 1"]
    sim, resync, api, worker = plan.steps
    assert sim.args == ("-m", "company_sim") and sim.health_url == "http://127.0.0.1:8001/healthz" and sim.group == "sim"
    assert resync.kind == "task" and resync.args == ("-m", "worker.provisioning", "resync")
    assert api.args[:3] == ("-m", "uvicorn", "worker.server:app") and ("--port", "8080") == api.args[api.args.index("--port"):][:2]
    assert api.health_url == "http://127.0.0.1:8080/healthz" and "0.0.0.0" in api.args
    assert worker.args == ("-m", "worker.jobs") and worker.kind == "service" and worker.group == "worker"
    assert plan.env["SIM_HOST"] == "127.0.0.1" and plan.env["SIM_PORT"] == "8001"
    assert plan.env["SIM_BASE_URL"] == "http://127.0.0.1:8001" and plan.env["FORWARDED_ALLOW_IPS"] == "*"
    assert "*" not in api.args  # click expands a bare "*" argument into file names on Windows


def test_plan_migrates_first_only_when_asked_to():
    start = load_start()
    assert "migrate" not in names(start.plan({}, multi_user=True))
    assert "migrate" not in names(start.plan({"MIGRATE_ON_START": "0"}, multi_user=True))
    plan = start.plan({"MIGRATE_ON_START": "1"}, multi_user=True)
    assert names(plan) == ["company_sim", "migrate", "resync environments", "api", "job worker 1"]
    assert plan.steps[1].args == ("-m", "worker.db", "migrate") and plan.steps[1].kind == "task"
    assert "migrate" in names(start.plan({"MIGRATE_ON_START": "True"}, multi_user=True))


def test_plan_without_a_database_is_the_single_user_shape():
    start = load_start()
    plan = start.plan({"MIGRATE_ON_START": "1", "WORKER_PROCESSES": "3"}, multi_user=False)
    assert names(plan) == ["company_sim", "api"]  # no migration, no resync, no job workers


def test_plan_worker_processes():
    start = load_start()
    assert names(start.plan({"WORKER_PROCESSES": "3"}, multi_user=True))[-3:] == ["job worker 1", "job worker 2", "job worker 3"]
    assert names(start.plan({"WORKER_PROCESSES": "0"}, multi_user=True)) == ["company_sim", "resync environments", "api"]
    with pytest.raises(start.ConfigError):
        start.plan({"WORKER_PROCESSES": "-1"}, multi_user=True)


def test_plan_roles():
    start = load_start()
    assert names(start.plan({"ROLE": "api"}, multi_user=True)) == ["api"]
    assert names(start.plan({"ROLE": "api"}, multi_user=False)) == ["api"]
    workers = start.plan({"ROLE": "worker", "WORKER_PROCESSES": "2"}, multi_user=True)
    assert names(workers) == ["job worker 1", "job worker 2"]
    with pytest.raises(start.ConfigError, match="database"):
        start.plan({"ROLE": "worker"}, multi_user=False)
    sim = start.plan({"ROLE": "SIM", "PORT": "9090", "HOST": "0.0.0.0"}, multi_user=True)
    assert names(sim) == ["company_sim"] and sim.env["SIM_PORT"] == "9090" and sim.env["SIM_HOST"] == "0.0.0.0"
    assert sim.steps[0].health_url == "http://127.0.0.1:9090/healthz"
    with pytest.raises(start.ConfigError, match="ROLE"):
        start.plan({"ROLE": "everything"}, multi_user=True)


def test_plan_generates_one_sim_admin_token_when_none_is_set():
    start = load_start()
    first, second = start.plan({}, multi_user=True), start.plan({}, multi_user=True)
    token = first.env["SIM_ADMIN_TOKEN"]
    assert len(token) >= 32 and token != second.env["SIM_ADMIN_TOKEN"]
    assert start.plan({"SIM_ADMIN_TOKEN": "   "}, multi_user=True).env["SIM_ADMIN_TOKEN"].strip()
    assert start.plan({"SIM_ADMIN_TOKEN": "mine"}, multi_user=True).env["SIM_ADMIN_TOKEN"] == "mine"  # a given one is kept
    assert "SIM_ADMIN_TOKEN" not in start.plan({"ROLE": "api"}, multi_user=True).env  # the sim is elsewhere: not ours to invent
    assert start.plan({"ROLE": "sim"}, multi_user=True).notes  # an open admin API on a public port is called out
    assert not start.plan({"ROLE": "sim", "SIM_ADMIN_TOKEN": "x"}, multi_user=True).notes


def test_plan_does_not_touch_the_callers_environment():
    start = load_start()
    env = {"PORT": "9000"}
    plan = start.plan(env, multi_user=True)
    assert env == {"PORT": "9000"} and plan.env is not env


def test_shutdown_stops_workers_before_the_api_before_the_sim(monkeypatch):
    start = load_start()
    stopped: list[str] = []
    monkeypatch.setattr(start, "stop", lambda proc, name, grace: stopped.append(name))
    steps = start.plan({"WORKER_PROCESSES": "2"}, multi_user=True).steps
    running = [(step, object()) for step in steps if step.kind == "service"]
    start.shutdown(running)
    assert stopped.index("api") > max(stopped.index("job worker 1"), stopped.index("job worker 2"))
    assert stopped[-2:] == ["api", "company_sim"]


# ---------------------------------------------------------------- bootstrap script


def test_bootstrap_script_takes_the_demo_password_from_the_environment(monkeypatch, capsys):
    import scripts.bootstrap as script

    seen: dict = {}

    async def fake_bootstrap(url, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")  # the script reports it and exits 2, which ends the test cheaply

    monkeypatch.setattr(script, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(script, "get_settings", lambda: Settings(DATABASE_URL="postgresql://u:pw@localhost/oneforall_test_x"))

    monkeypatch.setenv("BOOTSTRAP_DEMO_PASSWORD", "From-Env-1")
    assert script.main([]) == 2
    assert seen["demo_password"] == "From-Env-1"

    assert script.main(["--demo-password", "From-Flag-2"]) == 2  # the flag wins
    assert seen["demo_password"] == "From-Flag-2"

    monkeypatch.delenv("BOOTSTRAP_DEMO_PASSWORD")
    assert script.main([]) == 2
    assert seen["demo_password"] is None  # bootstrap() then generates one
    assert ":pw@" not in capsys.readouterr().err  # the error message shows the masked URL only


def test_describe_leaves_a_supplied_demo_password_out_of_the_text():
    from worker.db.seed import SeedResult

    seed = SeedResult(orgs_created=["acme"], users_created={"owner@acme.test": "owner"}, memberships_created=1)
    result = bootstrap_module.BootstrapResult(created_database=False, reset=False, revision="0005_memory", seed=seed,
                                              password="Supplied-Pw-9")
    url = "postgresql://u:dbpw@localhost/oneforall_test_x"
    assert "Supplied-Pw-9" in bootstrap_module.describe(result, url)  # the default: shown once
    hidden = bootstrap_module.describe(result, url, show_password=False)
    assert "Supplied-Pw-9" not in hidden and "dbpw" not in hidden and "not printed" in hidden


# ---------------------------------------------------------------- deterministic tenant secrets (SIM_TENANT_SECRET)


def make_tenant(tenant_id: str):
    return tenants.provision_tenant(tenant_id)


def shape(tenant) -> dict:
    return {"token": tenant.token, "api_key": tenant.api_key, "credentials": tenant.credentials, "salt": tenant.seed_salt}


def test_a_shared_secret_makes_tenants_identical_across_simulators(sim_dirs, monkeypatch):
    monkeypatch.setenv("SIM_TENANT_SECRET", "shared-secret-one")
    first = shape(make_tenant("org-acme"))
    sim_dirs()  # a new container: an empty data dir
    assert tenants.get_tenant("org-acme") is None
    again = make_tenant("org-acme")
    assert shape(again) == first
    assert tenants.find_by_token(first["token"]).id == "org-acme" and tenants.find_by_api_key(first["api_key"]).id == "org-acme"

    # same format and length as the random secrets
    assert len(again.token) == 32 and re.fullmatch(r"[A-Za-z0-9_-]{32}", again.token)
    assert again.api_key == again.credentials["erp_api_key"] and again.api_key.startswith("ledger-")
    assert len(again.api_key) == len("ledger-") + 32
    for app in ("mail", "portal", "erp"):
        assert re.fullmatch(r"[A-Za-z0-9_-]{16}", again.credentials[app]["password"])
    # the parts are independent of each other
    secrets_ = [again.token, again.api_key, *(again.credentials[a]["password"] for a in ("mail", "portal", "erp"))]
    assert len(set(secrets_)) == 5

    other_id = shape(make_tenant("org-globex"))
    assert other_id["token"] != first["token"] and other_id["api_key"] != first["api_key"]
    assert other_id["credentials"]["erp"]["password"] != first["credentials"]["erp"]["password"]

    sim_dirs()
    monkeypatch.setenv("SIM_TENANT_SECRET", "shared-secret-two")
    other_secret = shape(make_tenant("org-acme"))
    assert other_secret["token"] != first["token"] and other_secret["api_key"] != first["api_key"]
    assert other_secret["credentials"] != first["credentials"]
    assert other_secret["salt"] == first["salt"] == "org-acme"  # the seed salt (the data) does not depend on the secret


def test_the_seeded_data_of_a_tenant_is_the_same_in_every_simulator(sim_dirs, monkeypatch):
    from company_sim import db

    monkeypatch.setenv("SIM_TENANT_SECRET", "shared-secret-one")

    def invoices() -> list:
        make_tenant("org-acme")
        with tenants.use_tenant("org-acme"), db.connect() as conn:
            return [tuple(row) for row in conn.execute("select * from bills order by id")] \
                if _has_table(conn, "bills") else [tuple(r) for r in conn.execute("select * from sqlite_master order by name")]

    first = invoices()
    sim_dirs()
    assert invoices() == first and first


def _has_table(conn, name: str) -> bool:
    return bool(conn.execute("select 1 from sqlite_master where name = ?", (name,)).fetchone())


def test_without_a_secret_tenants_stay_random(sim_dirs, monkeypatch):
    monkeypatch.delenv("SIM_TENANT_SECRET", raising=False)
    first = shape(make_tenant("org-acme"))
    sim_dirs()
    second = shape(make_tenant("org-acme"))
    assert first["token"] != second["token"] and first["api_key"] != second["api_key"]
    assert first["credentials"] != second["credentials"]
    monkeypatch.setenv("SIM_TENANT_SECRET", "   ")  # blank counts as unset
    sim_dirs()
    a = make_tenant("org-acme")
    sim_dirs()
    assert make_tenant("org-acme").token != a.token


def test_entry_tokens_are_hidden_in_the_sim_access_log():
    from company_sim.__main__ import hide_entry_tokens

    original = logging.getLogRecordFactory()
    try:
        hide_entry_tokens()
        make = logging.getLogRecordFactory()
        record = make("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                   ("127.0.0.1:1", "GET", "/t/SecretToken123_-/erp?next=/x", "1.1", 200), None)
        text = record.getMessage()
        assert "SecretToken123" not in text and "/t/[hidden]/erp?next=/x" in text
        plain = make("uvicorn.access", logging.INFO, __file__, 1, "%s", ("/erp/bills",), None)
        assert plain.getMessage() == "/erp/bills"
    finally:
        logging.setLogRecordFactory(original)


async def test_resync_logs_and_summary_contain_no_secrets(pg_sessionmaker, sim_client, settings, monkeypatch, caplog):
    monkeypatch.setenv("SIM_TENANT_SECRET", "shared-secret-one")
    sm = pg_sessionmaker
    await make_orgs(sm, "acme")
    vault = Vault(PgVaultBackend(sm), generate_key())
    with caplog.at_level(logging.DEBUG):
        summary = await resync_org_environments(sm, vault, settings, sim_client)
    tenant = tenants.get_tenant("org-acme")
    assert summary["created"] == 1
    values = [tenant.token, tenant.api_key, *(tenant.credentials[a]["password"] for a in ("mail", "portal", "erp"))]
    blob = caplog.text + repr(summary)
    assert all(value not in blob for value in values)
