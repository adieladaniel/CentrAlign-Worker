"""Browser pool (one Chromium, an isolated context per run) and cross-run safety of the stateless browser tools."""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import pytest_asyncio

from worker.config import Settings
from worker.tools.base import Risk, ToolContext
from worker.tools.browser import BrowserSession, close_browser, peek_session, register_browser_tools, session_for
from worker.tools.browser_pool import POOL_KEY, BrowserPool, BrowserPoolBusy, get_pool, shutdown_pool
from worker.tools.registry import ToolRegistry
from worker.tools.retry import execute_with_retry

PAGES = {
    "/page-a": "<title>Page A</title><h1>Alpha</h1><a href='/x'>One</a><a href='/x'>Two</a><a href='/x'>Three</a>"
               "<a href='/x'>Four</a><form><button type='submit'>Save bill</button></form>",
    "/page-b": "<title>Page B</title><h1>Beta</h1><a href='/x'>One</a><a href='/x'>Two</a><a href='/x'>Three</a>"
               "<a href='/x'>Four</a><button type='button'>Next page</button>",
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        headers = {}
        if path == "/set":
            headers["Set-Cookie"] = f"sid={query}; Path=/"
            body = f"<title>Set</title><h1>cookie set to {query}</h1>"
        elif path == "/whoami":
            body = f"<title>Who</title><h1>cookie: {self.headers.get('Cookie') or 'none'}</h1>"
        elif path.startswith("/slow/"):
            body = f"<title>Slow {path[6:]}</title><h1>slow page {path[6:]}</h1>"
        else:
            body = PAGES.get(path, "<title>Other</title><h1>other</h1>")
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def base_url():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def make_ctx(tmp_path, name: str, pool: BrowserPool | None) -> ToolContext:
    workspace, run_dir = tmp_path / "workspace", tmp_path / "runs" / name
    workspace.mkdir(exist_ok=True)
    run_dir.mkdir(parents=True)
    extras = {POOL_KEY: pool} if pool is not None else {}
    return ToolContext(
        run_id=name, run_dir=run_dir, workspace=workspace, settings=Settings(_env_file=None, HEADLESS=True), extras=extras
    )


@pytest.fixture
def registry() -> ToolRegistry:
    """ONE registry (one set of tool instances) shared by every run in a test."""
    reg = ToolRegistry()
    register_browser_tools(reg)
    return reg


@pytest_asyncio.fixture
async def own_pool():
    """A private pool for tests that crash, cap or count launches (the shared session pool stays untouched)."""
    pool = BrowserPool(max_contexts=4, acquire_timeout_s=2)
    yield pool
    await pool.shutdown()


async def call(registry, ctx, name, **args):
    return await execute_with_retry(registry.get(name), args, ctx)


def ref_of(observation: str, needle: str) -> int:
    for line in observation.splitlines():
        if line.startswith("[") and needle in line:
            return int(line[1 : line.index("]")])
    raise AssertionError(f"no element line containing {needle!r} in:\n{observation}")


# ----------------------------------------------------------------------------- cross-run classification safety
def _cached(ctx: ToolContext, refs: dict[int, dict], focus: int | None = None, url: str = "http://x/page") -> None:
    session = session_for(ctx)
    session.refs, session.focus, session.url = refs, focus, url


async def test_one_registry_classifies_each_run_by_its_own_session(tmp_path):
    registry = ToolRegistry()
    register_browser_tools(registry)  # ONE set of tool instances for both runs
    click, press, typ = (registry.get(n) for n in ("browser_click", "browser_press", "browser_type"))
    run_a, run_b = make_ctx(tmp_path, "a", None), make_ctx(tmp_path, "b", None)
    _cached(run_a, {5: {"ref": 5, "role": "button", "name": "Save bill", "submit": True}}, url="http://x/bills/new")
    _cached(run_b, {5: {"ref": 5, "role": "button", "name": "Next page", "submit": True}}, url="http://x/bills?page=2")
    args = {"ref": 5}

    assert (click.classify(args, run_a), click.classify(args, run_b)) == (Risk.WRITE, Risk.READ)  # A then B
    assert (click.classify(args, run_b), click.classify(args, run_a)) == (Risk.READ, Risk.WRITE)  # B then A
    for _ in range(20):  # interleaved, as concurrent runs would
        assert click.classify(args, run_a) is Risk.WRITE
        assert click.classify(args, run_b) is Risk.READ
        assert click.describe_action(args, run_a) == 'Click button "Save bill" on /bills/new'
        assert click.describe_action(args, run_b) == 'Click button "Next page" on /bills?page=2'
    # the same holds for the retry decision, which also depends on the classification
    assert click.retry_safe(args, run_a) is False and click.retry_safe(args, run_b) is True
    # focus-based classification (Enter) and Type+submit follow the run too
    form_a = {"ref": 7, "role": "textbox", "name": "Due", "formSubmits": ["Save bill"]}
    form_b = {"ref": 7, "role": "textbox", "name": "Search", "formSubmits": ["Search"]}
    _cached(run_a, {7: form_a}, focus=7)
    _cached(run_b, {7: form_b}, focus=7)
    assert press.classify({"key": "Enter"}, run_a) is Risk.WRITE and press.classify({"key": "Enter"}, run_b) is Risk.READ
    assert typ.classify({"ref": 7, "text": "x", "submit": True}, run_a) is Risk.WRITE
    assert typ.classify({"ref": 7, "text": "x", "submit": True}, run_b) is Risk.READ


async def test_unknown_element_is_never_downgraded_to_read(tmp_path):
    registry = ToolRegistry()
    register_browser_tools(registry)
    click, typ, press = (registry.get(n) for n in ("browser_click", "browser_type", "browser_press"))
    fresh = make_ctx(tmp_path, "fresh", None)  # no browser session at all
    assert peek_session(fresh) is None
    assert click.classify({"ref": 1}, fresh) is Risk.WRITE
    assert typ.classify({"ref": 1, "text": "x", "submit": True}, fresh) is Risk.WRITE
    assert typ.classify({"ref": 1, "text": "x"}, fresh) is Risk.READ  # typing without Enter commits nothing
    assert press.classify({"key": "Enter"}, fresh) is Risk.READ  # nothing can be focused without a page
    assert click.describe_action({"ref": 1}, None) == "Click element [1] on the current page"  # ctx=None is fine

    run = make_ctx(tmp_path, "run", None)
    _cached(run, {1: {"ref": 1, "role": "link", "name": "Bills"}})
    assert click.classify({"ref": 1}, run) is Risk.READ
    assert click.classify({"ref": 2}, run) is Risk.WRITE  # ref missing from this run's cache
    assert click.classify({"ref": "abc"}, run) is Risk.WRITE
    _cached(run, {1: {"ref": 1, "role": "link", "name": "Bills"}}, focus=9)  # focused element unknown
    assert press.classify({"key": "Enter"}, run) is Risk.WRITE


async def test_registry_tools_hold_no_per_run_state(tmp_path):
    registry = ToolRegistry()
    register_browser_tools(registry)
    for tool in (registry.get(n) for n in registry.names()):
        assert not vars(tool), f"{tool.name} keeps instance state: {vars(tool)}"


# ----------------------------------------------------------------------------- pool behaviour
async def test_two_sessions_from_one_pool_are_isolated(tmp_path, registry, browser_pool, base_url):
    a, b = make_ctx(tmp_path, "a", browser_pool), make_ctx(tmp_path, "b", browser_pool)
    try:
        assert (await call(registry, a, "browser_navigate", url=f"{base_url}/set?alice")).ok
        seen_by_a = await call(registry, a, "browser_navigate", url=f"{base_url}/whoami")
        assert "cookie: sid=alice" in seen_by_a.observation
        seen_by_b = await call(registry, b, "browser_navigate", url=f"{base_url}/whoami")
        assert "cookie: none" in seen_by_b.observation  # A's cookie is not visible in B

        # navigation in A leaves B where it was
        before = session_for(b).page.url
        await call(registry, a, "browser_navigate", url=f"{base_url}/page-a")
        assert session_for(b).page.url == before == f"{base_url}/whoami"
        assert session_for(a)._context is not session_for(b)._context
        assert session_for(a)._context.browser is session_for(b)._context.browser  # one Chromium for both
        # each run's screenshots / downloads land in its own run dir
        shot = await call(registry, a, "browser_screenshot", label="a")
        assert (a.run_dir / shot.evidence[0]).is_file() and not (b.run_dir / "screenshots").exists()
    finally:
        await close_browser(a)
        await close_browser(b)


async def test_release_closes_only_that_context(tmp_path, registry, browser_pool, base_url):
    a, b = make_ctx(tmp_path, "a", browser_pool), make_ctx(tmp_path, "b", browser_pool)
    base = browser_pool.active_contexts
    try:
        await call(registry, a, "browser_navigate", url=f"{base_url}/page-a")
        await call(registry, b, "browser_navigate", url=f"{base_url}/page-b")
        assert browser_pool.active_contexts == base + 2
        context_a = session_for(a)._context

        await close_browser(a)
        assert "browser" not in a.extras
        assert browser_pool.active_contexts == base + 1
        with pytest.raises(Exception):  # the context is really closed
            await context_a.new_page()
        assert browser_pool.browser_alive(True)  # the shared Chromium keeps running
        assert (await call(registry, b, "browser_read_page")).ok  # B is unaffected
        await browser_pool.release(context_a)  # double release is harmless
        assert browser_pool.active_contexts == base + 1
    finally:
        await close_browser(a)
        await close_browser(b)
    assert browser_pool.active_contexts == base


async def test_chromium_crash_is_recovered_on_the_next_action(tmp_path, registry, own_pool, base_url):
    a, b = make_ctx(tmp_path, "a", own_pool), make_ctx(tmp_path, "b", own_pool)
    try:
        await call(registry, a, "browser_navigate", url=f"{base_url}/page-a")
        await call(registry, b, "browser_navigate", url=f"{base_url}/page-b")
        assert own_pool.launches == 1
        await session_for(a)._context.browser.close()  # crash: both runs lose their page
        assert not own_pool.browser_alive()

        result = await call(registry, a, "browser_read_page")
        assert result.ok and "browser was restarted" in result.observation
        assert own_pool.launches == 2 and own_pool.browser_alive()
        again = await call(registry, b, "browser_navigate", url=f"{base_url}/page-b")
        assert again.ok and "Title: Page B" in again.observation
        assert "browser was restarted" in again.observation  # B's note comes with its first observation
        assert own_pool.launches == 2  # one relaunch served both runs
        assert own_pool.active_contexts == 2  # the dead contexts gave their slots back
    finally:
        await close_browser(a)
        await close_browser(b)
    assert own_pool.active_contexts == 0


async def test_parallel_runs_navigate_and_read_independently(tmp_path, registry, own_pool, base_url):
    runs = [make_ctx(tmp_path, f"run{i}", own_pool) for i in range(4)]

    async def work(i: int, ctx: ToolContext) -> str:
        nav = await call(registry, ctx, "browser_navigate", url=f"{base_url}/slow/{i}")
        assert nav.ok, nav.observation
        await call(registry, ctx, "browser_navigate", url=f"{base_url}/set?user{i}")
        await asyncio.sleep(0.05 * (4 - i))  # let the runs overtake each other
        await call(registry, ctx, "browser_navigate", url=f"{base_url}/whoami")
        read = await call(registry, ctx, "browser_read_page")
        assert read.ok, read.observation
        return read.observation

    try:
        results = await asyncio.gather(*(work(i, ctx) for i, ctx in enumerate(runs)))
        assert own_pool.launches == 1 and own_pool.active_contexts == 4
        for i, observation in enumerate(results):
            assert f"cookie: sid=user{i}" in observation  # nobody saw somebody else's cookie
    finally:
        await asyncio.gather(*(close_browser(ctx) for ctx in runs))
    assert own_pool.active_contexts == 0


async def test_context_cap_waits_then_fails_clearly(tmp_path, registry, base_url):
    pool = BrowserPool(max_contexts=2, acquire_timeout_s=0.4)
    ctx = make_ctx(tmp_path, "capped", pool)
    try:
        first, second = await pool.acquire_context(), await pool.acquire_context()
        with pytest.raises(BrowserPoolBusy, match="2 browser slots"):
            await pool.acquire_context()
        assert pool.active_contexts == 2

        # a waiting caller proceeds as soon as a slot frees up
        waiter = asyncio.create_task(pool.acquire_context())
        await asyncio.sleep(0.1)
        assert not waiter.done()
        await pool.release(first)
        third = await asyncio.wait_for(waiter, 5)
        assert pool.active_contexts == 2

        # through a tool the agent gets a clear, non-transient message
        result = await call(registry, ctx, "browser_navigate", url=f"{base_url}/page-a")
        assert not result.ok and not result.transient and "browser slots are in use" in result.observation
        await pool.release(second)
        await pool.release(third)
        assert (await call(registry, ctx, "browser_navigate", url=f"{base_url}/page-a")).ok
    finally:
        await close_browser(ctx)
        await pool.shutdown()


async def test_failed_start_returns_the_slot(tmp_path):
    pool = BrowserPool(max_contexts=1, acquire_timeout_s=0.2)
    session = BrowserSession(make_ctx(tmp_path, "x", pool), pool)

    class Boom(Exception):
        pass

    async def failing_launch(headless):
        raise Boom()

    pool._launch = failing_launch  # type: ignore[method-assign]
    with pytest.raises(Boom):
        await session.start()
    assert pool.active_contexts == 0
    with pytest.raises(Boom):  # the single slot was freed, so this fails on launch, not on "busy"
        await pool.acquire_context()


async def test_default_pool_is_one_per_loop_and_sized_from_settings():
    settings = Settings(_env_file=None, BROWSER_MAX_CONTEXTS=3)
    pool = get_pool(settings)
    assert get_pool() is pool and pool.max_contexts == 3
    await shutdown_pool()  # nothing was launched; just drops the pool
    assert get_pool(settings) is not pool
    await shutdown_pool()
