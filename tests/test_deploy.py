"""Deployment features: access password, reverse proxy to the sim, daily run cap, config flags. No real ports."""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from worker.config import Settings
from worker.deploy import DailyRunCap, rewrite_location, safe_next
from worker.llm.base import LLMError
from worker.llm.fake import ScriptedLLM
from worker.server import create_app
from worker.tools.registry import ToolRegistry

UPSTREAM = "http://sim.internal:8001"
PASSWORD = "correct horse"


def empty_registry(_settings: Settings) -> ToolRegistry:
    return ToolRegistry()


def build(tmp_path: Path, llm_factory=None, **overrides) -> TestClient:
    settings = Settings(LLM_PROVIDER="fake", RUNS_DIR=str(tmp_path / "runs"), WORKSPACE_DIR=str(tmp_path / "ws"),
                        SIM_BASE_URL=UPSTREAM, LLM_MIN_INTERVAL_S=0.0, **overrides)
    (tmp_path / "ws").mkdir(exist_ok=True)
    app = create_app(settings, llm_factory=llm_factory or (lambda s: ScriptedLLM([])), registry_factory=empty_registry)
    app.state.sim_transport = httpx.ASGITransport(app=fake_sim())
    client = TestClient(app, follow_redirects=False)
    client.__enter__()
    return client


@pytest.fixture
def make(tmp_path):
    clients: list[TestClient] = []

    def factory(**kw) -> TestClient:
        client = build(tmp_path, **kw)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def fake_sim() -> FastAPI:
    """Stands in for company_sim: records what it receives, sets cookies, redirects to its own origin."""
    sim = FastAPI()
    sim.state.seen = []

    @sim.get("/")
    async def landing() -> PlainTextResponse:
        return PlainTextResponse("sim landing")

    @sim.get("/erp/login")
    async def erp_login() -> Response:
        response = PlainTextResponse("login page")
        response.set_cookie("erp_session", "abc", httponly=True)
        response.set_cookie("second", "two")
        return response

    @sim.get("/erp")
    async def erp_root() -> RedirectResponse:
        return RedirectResponse(f"{UPSTREAM}/erp/bills?page=2", status_code=303)

    @sim.get("/erp/away")
    async def erp_away() -> RedirectResponse:
        return RedirectResponse("https://example.org/elsewhere", status_code=302)

    @sim.api_route("/erp/echo", methods=["GET", "POST"])
    async def echo(request: Request) -> JSONResponse:
        return JSONResponse({"method": request.method, "query": request.url.query, "body": (await request.body()).decode(),
                             "cookie": request.headers.get("cookie", ""), "host": request.headers.get("host", "")})

    @sim.get("/static/style.css")
    async def css() -> Response:
        return Response("body{}", media_type="text/css")

    @sim.api_route("/admin/chaos", methods=["GET", "POST"])
    async def admin() -> dict:
        return {"active": [], "secret": "admin reached"}

    return sim


# --------------------------------------------------------------------------- access control


def test_everything_open_when_no_password(make):
    client = make()
    assert client.get("/").status_code == 200
    assert client.get("/api/scenarios").status_code == 200
    assert client.get("/login").status_code == 404  # no login routes without a password
    assert client.get("/erp/login").status_code == 404  # proxy is off by default


def test_protected_routes_require_login(make):
    client = make(ACCESS_PASSWORD=PASSWORD)
    page = client.get("/")
    assert page.status_code == 303 and page.headers["location"].startswith("/login?next=")
    assert client.get("/ui/app.js").status_code == 303
    api = client.get("/api/runs")
    assert api.status_code == 401 and api.json()["detail"]
    assert client.get("/api/config").status_code == 401
    assert client.post("/api/runs", json={"task": "x"}).status_code == 401
    assert client.get("/runs/abc/files/report.md").status_code == 303
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/runs/abcd1234/events"):
            pass


def test_login_flow(make):
    client = make(ACCESS_PASSWORD=PASSWORD, SESSION_SECRET="s3cret")
    form = client.get("/login")
    assert form.status_code == 200 and "<form" in form.text and 'type="password"' in form.text

    wrong = client.post("/login", data={"password": "nope", "next": "/"})
    assert wrong.status_code == 401 and "Wrong password" in wrong.text
    assert client.get("/api/runs").status_code == 401

    ok = client.post("/login", data={"password": PASSWORD, "next": "/api/scenarios"})
    assert ok.status_code == 303 and ok.headers["location"] == "/api/scenarios"
    cookie = ok.headers["set-cookie"].lower()
    assert "ca_auth=" in cookie and "httponly" in cookie
    assert client.get("/").status_code == 200
    assert client.get("/api/runs").status_code == 200
    with client.websocket_connect("/api/runs/abcd1234/events") as ws:  # authenticated socket gets through (unknown run -> error event)
        assert ws.receive_json()["type"] == "error"

    out = client.get("/logout")
    assert out.status_code == 303 and out.headers["location"] == "/login"
    assert client.get("/api/runs").status_code == 401


def test_https_login_cookie_is_secure_samesite_none(make):
    client = make(ACCESS_PASSWORD=PASSWORD)
    ok = client.post("/login", data={"password": PASSWORD}, headers={"x-forwarded-proto": "https"})
    cookie = ok.headers["set-cookie"].lower()
    assert "secure" in cookie and "samesite=none" in cookie


def test_forged_and_foreign_cookies_are_rejected(make):
    other = make(ACCESS_PASSWORD=PASSWORD, SESSION_SECRET="one")
    token = other.post("/login", data={"password": PASSWORD}).cookies["ca_auth"]
    victim = make(ACCESS_PASSWORD=PASSWORD, SESSION_SECRET="two")
    victim.cookies.set("ca_auth", token)
    assert victim.get("/api/runs").status_code == 401
    victim.cookies.set("ca_auth", "garbage")
    assert victim.get("/api/runs").status_code == 401


def test_login_slowdown_and_lockout(make, monkeypatch):
    client = make(ACCESS_PASSWORD=PASSWORD)
    app = client.app
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr("worker.deploy.asyncio.sleep", fake_sleep)
    for _ in range(app.state.access.PER_IP_LOCKOUT):
        assert client.post("/login", data={"password": "bad"}).status_code == 401
    assert delays == sorted(delays) and delays[-1] > delays[0]  # each failure waits longer
    blocked = client.post("/login", data={"password": PASSWORD})  # even the right password is refused during lockout
    assert blocked.status_code == 429


def test_next_target_is_same_site_only():
    assert safe_next("/erp/bills?x=1") == "/erp/bills?x=1"
    for bad in ("//evil.example", "https://evil.example", "evil", "/\\evil", None, ""):
        assert safe_next(bad) == "/"


def test_health_public_but_minimal_when_protected(make):
    client = make(ACCESS_PASSWORD=PASSWORD)
    anon = client.get("/api/health")
    assert anon.status_code == 200 and anon.json() == {"status": "ok"}
    client.post("/login", data={"password": PASSWORD})
    full = client.get("/api/health").json()
    assert full["status"] == "ok" and "provider" in full and "auth_enabled" in full


def test_cross_site_post_is_refused_when_authenticated(make):
    client = make(ACCESS_PASSWORD=PASSWORD)
    client.post("/login", data={"password": PASSWORD})
    resp = client.post("/api/runs", json={"task": "x"}, headers={"sec-fetch-site": "cross-site"})
    assert resp.status_code == 403


# --------------------------------------------------------------------------- reverse proxy


def test_proxy_forwards_method_query_body_and_cookies(make):
    client = make(PUBLIC_SIM_PROXY=True)
    got = client.post("/erp/echo?a=1&b=two", content=b"payload", headers={"cookie": "erp_session=abc; theme=dark"}).json()
    assert got["method"] == "POST" and got["query"] == "a=1&b=two" and got["body"] == "payload"
    assert got["cookie"] == "erp_session=abc; theme=dark"
    assert got["host"] == "sim.internal:8001"  # the sim sees its own origin, not the public host
    assert client.get("/erp/echo?x=%20y").json()["query"] == "x=%20y"
    assert client.get("/company").text == "sim landing"
    assert client.get("/company/").text == "sim landing"
    assert client.get("/static/style.css").headers["content-type"].startswith("text/css")


def test_proxy_passes_set_cookie_and_rewrites_location(make):
    client = make(PUBLIC_SIM_PROXY=True)
    login = client.get("/erp/login")
    cookies = login.headers.get_list("set-cookie")
    assert len(cookies) == 2 and any(c.startswith("erp_session=abc") for c in cookies) and any(c.startswith("second=two") for c in cookies)

    redirect = client.get("/erp")
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/erp/bills?page=2"  # internal origin stripped
    assert client.get("/erp/away").headers["location"] == "https://example.org/elsewhere"  # foreign targets untouched
    assert rewrite_location("http://localhost:8001/erp/", "http://127.0.0.1:8001") == "/erp/"
    assert rewrite_location("/erp/bills", UPSTREAM) == "/erp/bills"


def test_proxy_blocks_admin(make):
    client = make(PUBLIC_SIM_PROXY=True)
    for path in ("/admin/chaos", "/admin/reset", "/admin", "/admin/state"):
        for method in ("get", "post"):
            resp = getattr(client, method)(path)
            assert resp.status_code == 404 and "secret" not in resp.text


def test_proxy_reports_unreachable_sim(tmp_path):
    settings = Settings(LLM_PROVIDER="fake", RUNS_DIR=str(tmp_path / "runs"), SIM_BASE_URL="http://127.0.0.1:9", PUBLIC_SIM_PROXY=True)
    app = create_app(settings, registry_factory=empty_registry)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    app.state.sim_transport = httpx.MockTransport(refuse)
    with TestClient(app) as client:
        assert client.get("/erp/bills").status_code == 502


def test_proxy_sits_behind_the_password_and_hides_the_demo_cookie(make):
    client = make(PUBLIC_SIM_PROXY=True, ACCESS_PASSWORD=PASSWORD)
    assert client.get("/erp/login").status_code == 303
    assert client.post("/erp/echo", content=b"x").status_code == 401
    assert client.get("/company").status_code == 303
    client.post("/login", data={"password": PASSWORD})
    echoed = client.get("/erp/echo", headers={"cookie": f"ca_auth={client.cookies['ca_auth']}; erp_session=abc"}).json()
    assert "ca_auth" not in echoed["cookie"] and "erp_session=abc" in echoed["cookie"]
    assert client.get("/company").text == "sim landing"


def test_proxy_carries_a_tenant_entry_link_and_its_cookie_to_the_real_sim(make, tmp_path, monkeypatch):
    from company_sim import config as sim_config
    from company_sim import tenants
    from company_sim.app import app as real_sim

    monkeypatch.setattr(sim_config, "DATA_DIR", tmp_path / "simdata")
    monkeypatch.setattr(sim_config, "DB_PATH", tmp_path / "simdata" / "sim.db")
    monkeypatch.setattr(sim_config, "PDF_DIR", tmp_path / "simdata" / "pdfs")
    tenant = tenants.provision_tenant("proxied")
    client = make(PUBLIC_SIM_PROXY=True, ACCESS_PASSWORD=PASSWORD)
    client.app.state.sim_transport = httpx.ASGITransport(app=real_sim)  # the real simulator instead of the fake
    assert client.get(tenant.entry_path).status_code == 303  # the demo password gate still comes first
    client.post("/login", data={"password": PASSWORD})

    entry = client.get(tenant.entry_path)
    assert entry.status_code == 303 and entry.headers["location"] == "/"
    assert f"sim_tenant={tenant.token}" in entry.headers["set-cookie"] and client.cookies["sim_tenant"] == tenant.token
    assert client.get("/t/not-a-token/").status_code == 404
    # the browser now talks to the tenant: its own login works, the default tenant's does not
    assert "Invalid username" in client.post("/erp/login", data={"username": sim_config.ERP_USER,
                                                                 "password": sim_config.ERP_PASSWORD}).text
    login = client.post("/erp/login", data=tenant.credentials["erp"])
    assert login.status_code == 303 and login.headers["location"] == "/erp/bills"
    assert "Company X Ltd" in client.get("/erp/bills").text
    assert "ca_auth" in client.cookies and "sim_tenant" in client.cookies  # the gate cookie is separate and still ours


# --------------------------------------------------------------------------- run cap, flags


def quota_llm(_settings):
    def responder(messages, tools):
        raise LLMError("All configured Gemini models are out of free-tier quota for today (tried: a, b)")
    return ScriptedLLM(responder)


def wait_idle(client: TestClient, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = client.get("/api/runs").json()
        if runs and all(r["status"] in ("succeeded", "failed", "needs_attention", "cancelled") for r in runs):
            return
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_daily_run_cap_returns_429(make):
    client = make(MAX_RUNS_PER_DAY=1, llm_factory=quota_llm)
    assert client.get("/api/config").json()["runs_left_today"] == 1
    first = client.post("/api/runs", json={"task": "one"})
    assert first.status_code == 200
    wait_idle(client)
    second = client.post("/api/runs", json={"task": "two"})
    assert second.status_code == 429
    assert "limit" in second.json()["detail"].lower() and int(second.headers["retry-after"]) > 0
    cfg = client.get("/api/config").json()
    assert cfg["runs_left_today"] == 0 and cfg["max_runs_per_day"] == 1

    # The finished run explains itself in its report, which the dashboard turns into a quota notice.
    report = client.get(f"/api/runs/{first.json()['run_id']}").json()["report"]
    assert "out of free-tier quota" in report["summary"]


def test_unlimited_by_default(make):
    client = make()
    cfg = client.get("/api/config").json()
    assert cfg == {"deploy_mode": False, "sim_proxy": False, "auth_enabled": False, "max_runs_per_day": None, "runs_left_today": None}


def test_cap_counter_rolls_over_at_utc_midnight():
    cap = DailyRunCap(2)
    cap.record(); cap.record()
    assert not cap.allow() and cap.remaining() == 0
    cap._day = cap._day.replace(year=cap._day.year - 1)  # pretend the counter belongs to an earlier day
    assert cap.allow() and cap.remaining() == 2
    assert DailyRunCap(0).allow() and DailyRunCap(0).remaining() is None


def test_deploy_mode_flags_and_forced_headless(make):
    seen = {}

    def spy(settings):
        seen["headless"] = settings.HEADLESS
        return ScriptedLLM(lambda m, t: (_ for _ in ()).throw(LLMError("stop")))

    client = make(DEPLOY_MODE=True, PUBLIC_SIM_PROXY=True, MAX_RUNS_PER_DAY=15, HEADLESS=False, llm_factory=spy)
    cfg = client.get("/api/config").json()
    assert cfg["deploy_mode"] is True and cfg["sim_proxy"] is True and cfg["max_runs_per_day"] == 15
    assert client.get("/api/health").json()["headless"] is True
    assert client.post("/api/runs", json={"task": "x", "headed": True}).status_code == 200
    assert seen["headless"] is True  # a client asking for a headed browser is ignored in server mode
    wait_idle(client)


def test_dashboard_has_the_deploy_hooks():
    ui = Path(__file__).resolve().parent.parent / "worker" / "ui"
    html = (ui / "index.html").read_text(encoding="utf-8")
    for element in ('id="company-link"', 'id="headed-switch"', 'id="live-btn"', 'id="live-img"'):
        assert element in html
    js = (ui / "app.js").read_text(encoding="utf-8")
    assert 'location.protocol === "https:"' in js and "/login" in js
