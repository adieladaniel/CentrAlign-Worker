"""Observability: structured logs (context, redaction), Prometheus metrics, /metrics access, health endpoints and the
admin system summary (``worker/logging_setup.py``, ``metrics.py``, ``observability.py``, ``system_api.py``, ``health.py``).

Logging and file-mode tests need no database; the multi-user ones use the test database (skipped without PostgreSQL).
Metrics are process-global, so every assertion compares a series before and after what the test did.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
import uuid
from pathlib import Path

import httpx
import pytest
from dbapp import (
    Gates,
    Planner,
    blocked,
    call,
    finished,
    make_settings,
    new_client,
    registry_with,
    reply,
    sign_in,
    start_run,
    ws_connect,
)
from fastapi import Depends
from sqlalchemy import text

from worker import metrics
from worker.agent.broker import AutoBroker
from worker.agent.runner import AgentRunner
from worker.config import Settings
from worker.db import migrate
from worker.db.session import make_engine, make_sessionmaker
from worker.llm.base import LLMResponse, ToolCall, Usage
from worker.llm.fake import ScriptedLLM
from worker.llm.resilient import ResilientLLM
from worker.logging_setup import (
    clear_secrets,
    current_context,
    log_context,
    redact_text,
    register_run_redactor,
    register_secret,
    setup_logging,
)
from worker.observability import ObservabilityMiddleware
from worker.redaction import Redactor
from worker.server import create_app
from worker.system_api import queue_depths, sample_queue_depth
from worker.tools.base import Risk, Tool, ToolResult
from worker.tools.control import FinishTool, UpdatePlanTool
from worker.tools.registry import ToolRegistry
from worker.vault import Credential, VaultScope

DB_PASSWORD = "Sup3rS3cret!pw"
DB_URL = f"postgresql+asyncpg://postgres:{DB_PASSWORD}@localhost:5432/oneforall"
ERP_PASSWORD = "acme-erp-secret-81"  # the ERP login of the acme test org (tests/dbapp.py)


# --------------------------------------------------------------------------- helpers


def counter(name: str, **labels: str) -> float:
    return metrics.sample(name, **labels)


class Delta:
    """Series values at construction time; ``after`` is how much each grew since."""

    def __init__(self, *series: tuple[str, dict[str, str]]):
        self.series = series
        self.before = [counter(name, **labels) for name, labels in series]

    def grew(self) -> list[float]:
        return [counter(name, **labels) - before for (name, labels), before in zip(self.series, self.before)]


@pytest.fixture
def logs():
    """Install the real logging setup writing to a string buffer; undo it afterwards."""
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    clear_secrets()
    buffers: list[io.StringIO] = []

    def install(fmt: str = "json", **overrides) -> io.StringIO:
        buf = io.StringIO()
        buffers.append(buf)
        settings = Settings(_env_file=None, LOG_FORMAT=fmt, LOG_LEVEL="DEBUG", **overrides)
        setup_logging(settings, stream=buf, adopt_uvicorn=False)
        return buf

    yield install
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    clear_secrets()


def json_lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def file_settings(tmp_path: Path, **overrides) -> Settings:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    values = dict(_env_file=None, AUTH_MODE="file", LLM_PROVIDER="fake", RUNS_DIR=str(tmp_path / "runs"),
                  WORKSPACE_DIR=str(tmp_path / "workspace"), SIM_BASE_URL="http://127.0.0.1:9", LLM_MIN_INTERVAL_S=0.0,
                  READINESS_CHECK_SIM=False)
    values.update(overrides)
    return Settings(**values)


def small_registry(_settings: Settings) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (EchoTool(), BoomTool(), SaveTool(), UpdatePlanTool(), FinishTool()):
        registry.register(tool)
    return registry


class EchoTool(Tool):
    name = "echo"
    description = "Echo."
    parameters = {"type": "object", "properties": {}}

    async def run(self, args, ctx):
        return ToolResult(True, "echo")


class BoomTool(Tool):
    name = "boom"
    description = "Always fails."
    parameters = {"type": "object", "properties": {}}

    async def run(self, args, ctx):
        return ToolResult(False, "it failed", error="boom")


class SaveTool(Tool):
    name = "save"
    description = "A write."
    parameters = {"type": "object", "properties": {}}
    risk = Risk.WRITE
    read_only_safe = False

    async def run(self, args, ctx):
        return ToolResult(True, "saved")


def tc(name: str, **args) -> ToolCall:
    return ToolCall(id=f"c_{uuid.uuid4().hex[:8]}", name=name, args=args)


def say(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(text="", tool_calls=list(calls), usage=Usage(10, 5))


def finish_blocked() -> LLMResponse:
    return say(tc("finish", status="blocked", summary="Nothing more to do."))


async def file_app_client(tmp_path: Path, **overrides) -> httpx.AsyncClient:
    app = create_app(file_settings(tmp_path, **overrides), llm_factory=lambda s: ScriptedLLM([finish_blocked()]),
                     registry_factory=small_registry)
    return new_client(app)


# --------------------------------------------------------------------------- structured logs


def test_json_lines_have_the_fixed_fields_and_the_bound_context(logs):
    buf = logs("json")
    with log_context(run_id="r" * 32, org_id="org-1", user_id="user-1", worker_id="w-1", request_id="req-1"):
        logging.getLogger("worker.test").info("hello %s", "world", extra={"step": 3})
    logging.getLogger("worker.test").warning("outside")
    inside, outside = json_lines(buf)
    assert {"ts", "level", "logger", "msg"} <= set(inside)
    assert (inside["level"], inside["logger"], inside["msg"], inside["step"]) == ("INFO", "worker.test", "hello world", 3)
    assert (inside["run_id"], inside["org_id"], inside["user_id"], inside["worker_id"], inside["request_id"]) == (
        "r" * 32, "org-1", "user-1", "w-1", "req-1")
    assert inside["ts"].endswith("Z")
    assert not {"run_id", "org_id", "request_id"} & set(outside)  # the context ended with the block


def test_text_format_is_the_classic_line_with_the_context_appended(logs):
    buf = logs("text")
    with log_context(run_id="abcdef0123456789", worker_id="local-1"):
        logging.getLogger("worker.test").info("started")
    logging.getLogger("worker.test").info("plain")
    first, second = buf.getvalue().splitlines()
    assert " INFO worker.test: started [run=abcdef01 worker=local-1]" in first
    assert second.endswith(" INFO worker.test: plain")


def test_the_default_format_follows_deploy_mode():
    from worker.logging_setup import resolve_format

    assert resolve_format(Settings(_env_file=None)) == "text"
    assert resolve_format(Settings(_env_file=None, DEPLOY_MODE=True)) == "json"
    assert resolve_format(Settings(_env_file=None, DEPLOY_MODE=True, LOG_FORMAT="text")) == "text"


def test_secrets_never_reach_a_log_handler_or_caplog(logs, caplog):
    buf = logs("json", DATABASE_URL=DB_URL, GEMINI_API_KEY="AIzaSyD-not-a-real-key-0123456789abcdef", METRICS_TOKEN="scrape-token-xyz")
    log = logging.getLogger("worker.secrets")
    caplog.set_level(logging.DEBUG)
    key = "AIzaSyD-not-a-real-key-0123456789abcdef"
    log.error("cannot connect to %s", DB_URL)  # the password comes in as an argument
    log.error(f"asyncpg failed for {DB_URL}")  # ... and pre-formatted
    log.info("headers %s", {"X-API-Key": "k-12345-abcdef", "Authorization": "Bearer abc.def.ghi-123456", "x": "ok"})
    log.info("Cookie: ofa_session=SESSIONTOKEN123; theme=dark")
    log.info("sending Authorization: Bearer tok-9f8e7d6c5b4a and password=hunter2hunter2 and key %s", key)
    log.info("scraper used scrape-token-xyz")
    try:
        raise RuntimeError(f"connect failed: {DB_URL}")
    except RuntimeError:
        log.exception("boom")
    for text_ in (buf.getvalue(), caplog.text):
        for secret in (DB_PASSWORD, "k-12345-abcdef", "abc.def.ghi-123456", "SESSIONTOKEN123", "tok-9f8e7d6c5b4a",
                       "hunter2hunter2", key, "scrape-token-xyz"):
            assert secret not in text_, secret
    assert "postgresql+asyncpg://postgres:[REDACTED]@localhost:5432/oneforall" in buf.getvalue()
    assert "ok" in buf.getvalue()  # harmless values survive
    exc_lines = [line for line in json_lines(buf) if line["msg"] == "boom"]
    assert "RuntimeError" in exc_lines[0]["exc"] and DB_PASSWORD not in exc_lines[0]["exc"]


def test_redaction_is_exact_for_registered_values_and_run_secrets(logs):
    buf = logs("json")
    register_secret("my-very-own-secret")
    register_run_redactor("run-1", Redactor({"erp.password": "pa55w0rd-of-erp"}))
    logging.getLogger("t").info("a my-very-own-secret b pa55w0rd-of-erp c")
    assert redact_text("pa55w0rd-of-erp") == "[REDACTED:erp.password]"
    line = json_lines(buf)[0]
    assert "my-very-own-secret" not in line["msg"] and "pa55w0rd-of-erp" not in line["msg"]


async def test_a_runs_vault_secrets_are_masked_in_logs_only_while_it_runs(logs, tmp_path):
    buf = logs("json")
    secret = "Tr0ub4dor&3-vault"
    seen: list[str] = []

    class ChattyTool(Tool):
        name = "chatty"
        description = "Logs something sensitive."
        parameters = {"type": "object", "properties": {}}

        async def run(self, args, ctx):
            logging.getLogger("worker.tools.chatty").warning("logging in with %s", secret)
            seen.append(redact_text(secret))
            return ToolResult(True, "done")

    registry = ToolRegistry()
    for tool in (ChattyTool(), FinishTool()):
        registry.register(tool)
    scope = VaultScope("default", [Credential("default", "erp", "login", {"username": "ap", "password": secret})])
    llm = ScriptedLLM([say(tc("chatty")), finish_blocked()])
    runner = AgentRunner("log something", llm, registry, file_settings(tmp_path), AutoBroker.approve_all(), vault_scope=scope)
    state = await runner.run()
    lines = json_lines(buf)
    chatty = next(line for line in lines if line["logger"] == "worker.tools.chatty")
    assert secret not in buf.getvalue()
    assert "[REDACTED:erp.password]" in chatty["msg"]
    assert chatty["run_id"] == state.run_id  # the run's id is on every line written during the run
    logging.getLogger("after").info("the run is over: %s", secret)  # no longer a run secret...
    assert secret in buf.getvalue().splitlines()[-1]  # ... so only the generic patterns apply


# --------------------------------------------------------------------------- metrics of a scripted run


async def test_a_scripted_run_produces_the_expected_metrics(tmp_path):
    llm = ResilientLLM(ScriptedLLM([
        say(tc("echo")), say(tc("boom")), say(tc("save")), finish_blocked()]), min_interval_s=0.0)
    tools = [("oneforall_tool_calls_total", {"tool": "echo", "ok": "true"}),
             ("oneforall_tool_calls_total", {"tool": "boom", "ok": "false"}),
             ("oneforall_tool_calls_total", {"tool": "save", "ok": "true"}),
             ("oneforall_tool_duration_seconds_count", {"tool": "echo"}),
             ("oneforall_runs_total", {"status": "needs_attention"}),
             ("oneforall_run_duration_seconds_count", {}),
             ("oneforall_llm_calls_total", {"model": "fake", "outcome": "ok"}),
             ("oneforall_llm_tokens_total", {"model": "fake", "direction": "input"}),
             ("oneforall_llm_tokens_total", {"model": "fake", "direction": "output"}),
             ("oneforall_llm_latency_seconds_count", {"model": "fake"}),
             ("oneforall_llm_ratelimit_wait_seconds_count", {}),
             ("oneforall_approvals_total", {"kind": "approval", "outcome": "approved"}),
             ("oneforall_approval_wait_seconds_count", {})]
    watch = Delta(*tools)
    active_before = counter("oneforall_runs_active")

    runner = AgentRunner("metrics please", llm, small_registry(None), file_settings(tmp_path), AutoBroker.approve_all())
    state = await runner.run()

    assert state.status == "needs_attention"
    assert watch.grew() == [1, 1, 1, 1, 1, 1, 4, 40, 20, 4, 4, 1, 1]
    assert counter("oneforall_runs_active") == active_before  # the gauge went up and came back down


async def test_denied_answered_and_expired_approvals_get_their_own_outcome(tmp_path):
    denied = Delta(("oneforall_approvals_total", {"kind": "approval", "outcome": "denied"}))
    deny = AutoBroker(lambda kind, payload: {"approved": False})
    await AgentRunner("deny", ScriptedLLM([say(tc("save")), finish_blocked()]), small_registry(None),
                      file_settings(tmp_path), deny).run()
    assert denied.grew() == [1]

    answered = Delta(("oneforall_approvals_total", {"kind": "question", "outcome": "answered"}))
    asker = AutoBroker(lambda kind, payload: {"answer": "go on"})
    registry = small_registry(None)
    from worker.tools.interaction import AskUserTool

    registry.register(AskUserTool())
    await AgentRunner("ask", ScriptedLLM([say(tc("ask_user", question="Which one?")), finish_blocked()]), registry,
                      file_settings(tmp_path), asker).run()
    assert answered.grew() == [1]

    class Silent(AutoBroker):  # nobody answers before the broker's own timeout
        async def request(self, kind, payload, **kwargs):
            self.last_expired, self.no_response_reason = True, "approval expired after 1 min"
            return None

    expired = Delta(("oneforall_approvals_total", {"kind": "approval", "outcome": "expired"}))
    state = await AgentRunner("expire", ScriptedLLM([say(tc("save"))]), small_registry(None), file_settings(tmp_path),
                              Silent()).run()
    assert state.status == "needs_attention" and expired.grew() == [1]


async def test_a_run_that_fails_to_use_the_llm_counts_the_error(tmp_path):
    from worker.llm.base import LLMError, RateLimitError

    class Flaky(ScriptedLLM):
        calls_made = 0

        async def complete(self, *args, **kwargs):
            Flaky.calls_made += 1
            if Flaky.calls_made == 1:
                raise RateLimitError("slow down", retry_after=0)
            if Flaky.calls_made == 2:
                raise LLMError("broken")
            return await super().complete(*args, **kwargs)

    watch = Delta(("oneforall_llm_calls_total", {"model": "fake", "outcome": "rate_limited"}),
                  ("oneforall_llm_calls_total", {"model": "fake", "outcome": "error"}),
                  ("oneforall_llm_calls_total", {"model": "fake", "outcome": "ok"}))
    llm = ResilientLLM(Flaky([finish_blocked()]), min_interval_s=0.0, base_delay_s=0.0, max_delay_s=0.0)
    with pytest.raises(LLMError):
        await llm.complete([], [])  # attempt 1: rate limited, retried; attempt 2: a hard error
    assert watch.grew() == [1, 1, 0]


def test_run_observer_excludes_human_waiting_from_the_duration():
    clock = iter([0.0, 10.0, 70.0, 100.0])  # begin, wait start, wait end, finish
    observer = metrics.RunObserver(lambda: next(clock))
    watch = Delta(("oneforall_run_duration_seconds_sum", {}), ("oneforall_run_duration_seconds_count", {}))
    observer.begin()
    observer.wait_started()
    assert observer.wait_ended() == 60.0
    observer.finished("succeeded")
    observer.finished("failed")  # only the first final status counts
    observer.close()
    observer.close()
    assert watch.grew() == [40.0, 1]


def test_queue_depth_gauge_caps_its_label_cardinality():
    metrics.set_queue_depth({})  # purge what other tests left on the gauge
    metrics.set_queue_depth({})
    metrics.set_queue_depth({f"org-{n:03d}": n + 1 for n in range(60)})
    shown = {s.labels["org"]: s.value for m in metrics.QUEUE_DEPTH.collect() for s in m.samples}
    assert len(shown) == metrics.MAX_ORG_LABELS + 1 and "_other" in shown
    assert shown["org-059"] == 60 and shown["_other"] == sum(range(1, 11))
    metrics.set_queue_depth({})
    assert {s.value for m in metrics.QUEUE_DEPTH.collect() for s in m.samples} == {0.0}  # emptied: reported once as 0
    metrics.set_queue_depth({})
    assert not [s for m in metrics.QUEUE_DEPTH.collect() for s in m.samples]  # ... then dropped


def test_a_worker_can_serve_its_metrics_on_loopback():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    stop = metrics.serve_metrics(port)
    try:
        body = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5).text
    finally:
        stop()
    assert "oneforall_runs_total" in body and "oneforall_http_requests_total" in body


async def test_browser_pool_reports_contexts_and_relaunches(browser_pool):
    contexts = Delta(("oneforall_browser_contexts_active", {}))
    relaunches = Delta(("oneforall_browser_relaunches_total", {}))
    context = await browser_pool.acquire_context()
    assert contexts.grew() == [1]
    browser = context.browser
    await browser_pool.release(context)
    assert contexts.grew() == [0]
    await browser.close()  # a crash, as far as the pool can tell
    await asyncio.sleep(0.2)
    second = await browser_pool.acquire_context()
    await browser_pool.release(second)
    assert relaunches.grew() == [1]


# --------------------------------------------------------------------------- HTTP: request ids, routes, metrics access


async def test_request_id_is_echoed_or_generated_and_bad_ones_are_replaced(tmp_path):
    client = await file_app_client(tmp_path)
    async with client:
        echoed = await client.get("/api/health", headers={"X-Request-ID": "trace-abc.123"})
        assert echoed.headers["x-request-id"] == "trace-abc.123"
        made = await client.get("/api/health")
        assert len(made.headers["x-request-id"]) == 16
        replaced = await client.get("/api/health", headers={"X-Request-ID": "bad id with spaces"})
        assert replaced.headers["x-request-id"] != "bad id with spaces" and len(replaced.headers["x-request-id"]) == 16
        errored = await client.get("/api/runs/does-not-exist-" + "0" * 8)
        assert errored.headers["x-request-id"]  # also on error responses


async def test_http_metrics_use_route_templates_not_raw_paths(tmp_path):
    client = await file_app_client(tmp_path)
    ids = [uuid.uuid4().hex for _ in range(3)]
    watch = Delta(("oneforall_http_requests_total", {"route": "/api/runs/{run_id}", "method": "GET", "status": "404"}),
                  ("oneforall_http_request_duration_seconds_count", {"route": "/api/runs/{run_id}"}),
                  ("oneforall_http_requests_total", {"route": "unmatched", "method": "GET", "status": "404"}),
                  ("oneforall_http_requests_total", {"route": "/api/health", "method": "GET", "status": "200"}))
    async with client:
        for run_id in ids:
            assert (await client.get(f"/api/runs/{run_id}")).status_code == 404
        assert (await client.get("/definitely/not/a/route/123")).status_code == 404
        await client.get("/api/health")
        body = (await client.get("/metrics")).text
    assert watch.grew() == [3, 3, 1, 1]
    assert not any(run_id in body for run_id in ids) and "definitely" not in body
    labels = {s.labels.get("route") for m in metrics.HTTP_REQUESTS.collect() for s in m.samples}
    assert "/api/runs/{run_id}" in labels and not any(i in route for route in labels for i in ids)


async def test_websocket_gauge_counts_open_connections():
    entered, release = asyncio.Event(), asyncio.Event()

    async def ws_app(scope, receive, send):
        await send({"type": "websocket.accept"})
        entered.set()
        await release.wait()
        await send({"type": "websocket.close", "code": 1000})

    app = ObservabilityMiddleware(ws_app)
    sent: list[dict] = []

    async def receive():
        await asyncio.sleep(3600)

    async def send(message):
        sent.append(message)

    before = counter("oneforall_ws_connections")
    task = asyncio.create_task(app({"type": "websocket", "path": "/x", "headers": []}, receive, send))
    await asyncio.wait_for(entered.wait(), 2)
    assert counter("oneforall_ws_connections") == before + 1
    release.set()
    await asyncio.wait_for(task, 2)
    assert counter("oneforall_ws_connections") == before


async def test_metrics_are_open_only_in_local_development(tmp_path):
    async with await file_app_client(tmp_path) as client:  # no deploy mode, no token, no password
        response = await client.get("/metrics")
        assert response.status_code == 200 and "text/plain" in response.headers["content-type"]
        assert "# TYPE oneforall_runs_total counter" in response.text


async def test_metrics_in_deploy_mode_need_the_token(tmp_path):
    async with await file_app_client(tmp_path, DEPLOY_MODE=True, METRICS_TOKEN="scrape-me-123") as client:
        assert (await client.get("/metrics")).status_code == 401
        assert (await client.get("/metrics", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.get("/metrics", headers={"Authorization": "Basic scrape-me-123"})).status_code == 401
        good = await client.get("/metrics", headers={"Authorization": "Bearer scrape-me-123"})
        assert good.status_code == 200 and "oneforall_runs_total" in good.text
    async with await file_app_client(tmp_path, DEPLOY_MODE=True) as client:  # deployed, no token, no session: closed
        assert (await client.get("/metrics")).status_code == 401
        assert (await client.get("/healthz")).status_code == 200  # liveness stays public


async def test_metrics_with_a_shared_password_accept_the_logged_in_session(tmp_path):
    async with await file_app_client(tmp_path, ACCESS_PASSWORD="open-sesame-1") as client:
        assert (await client.get("/metrics")).status_code == 401  # the password gate does not hide this route; it checks itself
        assert (await client.get("/healthz")).status_code == 200
        login = await client.post("/login", data={"password": "open-sesame-1", "next": "/"})
        assert login.status_code == 303
        assert (await client.get("/metrics")).status_code == 200


async def test_metrics_in_database_mode_need_an_admin_or_the_token(world, db_app):
    app, _ = db_app(METRICS_TOKEN="tok-for-scrapers")
    anonymous = new_client(app)
    admin = await sign_in(app, "admin@acme.test", worker=False)
    operator = await sign_in(app, "operator@acme.test", worker=False)
    assert (await anonymous.get("/metrics")).status_code == 401
    assert (await operator.get("/metrics")).status_code == 403
    assert (await admin.get("/metrics")).status_code == 200
    assert (await anonymous.get("/metrics", headers={"Authorization": "Bearer tok-for-scrapers"})).status_code == 200
    open_app, _ = db_app()  # local development: no token, no deploy mode
    assert (await new_client(open_app).get("/metrics")).status_code == 200


# --------------------------------------------------------------------------- health and readiness


async def test_healthz_is_fast_and_readyz_passes_on_a_healthy_database(world, db_app):
    app, _ = db_app(READINESS_CHECK_SIM=False)
    client = new_client(app)
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "checks": {"database": "ok", "migrations": "ok"}}


async def test_readyz_fails_when_the_schema_is_behind(world, db_app, monkeypatch):
    app, _ = db_app(READINESS_CHECK_SIM=False)
    monkeypatch.setattr(migrate, "head_revision", lambda: "99999999_9999")
    response = await new_client(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready" and "99999999_9999" in response.json()["checks"]["migrations"]


async def test_readyz_fails_when_the_simulated_company_is_unreachable(world, db_app):
    app, _ = db_app(READINESS_CHECK_SIM=True, SIM_BASE_URL="http://127.0.0.1:9")
    response = await new_client(app).get("/readyz")
    assert response.status_code == 503 and response.json()["checks"]["sim"].startswith("unreachable")


async def test_readyz_fails_when_the_database_is_unreachable_but_healthz_still_answers(world, tmp_path):
    bad = make_sessionmaker(make_engine("postgresql+asyncpg://nobody:wrong-pw-123@127.0.0.1:1/nowhere", pool_size=1))
    settings = make_settings(tmp_path, READINESS_CHECK_SIM=False)
    app = create_app(settings, llm_factory=Planner(), vault=world.vault, sessionmaker=bad)
    client = new_client(app)
    assert (await client.get("/healthz")).status_code == 200
    response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready" and body["checks"]["database"].startswith("unreachable")
    assert "wrong-pw-123" not in response.text and "127.0.0.1" not in response.text


async def test_file_mode_readiness_only_checks_the_simulator(tmp_path):
    async with await file_app_client(tmp_path) as client:
        assert (await client.get("/readyz")).json() == {"status": "ready", "checks": {}}
    async with await file_app_client(tmp_path, READINESS_CHECK_SIM=True) as client:
        response = await client.get("/readyz")
        assert response.status_code == 503 and list(response.json()["checks"]) == ["sim"]


# --------------------------------------------------------------------------- context in the multi-user server


async def test_request_logs_carry_request_org_and_user(world, db_app, logs):
    buf = logs("json")
    app, _ = db_app()
    from worker.auth import require_db_role

    @app.get("/_probe")
    async def probe(ctx=Depends(require_db_role("viewer"))):
        logging.getLogger("worker.probe").info("inside the endpoint")
        return {"ok": True}

    operator = await sign_in(app, "operator@acme.test", worker=False)
    response = await operator.get("/_probe", headers={"X-Request-ID": "probe-req-1"})
    assert response.headers["x-request-id"] == "probe-req-1"
    line = next(line for line in json_lines(buf) if line["logger"] == "worker.probe")
    assert line["request_id"] == "probe-req-1"
    assert line["org_id"] == str(world.org_ids["acme"]) and line["user_id"] == str(world.user_ids["operator@acme.test"])
    access = next(line for line in json_lines(buf) if line["logger"] == "worker.http" and line["route"] == "/_probe")
    assert access["status"] == 200 and access["request_id"] == "probe-req-1"
    assert not {"org_id", "user_id", "request_id"} & set(current_context())  # nothing leaked out of the request


class ChattyLoginTool(Tool):
    name = "chatty"
    description = "Logs a vault secret, as a careless library might."
    parameters = {"type": "object", "properties": {}}

    async def run(self, args, ctx):
        logging.getLogger("worker.tools.chatty").info("logging in as ap.clerk with %s", ERP_PASSWORD)
        return ToolResult(True, "ok")


async def test_worker_log_lines_name_the_run_org_user_and_worker_and_hide_vault_secrets(world, db_app, logs):
    buf = logs("json")
    app, planner = db_app(registry_factory=registry_with(Gates(), ChattyLoginTool()))
    planner.add(reply("Chat.", call("chatty")), blocked())
    operator = await sign_in(app, "operator@acme.test")
    run_id = await start_run(operator, "log things")
    await finished(operator, run_id)
    lines = json_lines(buf)
    expected = {"run_id": run_id, "org_id": str(world.org_ids["acme"]),
                "user_id": str(world.user_ids["operator@acme.test"])}
    claimed = next(line for line in lines if line["msg"].startswith("claimed run"))
    chatty = next(line for line in lines if line["logger"] == "worker.tools.chatty")
    for line in (claimed, chatty):
        assert {k: line[k] for k in expected} == expected
        assert line["worker_id"] == app.state.test_worker.worker_id
    assert ERP_PASSWORD not in buf.getvalue() and "[REDACTED:erp.password]" in chatty["msg"]


# --------------------------------------------------------------------------- metrics in database mode


async def test_database_runs_feed_queue_wait_and_run_metrics(world, db_app):
    watch = Delta(("oneforall_queue_wait_seconds_count", {}), ("oneforall_runs_total", {"status": "needs_attention"}),
                  ("oneforall_run_duration_seconds_count", {}))
    app, planner = db_app()
    planner.add(reply("Nothing.", call("list_files")), blocked())
    operator = await sign_in(app, "operator@acme.test")
    await finished(operator, await start_run(operator, "count me"))
    assert watch.grew() == [1, 1, 1]


async def test_queue_depth_is_sampled_per_org(world, db_app):
    app, _ = db_app()
    acme = await sign_in(app, "operator@acme.test", worker=False)  # no worker: runs stay queued
    globex = await sign_in(app, "operator@globex.test", worker=False)
    await start_run(acme, "one")
    await start_run(acme, "two")
    await start_run(globex, "three")
    assert await queue_depths(world.sm) == {"acme": 2, "globex": 1}
    assert await sample_queue_depth(world.sm) == {"acme": 2, "globex": 1}
    assert (counter("oneforall_queue_depth", org="acme"), counter("oneforall_queue_depth", org="globex")) == (2, 1)
    async with world.sm() as session, session.begin():
        await session.execute(text("update runs set status = 'cancelled' where org_id = :o"), {"o": world.org_ids["acme"]})
    await sample_queue_depth(world.sm)
    assert (counter("oneforall_queue_depth", org="acme"), counter("oneforall_queue_depth", org="globex")) == (0, 1)


async def test_a_resumed_run_counts_the_requeue_and_the_resume(world, db_app):
    from dbapp import kill_worker, new_worker

    gates = Gates()
    app, planner = db_app(JOB_LEASE_S=1.5, JOB_POLL_S=0.2, JOB_REAPER_INTERVAL_S=0.2, registry_factory=registry_with(gates))
    planner.add(reply("Wait.", call("hold", gate="never")))
    planner.add(reply("Back.", call("list_files")), blocked())
    app.state.llm_factory = planner
    operator = await sign_in(app, "operator@acme.test", worker=False)
    run_id = await start_run(operator, "survive")
    watch = Delta(("oneforall_runs_requeued_total", {"reason": "worker_lost"}), ("oneforall_runs_resumed_total", {}),
                  ("oneforall_queue_wait_seconds_count", {}))
    doomed = new_worker(app, "w-doomed")
    await doomed.start()
    assert await gates.reached("never")
    await kill_worker(doomed)
    rescuer = new_worker(app, "w-rescuer")
    await rescuer.start()
    await finished(operator, run_id, timeout=20)
    assert watch.grew() == [1, 1, 1]  # one queue wait: the resume claim does not count as waiting


# --------------------------------------------------------------------------- GET /api/admin/system


async def test_system_summary_is_admin_only_and_scoped_to_the_callers_org(world, db_app):
    app, _ = db_app()
    admins = {slug: await sign_in(app, f"admin@{slug}.test", worker=False) for slug in ("acme", "globex")}
    operator = await sign_in(app, "operator@acme.test", worker=False)
    viewer = await sign_in(app, "viewer@acme.test", worker=False)
    await start_run(operator, "one")
    await start_run(operator, "two")
    await start_run(await sign_in(app, "operator@globex.test", worker=False), "three")

    assert (await new_client(app).get("/api/admin/system")).status_code == 401
    assert (await operator.get("/api/admin/system")).status_code == 403
    assert (await viewer.get("/api/admin/system")).status_code == 403

    acme = (await admins["acme"].get("/api/admin/system")).json()
    assert acme["scope"] == "org" and acme["org"]["slug"] == "acme"
    assert [(q["org"], q["queued"]) for q in acme["queue"]] == [("acme", 2)]
    assert acme["totals"] == {"queued": 2, "running": 0, "waiting": 0}
    assert acme["queue"][0]["oldest_queued_s"] is not None
    globex = (await admins["globex"].get("/api/admin/system")).json()
    assert [(q["org"], q["queued"]) for q in globex["queue"]] == [("globex", 1)]
    assert "acme" not in json.dumps(globex)


async def test_platform_admins_see_every_org(world, db_app):
    app, _ = db_app(PLATFORM_ADMIN_EMAILS="Admin@Acme.test")
    acme_admin = await sign_in(app, "admin@acme.test", worker=False)
    globex_admin = await sign_in(app, "admin@globex.test", worker=False)
    await start_run(await sign_in(app, "operator@acme.test", worker=False), "one")
    await start_run(await sign_in(app, "operator@globex.test", worker=False), "two")
    platform = (await acme_admin.get("/api/admin/system")).json()
    assert platform["scope"] == "platform" and platform["org"] is None
    assert {(q["org"], q["queued"]) for q in platform["queue"]} == {("acme", 1), ("globex", 1)}
    assert (await globex_admin.get("/api/admin/system")).json()["scope"] == "org"  # not on the platform list


async def test_system_summary_reports_workers_finished_runs_and_run_time(world, db_app):
    gates = Gates()
    app, planner = db_app(registry_factory=registry_with(gates))
    planner.add(reply("Done quickly.", call("list_files")), blocked())
    planner.add(reply("Hold on.", call("hold", gate="g")), blocked())
    operator = await sign_in(app, "operator@acme.test")
    admin = await sign_in(app, "admin@acme.test", worker=False)
    await finished(operator, await start_run(operator, "quick"))
    held = await start_run(operator, "held")
    assert await gates.reached("g")
    busy = (await admin.get("/api/admin/system")).json()
    assert busy["totals"]["running"] == 1
    assert [w["worker"] for w in busy["workers"]["list"]] == [app.state.test_worker.worker_id] and busy["workers"]["active"] == 1
    assert busy["workers"]["list"][0]["runs"] == 1
    assert busy["finished_24h"]["by_status"] == {"needs_attention": 1}
    assert busy["run_seconds_24h"]["n"] == 1 and busy["run_seconds_24h"]["p50"] is not None
    gates.gate("g").set()
    await finished(operator, held)
    done = (await admin.get("/api/admin/system")).json()
    assert done["finished_24h"] == {"total": 2, "by_status": {"needs_attention": 2}}
    assert done["run_seconds_24h"]["n"] == 2 and done["run_seconds_24h"]["p95"] >= done["run_seconds_24h"]["p50"] >= 0
    assert done["workers"]["active"] == 0


async def test_events_websocket_is_counted_and_released(world, db_app):
    app, planner = db_app()
    planner.add(blocked())
    operator = await sign_in(app, "operator@acme.test")
    run_id = await start_run(operator, "watch me")
    await finished(operator, run_id)
    before = counter("oneforall_ws_connections")
    from dbapp import session_cookie

    result = await ws_connect(app, f"/api/runs/{run_id}/events", cookie=session_cookie(operator))
    assert result.accepted and "replay_done" in result.types
    assert counter("oneforall_ws_connections") == before
