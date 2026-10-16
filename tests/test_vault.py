"""Credential vault: encryption, secret-safe tools, redaction and the runner wiring (v1 behavior without a vault)."""
from __future__ import annotations

import html
import itertools
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from worker.agent.broker import AutoBroker
from worker.agent.context import build_messages
from worker.agent.runner import AgentRunner
from worker.agent.state import Fact, RunState, Step
from worker.agent.toolset import build_registry
from worker.config import Settings
from worker.llm.base import LLMResponse, ToolCall, Usage
from worker.llm.fake import ScriptedLLM
from worker.redaction import RedactingStore, Redactor
from worker.server import create_app
from worker.store import FileRunStore
from worker.tools.base import Risk, ToolContext
from worker.tools.browser import ClickTool, close_browser
from worker.tools.browser_pool import POOL_KEY
from worker.tools.http import HttpRequestTool
from worker.tools.retry import execute_with_retry
from worker.tools.secrets import ListCredentialsTool
from worker.vault import (
    Credential,
    FileVaultBackend,
    Vault,
    VaultError,
    VaultScope,
    default_scope,
    generate_key,
    open_vault,
)
from worker.vault import (
    main as vault_main,
)

PASSWORD = "S3cret!pw#42 &x"
USERNAME = "ap.clerk"
API_KEY = "ledger-api-key-7f3a9"
_ids = itertools.count(1)


# ---------------------------------------------------------------- helpers


def call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"call_{next(_ids)}", name=name, args=args)


def reply(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(text="", tool_calls=list(calls), usage=Usage(10, 5))


def blocked() -> LLMResponse:
    return reply(call("finish", status="blocked", summary="Stopped here.", evidence=[], outputs=[]))


def ref_of(observation: str, needle: str) -> int:
    for line in observation.splitlines():
        if line.startswith("[") and needle in line:
            return int(line[1 : line.index("]")])
    raise AssertionError(f"no element line containing {needle!r} in:\n{observation}")


def scripted(*steps) -> ScriptedLLM:
    """Each step is ``f(last_message_text) -> LLMResponse``, used for the next worker turn."""
    queue = list(steps)
    return ScriptedLLM(lambda messages, tools: queue.pop(0)(messages[-1].content))


def all_text(llm: ScriptedLLM) -> str:
    """Everything the model was ever sent: messages and the tool-call arguments inside them."""
    parts = []
    for recorded in llm.calls:
        for message in recorded["messages"]:
            parts.append(message.content)
            parts.extend(json.dumps(c.args) for c in message.tool_calls)
    return "\n".join(parts)


def run_files_text(run_dir) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in run_dir.rglob("*")
                     if p.is_file() and p.suffix in (".json", ".jsonl", ".md"))


async def make_scope(vault: Vault, owner: str = "default") -> VaultScope:
    await vault.put(owner, "erp", "login", {"username": USERNAME, "password": PASSWORD},
                    description="ERP ledger login", url="http://127.0.0.1/erp")
    await vault.put(owner, "erp-api", "api_key", {"api_key": API_KEY, "header_name": "X-API-Key"},
                    description="ERP REST API", host="localhost")
    return await VaultScope.load(vault, owner)


def make_ctx(tmp_path, pool=None, scope: VaultScope | None = None, **settings) -> ToolContext:
    workspace, run_dir = tmp_path / "workspace", tmp_path / "runs" / "r1"
    workspace.mkdir(exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    extras: dict = {POOL_KEY: pool} if pool is not None else {}
    if scope is not None:
        extras["vault_scope"] = scope
        extras["redactor"] = Redactor.from_scope(scope)
    return ToolContext(run_id="r1", run_dir=run_dir, workspace=workspace,
                       settings=Settings(_env_file=None, HEADLESS=True, **settings), extras=extras)


@pytest.fixture
def vault(tmp_path) -> Vault:
    return Vault(FileVaultBackend(tmp_path / "vault"), generate_key())


# ---------------------------------------------------------------- local site


class _Site(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _send(self, body: str, status: int = 200):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/login":
            self._send(
                "<html><head><title>Sign in</title></head><body><main><h1>Sign in</h1>"
                "<form method='post' action='/echo'>"
                "<label for='u'>Username</label><input id='u' name='username' type='text'>"
                "<label for='p'>Password</label><input id='p' name='password' type='password'>"
                "<label for='n'>Notes</label><textarea id='n' name='notes'></textarea>"
                "<input type='checkbox' id='c' name='remember' aria-label='Remember me'>"
                "<button type='submit'>Sign in</button></form></main></body></html>"
            )
        elif url.path == "/register":  # looks like a login (username + password) but its button creates an account
            self._send(
                "<html><head><title>Register</title></head><body><main><h1>Create account</h1>"
                "<form method='post' action='/echo'>"
                "<label for='u'>Username</label><input id='u' name='username' type='text'>"
                "<label for='p'>Password</label><input id='p' name='password' type='password'>"
                "<button type='submit'>Sign up</button></form></main></body></html>"
            )
        elif url.path == "/prefilled":  # a text field that already holds a value from the query string
            value = html.escape(parse_qs(url.query).get("v", [""])[0], quote=True)
            self._send(
                "<html><head><title>Settings</title></head><body><main><h1>Settings</h1><form method='post' action='/echo'>"
                f"<input type='text' name='token' aria-label='API token' value='{value}'>"
                "<button type='submit'>Save settings</button></form></main></body></html>"
            )
        else:
            self._send("<html><body>not found</body></html>", 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        password = html.escape(form.get("password", [""])[0])
        user = html.escape(form.get("username", [""])[0])
        self._send(
            "<html><head><title>Welcome</title></head><body><main><h1>Welcome</h1>"
            f"<p>Signed in as {user}.</p><p>You sent password: {password}</p></main></body></html>"
        )


@pytest.fixture(scope="module")
def site():
    server = HTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


# ---------------------------------------------------------------- vault storage


async def test_encryption_round_trip_and_the_file_holds_no_plaintext(vault, tmp_path):
    info = await vault.put("default", "erp", "login", {"username": USERNAME, "password": PASSWORD},
                           description="ERP", url="http://erp.example.com:8001/login")
    assert info["host"] == "erp.example.com" and info["fields"] == ["password", "username"]

    credential = await vault.get("default", "erp")
    assert credential.fields == {"username": USERNAME, "password": PASSWORD}
    assert PASSWORD not in repr(credential)  # never leaks through a stray log line or traceback

    raw = (tmp_path / "vault" / "vault.json").read_text(encoding="utf-8")
    assert PASSWORD not in raw and USERNAME not in raw and "erp.example.com" in raw  # only metadata is in clear
    assert (await vault.get("default", "missing")) is None


async def test_wrong_or_invalid_key_fails_without_echoing_it(vault, tmp_path):
    await vault.put("default", "erp", "login", {"password": PASSWORD})
    other = Vault(FileVaultBackend(tmp_path / "vault"), generate_key())
    with pytest.raises(VaultError, match="wrong VAULT_KEY"):
        await other.get("default", "erp")
    with pytest.raises(VaultError) as exc:
        Vault(FileVaultBackend(tmp_path / "vault"), "not-a-fernet-key")
    assert "not-a-fernet-key" not in str(exc.value)


async def test_list_public_has_names_and_hints_but_never_values(vault):
    await vault.put("acme", "erp", "login", {"username": USERNAME, "password": PASSWORD}, description="ERP", url="http://h/x")
    await vault.put("acme", "api", "api_key", {"api_key": API_KEY})
    await vault.put("other", "secret-of-other-org", "api_key", {"api_key": "zzz-other-org-key"})

    rows = await vault.list_public("acme")
    assert [r["name"] for r in rows] == ["api", "erp"]  # scoped to the owner, sorted
    dump = json.dumps(rows)
    for secret in (PASSWORD, API_KEY, USERNAME, "zzz-other-org-key"):
        assert secret not in dump
    assert rows[1]["fields"] == ["password", "username"] and rows[1]["description"] == "ERP"


async def test_put_replaces_keeps_created_at_and_validates(vault):
    first = await vault.put("default", "erp", "login", {"password": "first-password"})
    again = await vault.put("default", "erp", "login", {"password": "second-password"})
    assert again["created_at"] == first["created_at"]
    assert (await vault.get("default", "erp")).fields["password"] == "second-password"
    assert await vault.delete("default", "erp") and not await vault.delete("default", "erp")
    for bad in (("default", "erp", "login", {}), ("default", "bad name", "login", {"password": "x"}),
                ("default", "erp", "oauth", {"password": "x"}), ("default", "k", "header", {"header_name": "X"})):
        with pytest.raises(VaultError):
            await vault.put(*bad)


async def test_rotate_key_reencrypts_everything(vault, tmp_path):
    await vault.put("default", "erp", "login", {"password": PASSWORD})
    old_key_vault = Vault(vault.backend, generate_key())  # stands in for "the old key": a different one fails
    new_key = await vault.rotate_key()
    assert (await vault.get("default", "erp")).fields["password"] == PASSWORD
    assert (await Vault(FileVaultBackend(tmp_path / "vault"), new_key).get("default", "erp")).fields["password"] == PASSWORD
    with pytest.raises(VaultError):
        await old_key_vault.get("default", "erp")


async def test_open_vault_generates_a_dev_key_once_and_never_logs_it(tmp_path, caplog):
    settings = Settings(_env_file=None, VAULT_DIR=str(tmp_path / "v"))
    with caplog.at_level(logging.WARNING, logger="worker.vault"):
        first = open_vault(settings)
    key = (tmp_path / "v" / ".vault_key").read_text(encoding="utf-8").strip()
    assert "VAULT_KEY is not set" in caplog.text and key not in caplog.text
    await first.put("default", "erp", "login", {"password": PASSWORD})
    assert (await open_vault(settings).get("default", "erp")).fields["password"] == PASSWORD  # same key reused

    (tmp_path / "v" / ".vault_key").unlink()
    with pytest.raises(VaultError, match="no key was found"):  # never invent a key over an existing vault
        open_vault(settings)
    with pytest.raises(VaultError, match="deploy mode"):
        open_vault(Settings(_env_file=None, VAULT_DIR=str(tmp_path / "other"), DEPLOY_MODE=True))
    assert not (tmp_path / "other").exists()
    env_key = generate_key()  # VAULT_KEY wins and no key file is written
    open_vault(Settings(_env_file=None, VAULT_DIR=str(tmp_path / "third"), VAULT_KEY=env_key))
    assert not (tmp_path / "third" / ".vault_key").exists()


async def test_default_scope_is_none_without_a_vault_and_creates_nothing(tmp_path):
    settings = Settings(_env_file=None, VAULT_DIR=str(tmp_path / "none"))
    assert await default_scope(settings) is None
    assert not (tmp_path / "none").exists()
    empty = Vault(FileVaultBackend(tmp_path / "empty"), generate_key())
    assert await default_scope(settings, vault=empty) is None


def test_cli_put_list_delete(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "cli"))
    from worker.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    try:
        assert vault_main(["put", "--name", "erp", "--kind", "login", "--url", "http://localhost:8001/erp",
                           "--field", "username=ap.clerk", "--field", "password=-", "--description", "ERP"]) == 0
        capsys.readouterr()
        assert vault_main(["list"]) == 0
        listing = capsys.readouterr().out
        assert "erp" in listing and "localhost" in listing and PASSWORD not in listing and "ap.clerk" not in listing
        assert vault_main(["delete", "--name", "erp"]) == 0
        assert vault_main(["delete", "--name", "erp"]) == 1
        assert PASSWORD not in (tmp_path / "cli" / "vault.json").read_text(encoding="utf-8")
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------- redactor and store


def test_redactor_catches_exact_encoded_and_basic_auth_forms():
    scope = VaultScope("default", [Credential("default", "erp", "login", {"username": "ap", "password": PASSWORD})])
    redactor = Redactor.from_scope(scope)
    import base64
    from urllib.parse import quote, quote_plus
    basic = base64.b64encode(f"ap:{PASSWORD}".encode()).decode()
    text = (f"a {PASSWORD} b {quote(PASSWORD, safe='')} c {quote_plus(PASSWORD)} d {html.escape(PASSWORD)} "
            f"e {json.dumps(PASSWORD)[1:-1]} f {basic} g {re.sub(r'%[0-9A-F]{2}', lambda m: m.group().lower(), quote(PASSWORD, safe=''))}")
    out = redactor.redact(text)
    assert PASSWORD not in out and quote(PASSWORD, safe="") not in out and basic not in out
    assert out.count("[REDACTED:erp.password]") == 6 and out.count("[REDACTED:erp.basic_auth]") == 1
    assert "ap" in redactor.redact("login ap ok")  # the username is an identifier, not a secret
    assert not Redactor({"x.pw": "abc"})  # too short to redact safely
    assert Redactor({"a": "REDACTED"}).redact("REDACTED REDACTED") == "[REDACTED:a] [REDACTED:a]"  # no marker re-scan


async def test_redacting_store_scrubs_every_write(tmp_path):
    redactor = Redactor({"erp.password": PASSWORD})
    inner = FileRunStore(tmp_path / "runs")
    store = RedactingStore(inner, redactor)
    state = RunState(run_id="s1", task=f"log in with {PASSWORD}")
    await store.create_run(state)
    state.goal_summary = f"use {PASSWORD}"
    await store.save_state(state)
    await store.append_step("s1", Step(tool="remember", args={"value": PASSWORD}, observation=f"saw {PASSWORD}", error=PASSWORD))
    await store.upsert_fact("s1", Fact("pw", PASSWORD, f"from {PASSWORD}"))
    await store.append_event("s1", "thought", {"text": PASSWORD, "nested": [{"x": PASSWORD}]})
    await store.save_runtime("s1", {"nudges": [PASSWORD]})
    await store.save_report("s1", {"summary": PASSWORD}, f"# {PASSWORD}")

    assert PASSWORD not in run_files_text(tmp_path / "runs" / "s1")
    loaded = await store.load_state("s1")
    assert loaded.steps[0].observation == "saw [REDACTED:erp.password]" and loaded.facts["pw"].value == "[REDACTED:erp.password]"
    assert loaded.goal_summary == "use [REDACTED:erp.password]"
    assert state.goal_summary == f"use {PASSWORD}"  # the caller's objects are not modified by the store


# ---------------------------------------------------------------- list_credentials and the verifier


async def test_list_credentials_shows_names_and_hints_only(vault, tmp_path):
    scope = await make_scope(vault)
    result = await execute_with_retry(ListCredentialsTool(), {}, make_ctx(tmp_path, scope=scope))
    assert result.ok and "erp (login): ERP ledger login" in result.observation and "erp-api (api_key)" in result.observation
    assert "fields: password, username" in result.observation
    for secret in (PASSWORD, API_KEY, USERNAME):
        assert secret not in result.observation and secret not in json.dumps(result.data)

    none = await ListCredentialsTool().run({}, make_ctx(tmp_path))
    assert none.ok and "No credentials" in none.observation


def test_verifier_gets_list_credentials_and_fill_secret_but_cannot_type():
    registry = build_registry(Settings(_env_file=None))
    safe = set(registry.subset(read_only=True).names())
    assert {"list_credentials", "browser_fill_secret"} <= safe
    assert "browser_type" not in safe and "browser_select" not in safe and "browser_press" not in safe
    # offered to the auditor without being "read-only safe": the approval policy still gates it under confirm_all
    assert registry.get("browser_fill_secret").verifier_allowed and not registry.get("browser_fill_secret").read_only_safe


# ---------------------------------------------------------------- browser_fill_secret


async def test_fill_secret_types_into_the_dom_and_shows_only_a_mask(vault, tmp_path, browser_pool, site):
    scope = await make_scope(vault)
    ctx = make_ctx(tmp_path, browser_pool, scope)
    registry = build_registry(ctx.settings)
    fill, navigate = registry.get("browser_fill_secret"), registry.get("browser_navigate")
    try:
        page = await execute_with_retry(navigate, {"url": f"{site}/login"}, ctx)
        user_ref, pw_ref = ref_of(page.observation, '"Username"'), ref_of(page.observation, '"Password"')

        user = await execute_with_retry(fill, {"ref": user_ref, "credential": "erp", "field": "username"}, ctx)
        result = await execute_with_retry(fill, {"ref": pw_ref, "credential": "erp", "field": "password"}, ctx)
        assert user.ok and result.ok, result.observation
        assert "•••• (credential erp.password)" in result.observation
        assert PASSWORD not in result.observation and PASSWORD not in json.dumps(result.data)
        assert fill.classify({"ref": pw_ref}, ctx) is Risk.READ and fill.describe_action(
            {"ref": pw_ref, "credential": "erp", "field": "password"}, ctx).count(PASSWORD) == 0

        session = ctx.extras["browser"]
        assert await session.page.input_value("#p") == PASSWORD  # the secret really is in the page
        assert await session.page.input_value("#u") == USERNAME
    finally:
        await close_browser(ctx)


async def test_fill_secret_refusals(vault, tmp_path, browser_pool, site):
    scope = await make_scope(vault)
    ctx = make_ctx(tmp_path, browser_pool, scope)
    registry = build_registry(ctx.settings)
    fill = registry.get("browser_fill_secret")
    try:
        page = await execute_with_retry(registry.get("browser_navigate"), {"url": f"{site}/login"}, ctx)
        user_ref = ref_of(page.observation, '"Username"')
        notes_ref, box_ref, button_ref = (ref_of(page.observation, n) for n in ('"Notes"', '"Remember me"', "button"))
        session = ctx.extras["browser"]

        for ref in (notes_ref, box_ref, button_ref):  # textarea, checkbox, button
            refused = await execute_with_retry(fill, {"ref": ref, "credential": "erp", "field": "password"}, ctx)
            assert not refused.ok and "text or password inputs" in refused.observation
        assert await session.page.input_value("#n") == ""

        unknown = await execute_with_retry(fill, {"ref": user_ref, "credential": "nope", "field": "password"}, ctx)
        assert not unknown.ok and "Available: erp, erp-api" in unknown.observation
        no_field = await execute_with_retry(fill, {"ref": user_ref, "credential": "erp", "field": "pin"}, ctx)
        assert not no_field.ok and "fields: password, username" in no_field.observation.replace("Its f", "f")

        # a credential bound to another host is not typed into this page
        await vault.put("default", "elsewhere", "login", {"password": "other-site-pass"}, host="erp.example.com")
        ctx.extras["vault_scope"] = await VaultScope.load(vault)
        mismatch = await execute_with_retry(fill, {"ref": user_ref, "credential": "elsewhere", "field": "password"}, ctx)
        assert not mismatch.ok and "only for erp.example.com" in mismatch.observation
        assert await session.page.input_value("#u") == ""

        # read-only mode (the auditor) may only sign in: the notes textarea and the checkbox of this login page are refused
        ctx.extras["read_only"] = True
        for ref in (notes_ref, box_ref):
            readonly = await execute_with_retry(fill, {"ref": ref, "credential": "erp", "field": "password"}, ctx)
            assert not readonly.ok and readonly.error == "read_only_refused"
        assert await session.page.input_value("#n") == ""
    finally:
        await close_browser(ctx)


async def test_fill_secret_in_read_only_mode_only_signs_in(vault, tmp_path, browser_pool, site):
    scope = await make_scope(vault)
    ctx = make_ctx(tmp_path, browser_pool, scope)
    registry = build_registry(ctx.settings)
    fill, navigate, click = (registry.get(n) for n in ("browser_fill_secret", "browser_navigate", "browser_click"))
    args = lambda ref, field: {"ref": ref, "credential": "erp", "field": field}  # noqa: E731
    try:
        ctx.extras["read_only"] = True
        # pages that are not a sign-in form: a settings form ("Save settings") and an account-creation form ("Sign up")
        page = await execute_with_retry(navigate, {"url": f"{site}/prefilled"}, ctx)
        refused = await execute_with_retry(fill, args(ref_of(page.observation, '"API token"'), "password"), ctx)
        assert not refused.ok and refused.error == "read_only_refused" and "sign-in form" in refused.observation
        assert await ctx.extras["browser"].page.input_value("input[name=token]") == ""
        page = await execute_with_retry(navigate, {"url": f"{site}/register"}, ctx)
        for label, field in (('"Username"', "username"), ('"Password"', "password")):
            refused = await execute_with_retry(fill, args(ref_of(page.observation, label), field), ctx)
            assert not refused.ok and refused.error == "read_only_refused"
        assert [await ctx.extras["browser"].page.input_value(sel) for sel in ("#u", "#p")] == ["", ""]

        # the real sign-in form: both fields can be filled and the Sign in click (READ) goes through
        page = await execute_with_retry(navigate, {"url": f"{site}/login"}, ctx)
        user = await execute_with_retry(fill, args(ref_of(page.observation, '"Username"'), "username"), ctx)
        password = await execute_with_retry(fill, args(ref_of(page.observation, '"Password"'), "password"), ctx)
        assert user.ok and password.ok, password.observation
        assert "•••• (credential erp.password)" in password.observation
        session = ctx.extras["browser"]
        assert await session.page.input_value("#p") == PASSWORD and await session.page.input_value("#u") == USERNAME
        sign_in = ref_of(password.observation, 'button "Sign in"')
        assert click.classify({"ref": sign_in}, ctx) is Risk.READ
        welcome = await execute_with_retry(click, {"ref": sign_in}, ctx)
        assert welcome.ok and f"Signed in as {USERNAME}" in welcome.observation
        assert PASSWORD not in welcome.observation  # the page echoes the password: it is scrubbed

        # but the account-creation click is still refused
        page = await execute_with_retry(navigate, {"url": f"{site}/register"}, ctx)
        refused = await execute_with_retry(click, {"ref": ref_of(page.observation, '"Sign up"')}, ctx)
        assert not refused.ok and refused.error == "read_only_refused"
    finally:
        await close_browser(ctx)


async def test_approval_preview_masks_fields_holding_a_secret(vault, tmp_path, browser_pool, site):
    from urllib.parse import quote
    scope = await make_scope(vault)
    ctx = make_ctx(tmp_path, browser_pool, scope)
    registry = build_registry(ctx.settings)
    try:
        page = await execute_with_retry(registry.get("browser_navigate"), {"url": f"{site}/prefilled?v={quote(API_KEY)}"}, ctx)
        assert API_KEY not in page.observation  # already scrubbed from what the model reads
        save = ref_of(page.observation, '"Save settings"')
        click = ClickTool()
        assert click.classify({"ref": save}, ctx) is Risk.WRITE
        rows = await click.approval_preview({"ref": save}, ctx)
        assert rows == [{"label": "API token", "value": "[REDACTED:erp-api.api_key]"}]
        assert API_KEY not in json.dumps(rows)
    finally:
        await close_browser(ctx)


# ---------------------------------------------------------------- http_request(auth_credential)


def _mock_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr("worker.tools.http.httpx.AsyncClient", lambda **kw: real_client(transport=transport, **kw))


async def test_http_auth_credential_injects_the_header_without_exposing_it(vault, tmp_path, monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"echo": request.headers.get("x-api-key"), "ok": True})  # a server that echoes it

    _mock_http(monkeypatch, handler)
    ctx = make_ctx(tmp_path, scope=await make_scope(vault))
    tool = HttpRequestTool()
    args = {"method": "GET", "url": "http://localhost:8001/api/bills", "auth_credential": "erp-api",
            "headers": {"x-api-key": "model-guess", "Accept": "application/json"}}
    result = await execute_with_retry(tool, args, ctx)

    assert result.ok and len(seen) == 1
    assert seen[0].headers["X-API-Key"] == API_KEY and seen[0].headers["Accept"] == "application/json"
    assert "model-guess" not in seen[0].headers.values()  # the vault header replaces what the model set
    assert API_KEY not in result.observation and "[REDACTED:erp-api.api_key]" in result.observation
    assert tool.describe_action(args, ctx).endswith("using credential erp-api") and API_KEY not in tool.describe_action(args, ctx)


async def test_http_auth_credential_refusals(vault, tmp_path, monkeypatch):
    seen: list[httpx.Request] = []
    _mock_http(monkeypatch, lambda request: (seen.append(request), httpx.Response(200, json={}))[1])
    scope = await make_scope(vault)
    ctx = make_ctx(tmp_path, scope=scope, HTTP_ALLOWED_HOSTS="localhost,127.0.0.1")
    tool = HttpRequestTool()

    # erp-api is bound to localhost: the allowlist alone would let it go to 127.0.0.1
    other_host = await execute_with_retry(tool, {"method": "GET", "url": "http://127.0.0.1:8001/x", "auth_credential": "erp-api"}, ctx)
    assert not other_host.ok and other_host.error == "credential_host_mismatch" and API_KEY not in other_host.observation
    unknown = await execute_with_retry(tool, {"method": "GET", "url": "http://localhost:8001/x", "auth_credential": "nope"}, ctx)
    assert not unknown.ok and unknown.error == "unknown_credential" and "erp-api" in unknown.observation
    assert seen == []  # nothing was sent in either case

    ok = await execute_with_retry(tool, {"method": "GET", "url": "http://localhost:8001/x"}, ctx)  # no credential: as before
    assert ok.ok and "x-api-key" not in seen[0].headers

    await vault.put("default", "svc", "header", {"header_name": "X-Token", "value": "tok-123456789"})
    ctx.extras["vault_scope"] = await VaultScope.load(vault)
    await execute_with_retry(tool, {"method": "GET", "url": "http://localhost:8001/x", "auth_credential": "svc"}, ctx)
    assert seen[-1].headers["X-Token"] == "tok-123456789"


# ---------------------------------------------------------------- the runner end to end


def runner_settings(tmp_path) -> Settings:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    return Settings(_env_file=None, LLM_PROVIDER="fake", APPROVAL_MODE="auto", RUNS_DIR=str(tmp_path / "runs"),
                    WORKSPACE_DIR=str(tmp_path / "workspace"), HEADLESS=True, LLM_MIN_INTERVAL_S=0.0)


async def test_login_run_never_exposes_the_password_anywhere(vault, tmp_path, browser_pool, site):
    scope = await make_scope(vault)
    settings = runner_settings(tmp_path)
    llm = scripted(
        lambda _: reply(call("browser_navigate", url=f"{site}/login")),
        lambda obs: reply(call("browser_fill_secret", ref=ref_of(obs, '"Username"'), credential="erp", field="username")),
        lambda obs: reply(call("browser_fill_secret", ref=ref_of(obs, '"Password"'), credential="erp", field="password")),
        lambda obs: reply(call("browser_click", ref=ref_of(obs, '"Sign in"'))),  # the page echoes the password back
        lambda obs: reply(call("remember", key="welcome", value=obs.split("You sent password:")[1].split("\n")[0], source="page")),
        lambda _: blocked(),
    )
    store = FileRunStore(tmp_path / "runs")
    runner = AgentRunner("Log in to the ERP.", llm, build_registry(settings), settings, AutoBroker(), run_id="login1",
                         store=store, vault_scope=scope)
    runner.ctx.extras[POOL_KEY] = browser_pool
    events = []

    async def sink(event_type, payload):
        events.append(json.dumps(payload, default=str))

    runner._sink = sink
    state = await runner.run()

    echoed = next(s for s in state.steps if s.tool == "browser_click")
    assert echoed.ok and "You sent password: [REDACTED:erp.password]" in echoed.observation
    fill_steps = [s for s in state.steps if s.tool == "browser_fill_secret"]
    assert len(fill_steps) == 2 and all(s.ok for s in fill_steps)
    assert "•••• (credential erp.password)" in fill_steps[1].observation
    assert fill_steps[1].args == {"ref": fill_steps[1].args["ref"], "credential": "erp", "field": "password"}

    # nowhere: not the stored files (run.json, events.jsonl, runtime.json, report.md), not the in-memory state,
    # not the event sink, not a single message the model received
    for needle in (PASSWORD, "S3cret"):
        assert needle not in run_files_text(tmp_path / "runs" / "login1")
        assert needle not in json.dumps(state.to_dict(), default=str)
        assert needle not in "\n".join(events)
        assert needle not in all_text(llm)
    assert "Credentials are available in the secure vault: erp (ERP ledger login), erp-api (ERP REST API)" in all_text(llm)
    assert "you never see the values" in all_text(llm)
    assert state.facts["welcome"].value.strip() == "[REDACTED:erp.password]"  # remember() of a leaked value is redacted too


async def test_remember_secret_is_stored_redacted(vault, tmp_path):
    scope = await make_scope(vault)
    settings = runner_settings(tmp_path)
    llm = scripted(
        lambda _: reply(call("remember", key="erp_password", value=PASSWORD, source=f"typed by the user: {PASSWORD}")),
        lambda _: blocked(),
    )
    runner = AgentRunner(f"Use the password {PASSWORD}.", llm, build_registry(settings), settings, AutoBroker(),
                         run_id="mem1", store=FileRunStore(tmp_path / "runs"), vault_scope=scope)
    state = await runner.run()

    assert state.facts["erp_password"].value == "[REDACTED:erp.password]"  # in memory ...
    assert PASSWORD not in run_files_text(tmp_path / "runs" / "mem1")  # ... in the run, events and report files ...
    assert PASSWORD not in all_text(llm)  # ... and in what the model sees on the next turn, task text included
    assert "[REDACTED:erp.password]" in all_text(llm)
    assert state.task == "Use the password [REDACTED:erp.password]."


async def test_http_run_hides_the_api_key_from_steps_events_and_the_model(vault, tmp_path, monkeypatch):
    _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"seen_key": request.headers.get("x-api-key")}))
    scope = await make_scope(vault)
    settings = runner_settings(tmp_path)
    llm = scripted(
        lambda _: reply(call("list_credentials")),
        lambda _: reply(call("http_request", method="GET", url="http://localhost:8001/api/bills", auth_credential="erp-api")),
        lambda _: blocked(),
    )
    runner = AgentRunner("Fetch the bills.", llm, build_registry(settings), settings, AutoBroker(), run_id="http1",
                         store=FileRunStore(tmp_path / "runs"), vault_scope=scope)
    state = await runner.run()

    http_step = next(s for s in state.steps if s.tool == "http_request")
    assert http_step.ok and "[REDACTED:erp-api.api_key]" in http_step.observation
    assert API_KEY not in run_files_text(tmp_path / "runs" / "http1") and API_KEY not in all_text(llm)
    listing = next(s for s in state.steps if s.tool == "list_credentials")
    assert "erp-api (api_key): ERP REST API" in listing.observation


async def test_approval_payload_and_events_are_redacted(vault, tmp_path):
    scope = await make_scope(vault)
    settings = runner_settings(tmp_path).model_copy(update={"APPROVAL_MODE": "confirm_all"})
    seen: list[dict] = []

    def approve(kind, payload):
        seen.append(payload)
        return {"approved": True}

    llm = scripted(
        lambda _: reply(call("write_file", path="notes.txt", content=f"password is {PASSWORD}")),
        lambda _: blocked(),
    )
    runner = AgentRunner("Write a note.", llm, build_registry(settings), settings, AutoBroker(approve), run_id="appr1",
                         store=FileRunStore(tmp_path / "runs"), vault_scope=scope)
    await runner.run()
    assert seen and PASSWORD not in json.dumps(seen) and "[REDACTED:erp.password]" in json.dumps(seen[0])
    assert PASSWORD not in run_files_text(tmp_path / "runs" / "appr1") and PASSWORD not in all_text(llm)


# ---------------------------------------------------------------- v1 behavior is unchanged without credentials


async def test_without_vault_credentials_the_run_is_exactly_v1(tmp_path):
    settings = runner_settings(tmp_path)
    for scope in (None, VaultScope("default")):  # no vault at all, and an empty vault
        llm = scripted(lambda _: reply(call("list_files")), lambda _: blocked())
        store = FileRunStore(tmp_path / "runs")
        runner = AgentRunner("List the files.", llm, build_registry(settings), settings, AutoBroker(), store=store,
                             vault_scope=scope)
        assert runner.redactor is None and runner.store is store
        assert "redactor" not in runner.ctx.extras
        state = await runner.run()
        assert state.steps[0].ok
        text = all_text(llm)
        assert "secure vault" not in text and "[REDACTED" not in text
        assert f"- Shared workspace folder (list_files/read_file/write_file): {settings.WORKSPACE_DIR}" in text

    listing = await execute_with_retry(build_registry(settings).get("list_credentials"), {}, runner.ctx)
    assert listing.ok and "No credentials" in listing.observation

    assert build_messages(RunState("r", "t"), settings, ["a"])[1].content == \
        build_messages(RunState("r", "t"), settings, ["a"], vault_credentials=[])[1].content


async def test_v1_credentials_file_is_still_readable_by_the_agent(tmp_path):
    settings = runner_settings(tmp_path)
    (tmp_path / "workspace" / "credentials.json").write_text('{"erp": {"password": "ledger-pass-789"}}', encoding="utf-8")
    llm = scripted(lambda _: reply(call("read_file", path="credentials.json")), lambda _: blocked())
    runner = AgentRunner("Read the credentials.", llm, build_registry(settings), settings, AutoBroker(),
                         store=FileRunStore(tmp_path / "runs"))
    state = await runner.run()
    assert "ledger-pass-789" in state.steps[0].observation


# ---------------------------------------------------------------- server wiring


def test_server_gives_runs_the_default_owner_scope_only_when_the_vault_has_credentials(tmp_path):
    import asyncio
    import time

    settings = Settings(_env_file=None, LLM_PROVIDER="fake", RUNS_DIR=str(tmp_path / "runs"),
                        WORKSPACE_DIR=str(tmp_path / "workspace"), VAULT_DIR=str(tmp_path / "nonexistent"),
                        SIM_BASE_URL="http://127.0.0.1:9", LLM_MIN_INTERVAL_S=0.0)
    (tmp_path / "workspace").mkdir()
    vault = Vault(FileVaultBackend(tmp_path / "vault"), generate_key())

    def start(app_vault) -> AgentRunner:
        llm = ScriptedLLM(lambda messages, tools: blocked())
        app = create_app(settings, llm_factory=lambda s: llm, registry_factory=build_registry, vault=app_vault)
        with TestClient(app) as client:
            run_id = client.post("/api/runs", json={"task": "t"}).json()["run_id"]
            runner = app.state.sessions[run_id].runner
            deadline = time.monotonic() + 10
            while app.state.sessions[run_id].active and time.monotonic() < deadline:
                time.sleep(0.05)
            return runner

    assert start(None).vault_scope is None  # no vault directory: v1
    assert start(vault).vault_scope is None  # an empty vault: v1
    asyncio.run(vault.put("default", "erp", "login", {"password": PASSWORD}))
    runner = start(vault)
    assert runner.vault_scope.names() == ["erp"] and runner.redactor
    assert not (tmp_path / "nonexistent").exists()
