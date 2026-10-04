"""Tests for the generic browser tools (real headless Chromium against a tiny local HTTP server)."""
from __future__ import annotations

import io
import socket
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from worker.config import Settings
from worker.tools import retry as retry_module
from worker.tools.base import Risk, ToolContext
from worker.tools.browser import (
    BrowserHub,
    BrowserSession,
    browser_tools,
    classify_element,
    close_browser,
    register_browser_tools,
)
from worker.tools.registry import ToolRegistry
from worker.tools.retry import execute_with_retry
from worker.tools.snapshot import chunk_lines, element_line, find_in_page, format_snapshot

FIXTURES = Path(__file__).parent / "fixtures"


def _pdf_bytes() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Invoice CX-2025-0412 total 1200.00")
    c.save()
    return buf.getvalue()


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/boom":
            self._send(500, b"<html><head><title>Server error</title></head><body><h1>Internal error</h1></body></html>")
        elif self.path == "/files/inline.pdf":
            self._send(200, _pdf_bytes(), "application/pdf", {"Content-Disposition": "inline; filename=inline.pdf"})
        elif self.path == "/long":
            rows = "".join(f"<tr><td>Row {i}</td><td>needle-{i}</td></tr>" for i in range(1, 400))
            self._send(200, f"<html><head><title>Long</title></head><body><main><h1>Long list</h1><table>{rows}</table>"
                            "<a href='/form.html'>Back home</a></main></body></html>".encode())
        else:
            super().do_GET()

    def _send(self, status: int, body: bytes, ctype: str = "text/html", headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def base_url():
    server = HTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(FIXTURES)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(retry_module, "BACKOFF_BASE_S", 0.0)


@pytest.fixture
def ctx(tmp_path) -> ToolContext:
    workspace = tmp_path / "workspace"
    run_dir = tmp_path / "runs" / "r1"
    workspace.mkdir()
    run_dir.mkdir(parents=True)
    return ToolContext(
        run_id="r1", run_dir=run_dir, workspace=workspace, settings=Settings(_env_file=None, HEADLESS=True)
    )


@pytest.fixture
async def tools(ctx):
    registry = ToolRegistry()
    register_browser_tools(registry)
    yield registry
    await close_browser(ctx)


async def call(tools: ToolRegistry, ctx: ToolContext, name: str, **args):
    return await execute_with_retry(tools.get(name), args, ctx)


def ref_of(observation: str, needle: str) -> int:
    """Find the ref of the element line containing ``needle``."""
    for line in observation.splitlines():
        if line.startswith("[") and needle in line:
            return int(line[1 : line.index("]")])
    raise AssertionError(f"no element line containing {needle!r} in:\n{observation}")


async def open_form(tools, ctx, base_url) -> str:
    result = await call(tools, ctx, "browser_navigate", url=f"{base_url}/form.html")
    assert result.ok, result.observation
    return result.observation


# ----------------------------------------------------------------------------- snapshot formatting
async def test_snapshot_format(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)

    assert f"URL: {base_url}/form.html" in obs
    assert "Title: New AP bill - Ledger" in obs
    assert "# New AP bill" in obs
    # table rendered as pipe rows, link inside a cell annotated with its ref
    assert "Invoice | Amount | Status" in obs
    assert "CX-2025-0412 | 1,200.00 | Open" in obs
    assert "CX-2025-0377 | 840.50 | View[" in obs
    # accessible names, values, placeholders, types
    assert 'textbox "Invoice number" value="CX-2025-0412"' in obs
    assert 'textbox "Due date" value="15/11/2026" placeholder="YYYY-MM-DD"' in obs
    assert 'checkbox "Urgent" unchecked' in obs
    assert 'button "Save bill"' in obs
    assert 'textbox "Notes"' in obs
    assert 'link "Bills" -> /form.html' in obs
    # select: current value, options capped with a remainder count (disabled one is still listed in the count)
    vendor = next(ln for ln in obs.splitlines() if 'combobox "Vendor"' in ln)
    assert 'selected="Globex Corporation"' in vendor
    assert "-- Select vendor --" in vendor and "...(+" in vendor
    # password is masked
    pw = next(ln for ln in obs.splitlines() if 'textbox "Portal password"' in ln)
    assert "type=password" in pw and '"••••"' in pw
    assert "hunter2" not in obs
    # hidden things are excluded everywhere
    for hidden in ("Hidden button", "Hidden paragraph", "Invisible button", "Aria hidden", "Zero size"):
        assert hidden not in obs
    # no alert yet (the flash div is display:none)
    assert "Alerts:" not in obs


async def test_refs_are_assigned_in_dom(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    session = ctx.extras["browser"]
    save = ref_of(obs, 'button "Save bill"')
    count = await session.page.locator(f'[data-agent-ref="{save}"]').count()
    assert count == 1
    assert await session.page.locator(f'[data-agent-ref="{save}"]').inner_text() == "Save bill"


async def test_truncation_scroll_and_find(tools, ctx, base_url):
    result = await call(tools, ctx, "browser_navigate", url=f"{base_url}/long")
    obs = result.observation
    assert "(truncated, use scroll or read_page with `find` to see more)" in obs
    assert "(part 1/" in obs
    assert "needle-399" not in obs

    scrolled = await call(tools, ctx, "browser_scroll", direction="down")
    assert "(part 2/" in scrolled.observation
    assert "Row 1 |" not in scrolled.observation

    found = await call(tools, ctx, "browser_read_page", find="needle-399")
    assert found.ok
    assert "> Row 399 | needle-399" in found.observation
    assert len(found.observation) < 1500  # compact

    missing = await call(tools, ctx, "browser_read_page", find="no-such-text-anywhere")
    assert "No match" in missing.observation

    top = await call(tools, ctx, "browser_scroll", direction="top")
    assert "(part 1/" in top.observation


# ----------------------------------------------------------------------------- action flows
async def test_type_click_flow_with_alert(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    due = ref_of(obs, 'textbox "Due date"')

    typed = await call(tools, ctx, "browser_type", ref=due, text="2026-11-15x")
    assert typed.ok and typed.evidence == []  # filling is not a state change
    assert 'value="2026-11-15x"' in typed.observation

    # replace (clear=true) and submit with the wrong format first
    await call(tools, ctx, "browser_type", ref=due, text="15/11/2026")
    save = ref_of(typed.observation, 'button "Save bill"')
    clicked = await call(tools, ctx, "browser_click", ref=save)
    assert clicked.ok, clicked.observation
    assert 'Alerts: [role=alert] "Due date must be in YYYY-MM-DD format"' in clicked.observation
    assert "INVALID" in clicked.observation  # aria-invalid field flagged
    assert len(clicked.evidence) == 1
    assert (ctx.run_dir / clicked.evidence[0]).is_file()
    assert clicked.evidence[0].startswith("screenshots/001_")

    # fix the date and submit with Enter
    due = ref_of(clicked.observation, 'textbox "Due date"')
    result = await call(tools, ctx, "browser_type", ref=due, text="2026-11-15", submit=True)
    assert result.ok
    assert 'Alerts: [role=alert] "Bill saved: Globex Corporation / 2026-11-15"' in result.observation
    assert result.evidence and result.evidence[0].startswith("screenshots/002_")


async def test_type_append_and_wrong_element_kinds(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    inv = ref_of(obs, 'textbox "Invoice number"')
    appended = await call(tools, ctx, "browser_type", ref=inv, text="-A", clear=False)
    assert 'value="CX-2025-0412-A"' in appended.observation

    wrong = await call(tools, ctx, "browser_type", ref=ref_of(obs, "Urgent"), text="x")
    assert not wrong.ok and not wrong.transient and "browser_click" in wrong.observation
    wrong = await call(tools, ctx, "browser_type", ref=ref_of(obs, 'combobox "Vendor"'), text="x")
    assert not wrong.ok and "browser_select" in wrong.observation


async def test_select_by_label_value_and_errors(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    vendor = ref_of(obs, 'combobox "Vendor"')

    by_label = await call(tools, ctx, "browser_select", ref=vendor, option="company x ltd")
    assert by_label.ok, by_label.observation
    assert 'selected="Company X Ltd"' in by_label.observation
    assert by_label.evidence

    by_value = await call(tools, ctx, "browser_select", ref=vendor, option="glb")
    assert 'selected="Globex Corporation"' in by_value.observation

    ambiguous = await call(tools, ctx, "browser_select", ref=vendor, option="Company X")
    assert not ambiguous.ok and "several options" in ambiguous.observation
    assert "'Company X Ltd'" in ambiguous.observation and "'Company X Holdings'" in ambiguous.observation

    bad = await call(tools, ctx, "browser_select", ref=vendor, option="Nonexistent Co")
    assert not bad.ok and not bad.transient
    assert "Valid options" in bad.observation and "'Initech'" in bad.observation
    assert "Old Vendor" not in bad.observation  # disabled options are not offered

    not_select = await call(tools, ctx, "browser_select", ref=ref_of(obs, 'textbox "Due date"'), option="x")
    assert not not_select.ok and "not a <select>" in not_select.observation


async def test_checkbox_click_toggles(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    urgent = ref_of(obs, "Urgent")
    result = await call(tools, ctx, "browser_click", ref=urgent)
    assert 'checkbox "Urgent" checked' in result.observation


async def test_stale_ref_errors(tools, ctx, base_url):
    # before any snapshot
    early = await call(tools, ctx, "browser_click", ref=1)
    assert not early.ok and "browser_navigate" in early.observation

    obs = await open_form(tools, ctx, base_url)
    missing = await call(tools, ctx, "browser_click", ref=999)
    assert not missing.ok and not missing.transient
    assert "browser_read_page" in missing.observation

    # a ref from the old page is invalid after navigating elsewhere
    old_ref = ref_of(obs, 'button "Delete"')
    await call(tools, ctx, "browser_navigate", url=f"{base_url}/popup.html")
    stale = await call(tools, ctx, "browser_click", ref=old_ref)
    assert not stale.ok and "not on the current page" in stale.observation


async def test_press_back_and_navigation_status(tools, ctx, base_url):
    await open_form(tools, ctx, base_url)
    boom = await call(tools, ctx, "browser_navigate", url=f"{base_url}/boom")
    assert boom.ok
    assert "HTTP status of last navigation: 500" in boom.observation
    assert "Internal error" in boom.observation

    back = await call(tools, ctx, "browser_back")
    assert back.ok and "Title: New AP bill - Ledger" in back.observation
    assert "HTTP status" not in back.observation

    esc = await call(tools, ctx, "browser_press", key="Escape")
    assert esc.ok and esc.evidence == []


async def test_relative_url_uses_sim_base(tools, ctx, base_url):
    ctx.settings.SIM_BASE_URL = base_url
    result = await call(tools, ctx, "browser_navigate", url="/popup.html")
    assert result.ok and "Title: Help popup" in result.observation
    assert (await call(tools, ctx, "browser_navigate", url="javascript:alert(1)")).ok is False


async def test_popup_switches_to_new_tab(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    result = await call(tools, ctx, "browser_click", ref=ref_of(obs, 'link "Popup link"'))
    assert result.ok, result.observation
    assert "Title: Help popup" in result.observation
    assert "new tab" in result.observation
    # follow-up actions apply to the new tab
    link = ref_of(result.observation, 'link "Back to form"')
    again = await call(tools, ctx, "browser_click", ref=link)
    assert "Title: New AP bill - Ledger" in again.observation


async def test_dialog_message_in_next_observation(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    result = await call(tools, ctx, "browser_click", ref=ref_of(obs, 'button "Show dialog"'))
    assert result.ok
    assert 'Dialog (alert) auto-accepted: "Hello dialog"' in result.observation
    follow = await call(tools, ctx, "browser_read_page")
    assert "Dialog (alert)" not in follow.observation  # reported only once


async def test_screenshot_tool(tools, ctx, base_url):
    await open_form(tools, ctx, base_url)
    shot = await call(tools, ctx, "browser_screenshot", label="my label!")
    assert shot.ok and shot.evidence == ["screenshots/001_my_label.png"]
    assert (ctx.run_dir / shot.evidence[0]).stat().st_size > 1000


# ----------------------------------------------------------------------------- downloads
async def test_download_link(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    result = await call(tools, ctx, "browser_download", ref=ref_of(obs, 'link "Download report"'))
    assert result.ok, result.observation
    assert result.data["path"] == "downloads/report.txt"
    assert (ctx.run_dir / "downloads" / "report.txt").read_text() == "Quarterly report\nTotal: 42\n"

    # same file again does not overwrite
    obs = (await call(tools, ctx, "browser_read_page")).observation
    result2 = await call(tools, ctx, "browser_download", ref=ref_of(obs, 'link "Download report"'))
    assert result2.data["path"] == "downloads/report_1.txt"


async def test_download_inline_pdf(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    result = await call(tools, ctx, "browser_download", ref=ref_of(obs, 'link "Open PDF"'))
    assert result.ok, result.observation
    assert result.data["path"] == "downloads/inline.pdf"
    data = (ctx.run_dir / "downloads" / "inline.pdf").read_bytes()
    assert data.startswith(b"%PDF")


async def test_download_via_plain_click_is_reported(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    result = await call(tools, ctx, "browser_click", ref=ref_of(obs, 'link "Download report"'))
    assert "Downloaded: downloads/report.txt" in result.observation


async def test_download_on_non_download_element(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    result = await call(tools, ctx, "browser_download", ref=ref_of(obs, 'checkbox "Urgent"'))
    assert not result.ok and not result.transient
    assert "did not start a download" in result.observation


# ----------------------------------------------------------------------------- risk classification
@pytest.mark.parametrize(
    "name,role,submit,expected",
    [
        ("Save bill", "button", True, Risk.WRITE),
        ("Create invoice", "button", False, Risk.WRITE),
        ("Send reply", "button", False, Risk.WRITE),
        ("Place order", "button", False, Risk.WRITE),
        ("Cancel order", "button", False, Risk.WRITE),
        ("Delete", "button", False, Risk.IRREVERSIBLE),
        ("Remove vendor", "button", False, Risk.IRREVERSIBLE),
        ("Pay now", "button", False, Risk.IRREVERSIBLE),
        ("Approve", "button", True, Risk.IRREVERSIBLE),
        ("Transfer funds", "button", False, Risk.IRREVERSIBLE),
        ("Next", "button", False, Risk.READ),
        ("Next", "button", True, Risk.READ),
        ("Cancel", "button", False, Risk.READ),
        ("Show more", "button", False, Risk.READ),
        ("Search", "button", True, Risk.READ),
        ("Sign in", "button", True, Risk.READ),
        ("Sign out", "button", True, Risk.READ),
        ("Continue", "button", True, Risk.WRITE),  # unknown submit button: be conservative
        ("", "button", True, Risk.WRITE),
        ("Bills", "link", False, Risk.READ),
        ("Payments", "link", False, Risk.READ),
        ("Create bill", "link", False, Risk.READ),  # navigation to a form, not a commit
        ("Delete item", "link", False, Risk.IRREVERSIBLE),
        ("Approve invoice", "link", False, Risk.IRREVERSIBLE),
        ("Urgent", "checkbox", False, Risk.READ),
        ("Delete notes", "textbox", False, Risk.READ),
    ],
)
def test_classify_element(name, role, submit, expected):
    assert classify_element(name, role, submit=submit) is expected


async def test_tool_classify_and_describe(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    click, typ, sel, press = (tools.get(n) for n in ("browser_click", "browser_type", "browser_select", "browser_press"))
    nav, read = tools.get("browser_navigate"), tools.get("browser_read_page")

    save, nxt, delete = (ref_of(obs, f'button "{n}"') for n in ("Save bill", "Next", "Delete"))
    assert click.classify({"ref": save}) is Risk.WRITE
    assert click.classify({"ref": delete}) is Risk.IRREVERSIBLE
    assert click.classify({"ref": nxt}) is Risk.READ
    assert click.classify({"ref": ref_of(obs, 'link "Bills"')}) is Risk.READ
    assert click.classify({"ref": 4242}) is Risk.READ  # unknown ref, will fail at run time

    assert click.describe_action({"ref": save}) == 'Click button "Save bill" on /form.html'
    assert nav.classify({"url": "http://x"}) is Risk.READ
    assert read.classify({}) is Risk.READ

    due = ref_of(obs, 'textbox "Due date"')
    assert typ.classify({"ref": due, "text": "x"}) is Risk.READ
    assert typ.classify({"ref": due, "text": "x", "submit": True}) is Risk.WRITE  # Enter submits the "Save bill" form
    search = ref_of(obs, 'textbox "Search bills"')
    assert typ.classify({"ref": search, "text": "x", "submit": True}) is Risk.READ  # search form
    assert sel.classify({"ref": ref_of(obs, 'combobox "Vendor"'), "option": "x"}) is Risk.READ
    assert typ.describe_action({"ref": due, "text": "2026-11-15"}) == (
        'Type "2026-11-15" into textbox "Due date" on /form.html'
    )
    pw = ref_of(obs, "Portal password")
    assert "hunter2" not in typ.describe_action({"ref": pw, "text": "hunter2"})
    assert sel.describe_action({"ref": ref_of(obs, "Vendor"), "option": "Initech"}).startswith(
        'Select "Initech" in combobox "Vendor"'
    )


async def test_press_enter_classification_follows_focus(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    press = tools.get("browser_press")
    assert press.classify({"key": "Enter"}) is Risk.READ  # nothing focused
    assert press.classify({"key": "Tab"}) is Risk.READ

    # focus the Due date field (inside the "Save bill" form); the snapshot reports the focus
    await call(tools, ctx, "browser_type", ref=ref_of(obs, 'textbox "Due date"'), text="2026-11-15")
    assert press.classify({"key": "Enter"}) is Risk.WRITE
    assert press.classify({"key": "Tab"}) is Risk.READ
    assert 'on textbox "Due date"' in press.describe_action({"key": "Enter"})

    # focus the search box (search form): Enter is harmless
    await call(tools, ctx, "browser_type", ref=ref_of(obs, 'textbox "Search bills"'), text="abc")
    assert press.classify({"key": "Enter"}) is Risk.READ


# ----------------------------------------------------------------------------- read-only mode
async def test_read_only_mode(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    ctx.extras["read_only"] = True

    # type / select are refused entirely
    typed = await call(tools, ctx, "browser_type", ref=ref_of(obs, 'textbox "Due date"'), text="2026-01-01")
    assert not typed.ok and not typed.transient and "read-only" in typed.observation
    sel = await call(tools, ctx, "browser_select", ref=ref_of(obs, 'combobox "Vendor"'), option="Initech")
    assert not sel.ok and "read-only" in sel.observation

    # write / irreversible clicks are refused and nothing happened on the page
    for name in ("Save bill", "Delete"):
        refused = await call(tools, ctx, "browser_click", ref=ref_of(obs, f'button "{name}"'))
        assert not refused.ok and "read-only" in refused.observation
    page_obs = (await call(tools, ctx, "browser_read_page")).observation
    assert "Alerts:" not in page_obs
    assert 'value="15/11/2026"' in page_obs

    # Enter in the form is refused once a field in it has focus
    await ctx.extras["browser"].page.focus("#due")
    page_obs = (await call(tools, ctx, "browser_read_page")).observation
    refused = await call(tools, ctx, "browser_press", key="Enter")
    assert not refused.ok and "read-only" in refused.observation

    # reading and harmless clicks still work
    ok = await call(tools, ctx, "browser_click", ref=ref_of(page_obs, 'button "Next"'))
    assert ok.ok
    nav = await call(tools, ctx, "browser_navigate", url=f"{base_url}/popup.html")
    assert nav.ok
    assert (await call(tools, ctx, "browser_scroll", direction="down")).ok
    assert (await call(tools, ctx, "browser_back")).ok
    assert (await call(tools, ctx, "browser_screenshot", label="ro")).ok


def test_read_only_safe_flags_and_registry_subset():
    registry = ToolRegistry()
    register_browser_tools(registry)
    assert len(registry) == 10
    safe = set(registry.subset(read_only=True).names())
    assert {"browser_navigate", "browser_click", "browser_read_page", "browser_scroll", "browser_back",
            "browser_screenshot", "browser_download"} <= safe
    assert "browser_type" not in safe and "browser_select" not in safe and "browser_press" not in safe
    for tool in registry.specs():
        assert tool.parameters["type"] == "object"


# ----------------------------------------------------------------------------- transient errors / lifecycle
async def test_connection_refused_is_transient(tools, ctx):
    with socket.socket() as sock:  # grab a free port, then close it so nothing listens there
        sock.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{sock.getsockname()[1]}/"
    tool = tools.get("browser_navigate")
    result = await tool.run({"url": dead}, ctx)
    assert not result.ok and result.transient
    assert "ERR_CONNECTION_REFUSED" in result.observation

    retried = await execute_with_retry(tool, {"url": dead}, ctx)
    assert not retried.ok and "automatic retr" in retried.observation


async def test_write_click_timeout_is_not_retried_but_read_click_is(tools, ctx, base_url, monkeypatch):
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    obs = await open_form(tools, ctx, base_url)
    save = ref_of(obs, 'button "Save bill"')
    field = ref_of(obs, 'textbox "Invoice number"')
    click = tools.get("browser_click")
    assert click.classify({"ref": save}) is Risk.WRITE and click.classify({"ref": field}) is Risk.READ

    runs = []

    async def timing_out(self, args, ctx, session):
        runs.append(args["ref"])
        raise PlaywrightTimeoutError("Locator.click: Timeout 5000ms exceeded.")

    monkeypatch.setattr(type(click), "execute", timing_out)

    write = await execute_with_retry(click, {"ref": save}, ctx)
    assert runs == [save]  # one attempt only
    assert not write.ok and not write.transient
    assert "NOT retried automatically" in write.observation and "browser_read_page" in write.observation

    runs.clear()
    read = await execute_with_retry(click, {"ref": field}, ctx)
    assert runs == [field, field] and not read.ok  # READ-level click keeps its auto-retry
    assert "automatic retr" in read.observation


async def test_browser_crash_is_recovered(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    session: BrowserSession = ctx.extras["browser"]
    await session._browser.close()  # simulate a crash
    # the next call relaunches Chromium and tells the agent the old page is gone
    result = await tools.get("browser_read_page").run({}, ctx)
    assert result.ok and "browser was restarted" in result.observation
    # old refs are not valid in the fresh browser
    stale = await call(tools, ctx, "browser_click", ref=ref_of(obs, 'button "Save bill"'))
    assert not stale.ok
    recovered = await call(tools, ctx, "browser_navigate", url=f"{base_url}/popup.html")
    assert recovered.ok and "Title: Help popup" in recovered.observation

    # a page closed underneath us surfaces as a transient error (the retry layer re-runs the call)
    await session._context.close()
    closed = await tools.get("browser_click").run({"ref": 1}, ctx)
    assert not closed.ok  # stale/closed, never an unhandled exception


async def test_close_browser_is_idempotent(tools, ctx, base_url):
    await open_form(tools, ctx, base_url)
    await close_browser(ctx)
    assert "browser" not in ctx.extras
    await close_browser(ctx)


async def test_headed_slow_mo_setting(ctx, monkeypatch):
    """Headed mode uses slow_mo for demo watchability (launch is stubbed; no window is opened)."""
    launched = {}

    class FakeBrowser:
        def is_connected(self):
            return True

        async def new_context(self, **kw):
            raise RuntimeError("stop here")

        async def close(self):
            pass

    class FakeChromium:
        async def launch(self, **kw):
            launched.update(kw)
            return FakeBrowser()

    class FakePW:
        chromium = FakeChromium()

        async def stop(self):
            pass

    class FakeStarter:
        async def start(self):
            return FakePW()

    import worker.tools.browser as browser_module

    monkeypatch.setattr(browser_module, "async_playwright", lambda: FakeStarter())
    ctx.settings.HEADLESS = False
    session = BrowserSession(ctx)
    with pytest.raises(RuntimeError):
        await session.start()
    assert launched == {"headless": False, "slow_mo": 150}


# ----------------------------------------------------------------------------- pure formatting
def _data(**over):
    base = {
        "url": "http://x/y", "title": "T", "alerts": [], "text": "line1\nline2", "textLen": 11,
        "elements": [], "totalElements": 0, "focus": None,
    }
    base.update(over)
    return base


def test_element_line_variants():
    assert element_line({"ref": 1, "role": "link", "name": "Bills", "href": "/erp/bills"}) == '[1] link "Bills" -> /erp/bills'
    line = element_line({
        "ref": 3, "role": "combobox", "name": "Vendor", "selected": "Company X Ltd",
        "options": ["Company X Ltd", "Globex"], "optionCount": 5,
    })
    assert line == '[3] combobox "Vendor" selected="Company X Ltd" options=[Company X Ltd, Globex, ...(+3)]'
    line = element_line({"ref": 4, "role": "textbox", "name": "Email", "type": "email", "flags": ["required", "disabled"],
                         "placeholder": "you@x.com"})
    assert line == '[4] textbox "Email" type=email required disabled placeholder="you@x.com"'
    assert 'INVALID: bad date' in element_line({"ref": 5, "role": "textbox", "name": "D", "flags": ["invalid"], "error": "bad date"})


def test_format_snapshot_budgets_and_status():
    elements = [{"ref": i, "role": "button", "name": f"B{i}"} for i in range(1, 11)]
    text = "\n".join(f"paragraph number {i} " + "x" * 40 for i in range(30))
    view = format_snapshot(_data(text=text, textLen=len(text), elements=elements), max_text=300, max_elements=4,
                           status=404, notes=["Downloaded: downloads/a.pdf"])
    assert "HTTP status of last navigation: 404" in view.text
    assert "Downloaded: downloads/a.pdf" in view.text
    assert view.pages > 1 and view.page == 0
    assert "(truncated, use scroll or read_page with `find` to see more)" in view.text
    assert '[4] button "B4"' in view.text and "[5]" not in view.text
    later = format_snapshot(_data(text=text, textLen=len(text), elements=elements), page=2, max_text=300, max_elements=4)
    assert '[9] button "B9"' in later.text and "(part 3/" in later.text
    # page clamps
    assert format_snapshot(_data(), page=7).page == 0
    assert "(none)" in format_snapshot(_data()).text


def test_chunk_lines_never_splits_lines():
    chunks = chunk_lines(["a" * 10, "b" * 10, "c" * 10], 25)
    assert chunks == [["a" * 10, "b" * 10], ["c" * 10]]
    assert chunk_lines(["z" * 100], 20)[0][0].endswith("…")


def test_find_in_page_context():
    text = "\n".join(f"line {i}" for i in range(10))
    out = find_in_page(_data(text=text), "line 5", context=1)
    assert "> line 5" in out and "  line 4" in out and "  line 6" in out and "line 8" not in out
    assert "No match" in find_in_page(_data(text=text), "zzz")


async def test_tools_share_one_session_per_ctx(ctx):
    hub = BrowserHub()
    a, b = browser_tools(hub)[:2]
    assert a.hub is b.hub
    assert hub.session_for(ctx) is hub.session_for(ctx)


# ----------------------------------------------------------------------------- approval preview (form values)
async def test_form_values_and_approval_preview_for_a_submit_click(tools, ctx, base_url):
    obs = await open_form(tools, ctx, base_url)
    await call(tools, ctx, "browser_type", ref=ref_of(obs, 'textbox "Due date"'), text="2026-11-15")
    save = ref_of(obs, 'button "Save bill"')
    session = ctx.extras["browser"]

    rows = {r["label"]: r["value"] for r in await session.form_values(save)}
    assert rows["Invoice number"] == "CX-2025-0412" and rows["Due date"] == "2026-11-15"
    assert rows["Vendor"] == "Globex Corporation" and rows["Urgent"] == "unchecked"
    assert rows["Portal password"] == "••••" and rows["Notes"] == ""
    assert "Search bills" not in rows  # only the clicked button's own form

    preview = await tools.get("browser_click").approval_preview({"ref": save}, ctx)
    assert {"label": "Due date", "value": "2026-11-15"} in preview
    # a plain navigation/read click has nothing worth previewing
    assert await tools.get("browser_click").approval_preview({"ref": ref_of(obs, 'button "Next"')}, ctx) is None
    assert await tools.get("browser_scroll").approval_preview({}, ctx) is None
