"""Tests for the tool framework and the files / http tools."""
from __future__ import annotations

import json

import httpx
import pytest
from reportlab.pdfgen import canvas

from worker.config import Settings
from worker.tools import retry as retry_module
from worker.tools.base import Risk, Tool, ToolContext, ToolResult
from worker.tools.files import ListFilesTool, ReadFileTool, WriteFileTool, resolve_path
from worker.tools.http import HttpRequestTool
from worker.tools.registry import ToolRegistry
from worker.tools.retry import execute_with_retry


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(retry_module, "BACKOFF_BASE_S", 0.0)


@pytest.fixture
def ctx(tmp_path) -> ToolContext:
    workspace = tmp_path / "workspace"
    run_dir = tmp_path / "runs" / "r1"
    workspace.mkdir()
    (run_dir / "downloads").mkdir(parents=True)
    return ToolContext(run_id="r1", run_dir=run_dir, workspace=workspace, settings=Settings(_env_file=None))


class ScriptedTool(Tool):
    """Tool whose run() pops queued outcomes (ToolResult or Exception)."""

    name = "scripted"
    description = "test tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, outcomes, max_retries=2):
        self.outcomes = list(outcomes)
        self.max_retries = max_retries
        self.runs = 0

    async def run(self, args, ctx):
        self.runs += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# ---------------------------------------------------------------- registry


def test_registry_subset_only_read_only_safe_tools():
    reg = ToolRegistry()
    for tool in (ListFilesTool(), ReadFileTool(), WriteFileTool(), HttpRequestTool()):
        reg.register(tool)
    sub = reg.subset(read_only=True)
    assert sub.names() == ["list_files", "read_file", "http_request"]
    assert "write_file" in reg and "write_file" not in sub
    assert [s.name for s in reg.specs(["read_file", "missing"])] == ["read_file"]
    assert reg.get("write_file").spec().parameters["required"] == ["path", "content"]
    with pytest.raises(ValueError):
        reg.register(ReadFileTool())


# ---------------------------------------------------------------- execute_with_retry


async def test_transient_failure_retried_then_succeeds(ctx):
    tool = ScriptedTool([ToolResult(False, "boom", "timeout", transient=True), ToolResult(False, "boom", "x", transient=True), ToolResult(True, "fine")])
    result = await execute_with_retry(tool, {}, ctx)
    assert result.ok and tool.runs == 3
    assert "after 2 automatic retries" in result.observation


async def test_non_transient_failure_not_retried(ctx):
    tool = ScriptedTool([ToolResult(False, "validation failed", "invalid", transient=False)])
    result = await execute_with_retry(tool, {}, ctx)
    assert not result.ok and tool.runs == 1 and "retr" not in result.observation


async def test_transient_retries_are_capped(ctx):
    tool = ScriptedTool([ToolResult(False, "down", "503", transient=True)] * 5, max_retries=1)
    result = await execute_with_retry(tool, {}, ctx)
    assert not result.ok and tool.runs == 2 and "still failing after 1 automatic retry" in result.observation


async def test_exceptions_become_results_with_transient_heuristic(ctx):
    timeout = ScriptedTool([TimeoutError("took too long"), ToolResult(True, "ok")])
    assert (await execute_with_retry(timeout, {}, ctx)).ok and timeout.runs == 2

    boom = ScriptedTool([ValueError("bad input")])
    result = await execute_with_retry(boom, {}, ctx)
    assert not result.ok and not result.transient and boom.runs == 1
    assert "ValueError: bad input" in result.error

    req = httpx.Request("GET", "http://x")
    server_err = httpx.HTTPStatusError("boom", request=req, response=httpx.Response(502, request=req))
    assert retry_module.is_transient_exception(server_err)
    client_err = httpx.HTTPStatusError("nope", request=req, response=httpx.Response(404, request=req))
    assert not retry_module.is_transient_exception(client_err)
    assert retry_module.is_transient_exception(RuntimeError("Page.click: Timeout 30000ms exceeded"))


# ---------------------------------------------------------------- files


def make_pdf(path, pages):
    c = canvas.Canvas(str(path))
    for text in pages:
        c.drawString(72, 720, text)
        c.showPage()
    c.save()


async def test_file_sandbox_rejects_escapes(ctx, tmp_path):
    (tmp_path / "secret.txt").write_text("top secret")
    read = ReadFileTool()
    for bad in ("../secret.txt", "../../secret.txt", "downloads/../../../secret.txt", str(tmp_path / "secret.txt"), "sub/../../secret.txt"):
        with pytest.raises(ValueError, match="outside the allowed"):
            await read.run({"path": bad}, ctx)
    with pytest.raises(ValueError):
        resolve_path("../escape.txt", ctx, for_write=True)
    # through execute_with_retry the escape becomes a clean non-transient failure
    result = await execute_with_retry(read, {"path": "../secret.txt"}, ctx)
    assert not result.ok and not result.transient and "outside the allowed" in result.observation


async def test_write_read_list_roundtrip_and_downloads_are_read_only(ctx):
    write, read, ls = WriteFileTool(), ReadFileTool(), ListFilesTool()
    assert write.classify({}) is Risk.WRITE and not write.read_only_safe
    res = await write.run({"path": "notes/a.txt", "content": "hello world"}, ctx)
    assert res.ok and (ctx.workspace / "notes" / "a.txt").read_bytes() == b"hello world"

    assert (await read.run({"path": "notes/a.txt"}, ctx)).observation == "hello world"
    short = await read.run({"path": "notes/a.txt", "max_chars": 5}, ctx)
    assert short.observation.startswith("hello") and short.data["truncated"]

    (ctx.run_dir / "downloads" / "inv.csv").write_bytes(b"a,b\n1,2\n")
    listing = (await ls.run({"path": "."}, ctx)).observation
    assert "notes/" in listing and "downloads/" in listing
    assert "downloads/inv.csv" in (await ls.run({"path": "downloads"}, ctx)).observation
    assert (await read.run({"path": "downloads/inv.csv"}, ctx)).observation == "a,b\n1,2\n"

    with pytest.raises(ValueError, match="read-only"):
        await write.run({"path": "downloads/x.txt", "content": "no"}, ctx)

    missing = await read.run({"path": "nope.txt"}, ctx)
    assert not missing.ok and missing.error == "not_found"


async def test_read_pdf_extracts_text_with_page_markers(ctx):
    make_pdf(ctx.run_dir / "downloads" / "invoice.pdf", ["Invoice INV-1001 Amount 1234.50", "Due date 2026-11-30"])
    result = await ReadFileTool().run({"path": "downloads/invoice.pdf"}, ctx)
    assert result.ok
    assert "--- page 1 of 2 ---" in result.observation and "--- page 2 of 2 ---" in result.observation
    assert "INV-1001" in result.observation and "2026-11-30" in result.observation


# ---------------------------------------------------------------- http


def test_http_classify_and_describe():
    tool = HttpRequestTool()
    assert tool.classify({"method": "GET"}) is Risk.READ
    assert tool.classify({"method": "head"}) is Risk.READ
    assert tool.classify({"method": "POST"}) is Risk.WRITE
    assert tool.classify({"method": "PUT"}) is Risk.WRITE
    assert tool.classify({"method": "PATCH"}) is Risk.WRITE
    assert tool.classify({"method": "DELETE"}) is Risk.IRREVERSIBLE
    assert tool.read_only_safe
    assert tool.describe_action({"method": "post", "url": "http://localhost/x", "json_body": {"a": 1}}) == 'POST http://localhost/x with body {"a": 1}'


async def test_http_allowlist_and_read_only_mode(ctx):
    tool = HttpRequestTool()
    blocked = await tool.run({"method": "GET", "url": "https://evil.example.com/x"}, ctx)
    assert not blocked.ok and blocked.error == "host_not_allowed"
    ctx.extras["read_only"] = True
    denied = await tool.run({"method": "POST", "url": "http://localhost:8001/api/bills"}, ctx)
    assert not denied.ok and denied.error == "read_only_mode"


async def test_http_request_formats_json_and_flags_5xx_transient(ctx, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/boom":
            return httpx.Response(503, text="unavailable")
        if request.url.path == "/missing":
            return httpx.Response(404, json={"detail": "nope"})
        return httpx.Response(200, json={"bills": [1, 2], "q": request.url.params.get("q")})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr("worker.tools.http.httpx.AsyncClient", lambda **kw: real_client(transport=transport, **kw))

    tool = HttpRequestTool()
    ok = await tool.run({"method": "GET", "url": "http://localhost:8001/api/bills", "query": {"q": "acme"}}, ctx)
    assert ok.ok and ok.data["status"] == 200
    assert json.loads(ok.observation.split("\n", 1)[1]) == {"bills": [1, 2], "q": "acme"}

    boom = await tool.run({"method": "GET", "url": "http://localhost:8001/boom"}, ctx)
    assert not boom.ok and boom.transient
    missing = await tool.run({"method": "GET", "url": "http://localhost:8001/missing"}, ctx)
    assert not missing.ok and not missing.transient and "nope" in missing.observation


def _mock_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr("worker.tools.http.httpx.AsyncClient", lambda **kw: real_client(transport=transport, **kw))


async def test_http_get_5xx_is_retried(ctx, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(503, text="unavailable") if len(calls) < 3 else httpx.Response(200, json={"ok": 1})

    _mock_http(monkeypatch, handler)
    result = await execute_with_retry(HttpRequestTool(), {"method": "GET", "url": "http://localhost:8001/api/bills"}, ctx)
    assert result.ok and len(calls) == 3 and "after 2 automatic retries" in result.observation


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_http_non_idempotent_5xx_is_not_retried(ctx, monkeypatch, method):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(500, text="partial write then crash")

    _mock_http(monkeypatch, handler)
    result = await execute_with_retry(
        HttpRequestTool(), {"method": method, "url": "http://localhost:8001/api/bills", "json_body": {"a": 1}}, ctx
    )
    assert calls == [method]
    assert not result.ok and not result.transient and result.error == "HTTP 500"
    assert "NOT retried automatically" in result.observation
    assert "check whether the record exists (GET) before retrying" in result.observation
    assert "automatic retr" not in result.observation.replace("NOT retried automatically", "")


async def test_http_non_idempotent_network_error_is_not_retried(ctx, monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        raise httpx.ReadTimeout("timed out", request=request)

    _mock_http(monkeypatch, handler)
    post = await execute_with_retry(HttpRequestTool(), {"method": "POST", "url": "http://localhost:8001/api/bills"}, ctx)
    assert calls == ["POST"] and not post.ok and not post.transient
    assert "NOT retried automatically" in post.observation

    calls.clear()
    get = await execute_with_retry(HttpRequestTool(), {"method": "GET", "url": "http://localhost:8001/api/bills"}, ctx)
    assert len(calls) == 3 and not get.ok and "still failing after 2 automatic retries" in get.observation
