"""Tenant isolation in the multi-user server: another org's runs, files, events, approvals and credentials are invisible
(always 404, never 403), and each org gets its own simulated company, credentials and browser cookie.

Database mode against ``oneforall_test``; the simulated company runs in-process (``httpx.ASGITransport``) on a temporary
``DATA_DIR``, and the cookie pre-seed is checked in a real pooled Chromium against that same app served on a local port.
"""
from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import pytest_asyncio
import uvicorn
from dbapp import (
    ENTRY_TOKENS,
    ERP_PASSWORDS,
    finished,
    pending_request,
    runner_of,
    seed_world,
    session_cookie,
    shutdown_all,
    sign_in,
    start_run,
    write_script,
    ws_connect,
)
from sqlalchemy import select, text

from company_sim import chaos, config, tenants
from worker.config import Settings
from worker.db.bootstrap import bootstrap
from worker.db.models import Org
from worker.provisioning import load_org_environment
from worker.tools.base import ToolContext
from worker.tools.browser import BrowserSession
from worker.tools.browser_pool import POOL_KEY
from worker.tools.retry import execute_with_retry
from worker.tools.secrets import ListCredentialsTool
from worker.vault import Vault
from worker.vault_pg import PgVaultBackend

UNKNOWN_RUN = "0" * 32


# --------------------------------------------------------------------------- foreign runs


async def test_a_foreign_run_is_a_404_everywhere_and_never_a_403(world, db_app, tmp_path):
    app, planner = db_app()
    planner.add(*write_script())  # a globex run that stays pending: waiting for approval
    globex_op = await sign_in(app, "operator@globex.test")
    run_id = await start_run(globex_op, "Globex secret task")
    request = await pending_request(globex_op, run_id)
    (tmp_path / "runs" / run_id / "screenshots").mkdir(parents=True)
    (tmp_path / "runs" / run_id / "screenshots" / "a.png").write_bytes(b"\x89PNG globex only")

    acme_users = [await sign_in(app, f"{who}@acme.test") for who in ("owner", "admin", "operator", "approver", "viewer")]
    answer = {"request_id": request["request_id"], "approved": True}
    probes = [("GET", "/api/runs/{id}", None), ("POST", "/api/runs/{id}/respond", answer),
              ("GET", "/runs/{id}/files/report.md", None), ("GET", "/runs/{id}/files/screenshots/a.png", None)]
    for user in acme_users:  # every role of acme, including the ones that could approve in their own org
        assert (await user.get("/api/runs")).json() == []
        for method, template, body in probes:
            foreign = await user.request(method, template.format(id=run_id), json=body)
            ghost = await user.request(method, template.format(id=UNKNOWN_RUN), json=body)
            assert foreign.status_code == 404, (method, template, foreign.status_code)
            assert (foreign.status_code, foreign.json()) == (ghost.status_code, ghost.json())  # same answer as for an unknown id
    assert "Globex secret task" not in (await acme_users[0].get("/api/runs")).text

    # nothing an acme user tried touched the globex run
    detail = (await globex_op.get(f"/api/runs/{run_id}")).json()
    assert detail["pending"] and detail["active"] and detail["status"] == "waiting_approval"
    assert (await globex_op.get("/api/runs")).json()[0]["id"] == run_id
    assert (await globex_op.get(f"/runs/{run_id}/files/screenshots/a.png")).content.startswith(b"\x89PNG")

    approver = await sign_in(app, "approver@globex.test")  # the right org's approver can answer it
    assert (await approver.post(f"/api/runs/{run_id}/respond", json=answer)).status_code == 200
    await finished(globex_op, run_id)
    for user in acme_users:  # after the run is over the answers are the same
        assert (await user.get(f"/api/runs/{run_id}")).status_code == 404
        assert (await user.get(f"/runs/{run_id}/files/report.md")).status_code == 404
        assert (await user.get(f"/runs/{run_id}/files/screenshots/a.png")).status_code == 404


async def test_cancel_of_a_foreign_run_is_404_for_operators_and_does_not_cancel_it(world, db_app):
    app, planner = db_app()
    planner.add(*write_script())
    globex_op = await sign_in(app, "operator@globex.test")
    run_id = await start_run(globex_op)
    await pending_request(globex_op, run_id)
    for who in ("operator", "admin", "owner"):
        user = await sign_in(app, f"{who}@acme.test")
        assert (await user.post(f"/api/runs/{run_id}/cancel")).status_code == 404
        assert (await user.post(f"/api/runs/{UNKNOWN_RUN}/cancel")).status_code == 404
    assert (await globex_op.get(f"/api/runs/{run_id}")).json()["active"] is True
    assert (await globex_op.post(f"/api/runs/{run_id}/cancel")).status_code == 200
    await finished(globex_op, run_id)


async def test_websocket_needs_a_session_and_the_run_must_belong_to_the_callers_org(world, db_app):
    app, planner = db_app()
    globex_op = await sign_in(app, "operator@globex.test")
    run_id = await start_run(globex_op, "Globex only")
    await finished(globex_op, run_id)
    path = f"/api/runs/{run_id}/events"

    anonymous = await ws_connect(app, path)
    assert not anonymous.accepted and anonymous.close_code == 1008 and anonymous.messages == []
    forged = await ws_connect(app, path, "ofa_session=" + "a" * 43)
    assert not forged.accepted and forged.close_code == 1008

    own = await ws_connect(app, path, session_cookie(globex_op))
    assert own.accepted and own.types[0] == "run_queued" and own.types[-1] == "replay_done" and "run_finished" in own.types
    assert "Globex only" in own.messages[0]["payload"]["task"]

    acme = await sign_in(app, "admin@acme.test")
    foreign = await ws_connect(app, path, session_cookie(acme))
    ghost = await ws_connect(app, f"/api/runs/{UNKNOWN_RUN}/events", session_cookie(acme))
    assert foreign.accepted and foreign.types == ["error"] and foreign.messages[0]["payload"] == {"message": "Unknown run"}
    assert (ghost.types, ghost.messages[0]["payload"]) == (foreign.types, foreign.messages[0]["payload"])  # indistinguishable
    assert "Globex only" not in repr(foreign.messages)

    old_cookie = session_cookie(globex_op)
    await globex_op.post("/api/auth/logout")
    revoked = await ws_connect(app, path, old_cookie)
    assert not revoked.accepted and revoked.close_code == 1008

    acme_op = await sign_in(app, "operator@acme.test")  # a page on another origin cannot ride the cookie
    hostile = await ws_connect(app, f"/api/runs/{UNKNOWN_RUN}/events", session_cookie(acme_op), {"origin": "http://evil.example"})
    assert not hostile.accepted and hostile.close_code == 1008
    friendly = await ws_connect(app, f"/api/runs/{UNKNOWN_RUN}/events", session_cookie(acme_op), {"origin": "http://test"})
    assert friendly.accepted


async def test_a_foreign_websocket_does_not_subscribe_to_a_live_run(world, db_app):
    app, planner = db_app()
    planner.add(*write_script())
    globex_op = await sign_in(app, "operator@globex.test")
    run_id = await start_run(globex_op)
    await pending_request(globex_op, run_id)
    foreign = await ws_connect(app, f"/api/runs/{run_id}/events", session_cookie(await sign_in(app, "viewer@acme.test")))
    assert foreign.types == ["error"] and app.state.hub._feeds == {}  # nothing was subscribed on the foreign caller's behalf
    own = await ws_connect(app, f"/api/runs/{run_id}/events", session_cookie(globex_op), timeout=1.0)
    assert "approval_requested" in own.types and own.messages[-1]["payload"] == {"live": True}  # the live run is followable
    await globex_op.post(f"/api/runs/{run_id}/cancel")
    await finished(globex_op, run_id)


# --------------------------------------------------------------------------- credentials and the run's environment


async def test_runs_get_only_their_own_orgs_credentials_and_never_see_the_entry_token(world, db_app):
    app, planner = db_app()
    runs = {}
    for slug in ("acme", "globex"):
        client = await sign_in(app, f"operator@{slug}.test")
        runs[slug] = await start_run(client)
        await finished(client, runs[slug])

    for slug, other in (("acme", "globex"), ("globex", "acme")):
        runner = runner_of(app, runs[slug])
        scope = runner.vault_scope
        assert scope.owner == str(world.org_ids[slug]) and scope.names() == ["erp"]
        assert scope.get("erp").fields["password"] == ERP_PASSWORDS[slug]
        assert scope.get("sim_entry") is None and "sim_entry" not in [c["name"] for c in scope.public()]  # hidden from the agent
        assert scope.internal("sim_entry").fields["value"] == ENTRY_TOKENS[slug]
        listing = await execute_with_retry(ListCredentialsTool(), {}, runner.ctx)  # what the agent's own tool shows it
        assert "erp" in listing.observation and "sim_entry" not in listing.observation and ENTRY_TOKENS[slug] not in listing.observation
        # redaction covers the token too, but only the run's own org's secrets
        assert runner.redactor.redact(f"cookie={ENTRY_TOKENS[slug]}") == "cookie=[REDACTED:sim_entry.value]"
        assert runner.redactor.redact(ENTRY_TOKENS[other]) == ENTRY_TOKENS[other]
        assert runner.redactor.redact(ERP_PASSWORDS[slug]) == "[REDACTED:erp.password]"

    llms = dict(zip(app.state.test_worker.claimed, planner.created))  # the worker creates each run's LLM when it claims it
    for slug, _other in (("acme", "globex"), ("globex", "acme")):
        prompt = "\n".join(m.content for m in llms[runs[slug]].calls[0]["messages"])
        assert "erp" in prompt and "secure vault" in prompt  # the agent is told which credential names exist
        for secret in (ENTRY_TOKENS["acme"], ENTRY_TOKENS["globex"], ERP_PASSWORDS["acme"], ERP_PASSWORDS["globex"], "sim_entry"):
            assert secret not in prompt, secret
        assert "Company intranet start page: http://127.0.0.1:9" in prompt and "/t/" not in prompt  # no token link either

    cookie = runner_of(app, runs["acme"]).ctx.extras["browser_init_cookies"]
    assert cookie == [{"name": "sim_tenant", "value": ENTRY_TOKENS["acme"], "url": "http://127.0.0.1:9/", "httpOnly": True, "sameSite": "Lax"}]
    assert runner_of(app, runs["globex"]).ctx.extras["browser_init_cookies"][0]["value"] == ENTRY_TOKENS["globex"]

    async with world.sm() as session:  # nothing about the token was written to the run's events or steps
        dump = repr((await session.execute(text("select payload from run_events"))).all()) + repr(
            (await session.execute(text("select observation, args, thought from run_steps"))).all())
    assert ENTRY_TOKENS["acme"] not in dump and ENTRY_TOKENS["globex"] not in dump


# --------------------------------------------------------------------------- the simulated company, in-process


@pytest.fixture
def sim_app(tmp_path, monkeypatch):
    """The company simulator on a temporary data dir (tenants, databases and PDFs are all under tmp_path)."""
    data = tmp_path / "simdata"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "DB_PATH", data / "sim.db")
    monkeypatch.setattr(config, "PDF_DIR", data / "pdfs")
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path / "simworkspace")
    monkeypatch.delenv("SIM_ADMIN_TOKEN", raising=False)
    from company_sim.app import app

    chaos.set_flags([])
    yield app
    chaos.set_flags([])


@pytest_asyncio.fixture
async def sim_client(sim_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sim_app), base_url="http://sim.test") as client:
        yield client


@pytest_asyncio.fixture(loop_scope="session")
async def bare_world(pg_sessionmaker):
    """The seeded orgs, but without any simulated company yet."""
    yield await seed_world(pg_sessionmaker, environments=False)
    await shutdown_all()


def provisioned_app(bare_world, sim_client, tmp_path, **settings):
    from dbapp import make_app

    app, planner = make_app(bare_world, tmp_path, SIM_BASE_URL="http://sim.test", **settings)
    app.state.sim_http = sim_client  # the platform reaches the simulator in-process
    return app, planner


async def raw_credentials(sm) -> str:
    async with sm() as session:
        return repr((await session.execute(text("select * from credentials"))).all())


async def test_provisioning_creates_the_orgs_tenant_and_stores_its_logins_encrypted(bare_world, sim_client, tmp_path):
    app, planner = provisioned_app(bare_world, sim_client, tmp_path)
    sm, vault = bare_world.sm, bare_world.vault
    acme_admin = await sign_in(app, "admin@acme.test")
    acme_op = await sign_in(app, "operator@acme.test")

    assert (await acme_admin.get("/api/admin/environment")).json() == {"provisioned": False, "tenant_id": None, "sim_base_url": "http://sim.test"}
    assert (await acme_op.get("/api/auth/me")).json()["org"]["environment_ready"] is False
    refused = await acme_op.post("/api/runs", json={"task": "x"})
    assert refused.status_code == 409 and "provision" in refused.json()["detail"]

    resp = await acme_admin.post("/api/admin/environment/provision")
    assert resp.status_code == 200 and resp.json() == {"tenant_id": "org-acme", "created": True,
                                                       "credentials": ["mail", "portal", "erp", "erp_api"]}
    tenant = tenants.get_tenant("org-acme")
    assert tenant is not None and "token" not in resp.text and tenant.token not in resp.text and tenant.api_key not in resp.text

    owner = str(bare_world.org_ids["acme"])
    entries = {c["name"]: c for c in await vault.list_public(owner)}
    assert sorted(entries) == ["erp", "erp_api", "mail", "portal", "sim_entry"]
    assert (entries["mail"]["kind"], entries["erp_api"]["kind"], entries["sim_entry"]["kind"]) == ("login", "api_key", "header")
    assert entries["erp"]["url"] == "http://sim.test/erp" and entries["erp"]["host"] == "sim.test"
    assert entries["erp_api"]["host"] == "sim.test" and entries["erp_api"]["fields"] == ["api_key", "header_name"]
    for app_key in ("mail", "portal", "erp"):
        fields = (await vault.get(owner, app_key)).fields
        assert fields == tenant.credentials[app_key]  # exactly the logins the simulator generated for this tenant
    api = await vault.get(owner, "erp_api")
    assert api.fields == {"api_key": tenant.api_key, "header_name": "X-API-Key"} and api.auth_header() == ("X-API-Key", tenant.api_key)
    assert (await vault.get(owner, "sim_entry")).fields == {"header_name": "X-Sim-Tenant", "value": tenant.token}

    dump = await raw_credentials(sm)  # the database holds ciphertext only
    for secret in (tenant.token, tenant.api_key, *(tenant.credentials[k]["password"] for k in ("mail", "portal", "erp"))):
        assert secret not in dump
    async with sm() as session:
        org = await session.get(Org, bare_world.org_ids["acme"])
        other = await session.get(Org, bare_world.org_ids["globex"])
        blobs = (await session.execute(text("select ciphertext from credentials"))).scalars().all()
    assert (org.sim_tenant_id, other.sim_tenant_id) == ("org-acme", None)
    assert all(bytes(b).startswith(b"gAAAA") for b in blobs)
    assert (await acme_op.get("/api/auth/me")).json()["org"]["environment_ready"] is True
    assert "environment.provision" in [row.action for row in await audit_rows(sm)]

    again = await acme_admin.post("/api/admin/environment/provision")  # safe to repeat: same tenant, same token, no reset
    assert again.json()["created"] is False and tenants.get_tenant("org-acme").token == tenant.token
    assert (await vault.get(owner, "sim_entry")).fields["value"] == tenant.token

    # the second org gets a different, separate company
    globex_admin = await sign_in(app, "admin@globex.test")
    assert (await globex_admin.post("/api/admin/environment/provision")).json()["tenant_id"] == "org-globex"
    globex = tenants.get_tenant("org-globex")
    assert globex.token != tenant.token and globex.api_key != tenant.api_key
    assert globex.credentials["erp"]["password"] != tenant.credentials["erp"]["password"]
    gx_owner = str(bare_world.org_ids["globex"])
    assert (await vault.get(gx_owner, "erp")).fields == globex.credentials["erp"]
    assert (await vault.get(owner, "erp")).fields == tenant.credentials["erp"]  # acme's entry untouched by globex's provisioning

    # the org can now run, and its run is pinned to its own tenant
    run_id = await start_run(acme_op)
    await finished(acme_op, run_id)
    runner = runner_of(app, run_id)
    assert runner.ctx.extras["browser_init_cookies"] == [{"name": "sim_tenant", "value": tenant.token, "url": "http://sim.test/",
                                                         "httpOnly": True, "sameSite": "Lax"}]
    assert sorted(runner.vault_scope.names()) == ["erp", "erp_api", "mail", "portal"]


async def audit_rows(sm):
    from worker.db.models import AuditLog

    async with sm() as session:
        return list((await session.scalars(select(AuditLog).order_by(AuditLog.id))).all())


async def test_environment_reset_only_touches_the_admins_own_tenant(bare_world, sim_client, tmp_path):
    app, planner = provisioned_app(bare_world, sim_client, tmp_path)
    acme_admin, globex_admin = await sign_in(app, "admin@acme.test"), await sign_in(app, "admin@globex.test")
    for admin in (acme_admin, globex_admin):
        assert (await admin.post("/api/admin/environment/provision")).status_code == 200
    assert (await acme_admin.get("/api/sim/chaos")).json()["active"] == []

    reset = await acme_admin.post("/api/admin/environment/reset", json={"chaos": ["duplicate_bill"]})
    assert reset.status_code == 200 and reset.json()["chaos"] == ["duplicate_bill"]
    assert tenants.get_tenant("org-acme").chaos == ("duplicate_bill",) and tenants.get_tenant("org-globex").chaos == ()
    assert (await acme_admin.get("/api/sim/chaos")).json()["active"] == ["duplicate_bill"]  # the dashboard panel reads the org's own
    assert (await globex_admin.get("/api/sim/chaos")).json()["active"] == []
    assert "duplicate_bill" not in chaos.active()  # and the shared default tenant is not involved at all

    alias = await acme_admin.post("/api/sim/reset", json={"chaos": []}, follow_redirects=True)  # the old endpoint, now org-scoped
    assert alias.status_code == 200 and tenants.get_tenant("org-acme").chaos == ()
    assert (await acme_admin.post("/api/admin/environment/reset", json={"chaos": ["bogus"]})).status_code == 422
    operator = await sign_in(app, "operator@acme.test")
    assert (await operator.post("/api/admin/environment/reset", json={"chaos": []})).status_code == 403
    assert (await operator.post("/api/sim/reset", json={"chaos": []})).status_code == 403

    planner.add(*write_script())  # a run in progress blocks the reset
    run_id = await start_run(operator)
    await pending_request(operator, run_id)
    busy = await acme_admin.post("/api/admin/environment/reset", json={"chaos": []})
    assert busy.status_code == 409 and "run is active" in busy.json()["detail"]
    assert (await globex_admin.post("/api/admin/environment/reset", json={"chaos": []})).status_code == 200  # other orgs are free
    await operator.post(f"/api/runs/{run_id}/cancel")
    await finished(operator, run_id)
    assert [r.action for r in await audit_rows(bare_world.sm)].count("environment.reset") == 3


async def test_reset_needs_an_environment_and_provisioning_reports_an_unreachable_simulator(bare_world, tmp_path):
    from dbapp import make_app

    app, _ = make_app(bare_world, tmp_path)  # SIM_BASE_URL points at a closed port
    admin = await sign_in(app, "admin@acme.test")
    assert (await admin.post("/api/admin/environment/provision")).status_code == 502
    assert "not reachable" in (await admin.post("/api/admin/environment/provision")).json()["detail"]
    assert (await admin.post("/api/admin/environment/reset", json={"chaos": []})).status_code == 409  # nothing to reset yet
    assert (await admin.get("/api/sim/chaos")).status_code == 409


async def test_the_admin_token_is_sent_to_the_simulator_and_never_leaked(bare_world, sim_client, tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_ADMIN_TOKEN", "sim-admin-secret-42")
    good, _ = provisioned_app(bare_world, sim_client, tmp_path, SIM_ADMIN_TOKEN="sim-admin-secret-42")
    assert (await (await sign_in(good, "admin@acme.test")).post("/api/admin/environment/provision")).status_code == 200
    bad, _ = provisioned_app(bare_world, sim_client, tmp_path, SIM_ADMIN_TOKEN="wrong-token-value")
    resp = await (await sign_in(bad, "admin@globex.test")).post("/api/admin/environment/provision")
    assert resp.status_code == 502 and "401" in resp.json()["detail"]
    assert "wrong-token-value" not in resp.text and "sim-admin-secret-42" not in resp.text


async def test_bootstrap_provisions_the_demo_orgs_when_asked_and_skips_gracefully(pg_required_engine, pg_sessionmaker, sim_client, tmp_path):
    url = pg_required_engine.url
    settings = Settings(_env_file=None, SIM_BASE_URL="http://sim.test", VAULT_DIR=str(tmp_path / "vault"))
    done = await bootstrap(url, demo_password="Demo-Pass-1", provision_sim=True, settings=settings, sim_client=sim_client)
    assert done.provisioned == {"acme": "org-acme", "globex": "org-globex"} and done.provision_note is None
    async with pg_sessionmaker() as session:
        orgs = {o.slug: o for o in (await session.scalars(select(Org))).all()}
    assert (orgs["acme"].sim_tenant_id, orgs["globex"].sim_tenant_id) == ("org-acme", "org-globex")
    vault = Vault(PgVaultBackend(pg_sessionmaker), (tmp_path / "vault" / ".vault_key").read_text().strip())
    assert sorted(c["name"] for c in await vault.list_public(str(orgs["acme"].id))) == ["erp", "erp_api", "mail", "portal", "sim_entry"]
    assert (await vault.get(str(orgs["globex"].id), "sim_entry")).fields["value"] == tenants.get_tenant("org-globex").token

    offline = Settings(_env_file=None, SIM_BASE_URL="http://127.0.0.1:9", VAULT_DIR=str(tmp_path / "vault"))
    skipped = await bootstrap(url, provision_sim=True, settings=offline)  # no simulator: a message, not an error
    assert skipped.provisioned == {} and "not provisioned" in skipped.provision_note and "Start the simulator" in skipped.provision_note
    from worker.db.bootstrap import describe

    assert skipped.provision_note in describe(skipped, url)
    assert (await bootstrap(url, settings=offline)).provision_note is None  # without the flag nothing is attempted


# --------------------------------------------------------------------------- cookie pre-seed in a real browser


def free_port() -> int:
    for port in range(8301, 8400):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 8301-8399")


class SimServer:
    """The simulator served on a real local port (a browser cannot use ASGITransport)."""

    def __init__(self, app, port: int) -> None:
        self.url = f"http://127.0.0.1:{port}"
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> SimServer:
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert self.server.started, "the simulator did not start"
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(10)


async def erp_login(session: BrowserSession, base: str, username: str, password: str) -> tuple[str, str]:
    """Open the ERP login page in the run's browser context, sign in, and return (final URL, page text)."""
    page = session.page
    await page.goto(f"{base}/erp/login")
    await page.fill("input[name=username]", username)
    await page.fill("input[name=password]", password)
    async with page.expect_navigation():
        await page.click("button[type=submit]")
    return page.url, await page.inner_text("body")


async def test_the_runs_browser_context_is_pinned_to_its_orgs_tenant(bare_world, sim_app, sim_client, browser_pool, tmp_path):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    app, _ = provisioned_app(bare_world, sim_client, tmp_path)
    for slug in ("acme", "globex"):
        admin = await sign_in(app, f"admin@{slug}.test")
        # provisioning goes through the in-process client; only the cookie URL below needs the real port
        assert (await admin.post("/api/admin/environment/provision")).status_code == 200

    async def environment(slug: str):
        async with bare_world.sm() as session:
            org = await session.get(Org, bare_world.org_ids[slug])
        env = await load_org_environment(org, bare_world.vault)
        assert env is not None
        return env

    def make_ctx(name: str, env, with_cookie: bool) -> ToolContext:
        run_dir = tmp_path / "ctx" / name
        run_dir.mkdir(parents=True)
        extras = {POOL_KEY: browser_pool, "vault_scope": env.scope}
        if with_cookie:
            extras["browser_init_cookies"] = env.browser_cookies(base)
        return ToolContext(run_id=name, run_dir=run_dir, workspace=tmp_path, settings=Settings(_env_file=None, HEADLESS=True), extras=extras)

    acme, globex = await environment("acme"), await environment("globex")
    acme_erp, globex_erp = acme.scope.get("erp").fields, globex.scope.get("erp").fields
    assert acme_erp != globex_erp
    sessions: list[BrowserSession] = []

    async def browser(name: str, env, with_cookie: bool = True) -> BrowserSession:
        session = BrowserSession(make_ctx(name, env, with_cookie))
        sessions.append(session)
        await session.start()
        return session

    try:
        with SimServer(sim_app, port):
            pinned = await browser("acme-run", acme)
            cookies = {c["name"]: c for c in await pinned._context.cookies()}
            assert cookies["sim_tenant"]["value"] == acme.scope.internal("sim_entry").fields["value"]
            assert cookies["sim_tenant"]["httpOnly"] is True and cookies["sim_tenant"]["domain"] == "127.0.0.1"

            # without ever opening an entry link, the agent is inside acme's own company: acme's login works ...
            url, body = await erp_login(pinned, base, acme_erp["username"], acme_erp["password"])
            assert url.endswith("/erp/bills"), body[:200]
            expected = tenants.tenant_state("org-acme")["bills"]
            assert expected and any(f"{float(b['amount']):,.2f}" in body for b in expected)  # acme's own ledger is on screen
            # ... and globex's password is rejected there
            url, body = await erp_login(await browser("acme-run-2", acme), base, globex_erp["username"], globex_erp["password"])
            assert "/erp/login" in url and "Invalid username or password" in body

            other = await browser("globex-run", globex)
            url, _ = await erp_login(other, base, globex_erp["username"], globex_erp["password"])
            assert url.endswith("/erp/bills")
            url, body = await erp_login(await browser("globex-run-2", globex), base, acme_erp["username"], acme_erp["password"])
            assert "/erp/login" in url and "Invalid username or password" in body

            # a context without the pre-seed lands in the default company: neither tenant's password works there
            bare = await browser("no-cookie", acme, with_cookie=False)
            assert "sim_tenant" not in {c["name"] for c in await bare._context.cookies()}
            url, body = await erp_login(bare, base, acme_erp["username"], acme_erp["password"])
            assert "/erp/login" in url and "Invalid username or password" in body

            # a restarted browser context gets the cookie again before its first page
            await pinned.reset()
            await pinned.start()
            assert "sim_tenant" in {c["name"] for c in await pinned._context.cookies()}
            url, _ = await erp_login(pinned, base, acme_erp["username"], acme_erp["password"])
            assert url.endswith("/erp/bills")
    finally:
        for session in sessions:
            await session.close()
    assert browser_pool.active_contexts == 0
