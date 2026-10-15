"""Helpers for the multi-user (database mode) tests: a seeded two-org world, an app wired to it, signed-in HTTP clients
over ``httpx.ASGITransport`` (the asyncpg pool must stay on the test's own event loop, which TestClient's thread would
break), a WebSocket driver for ASGI apps, and a scripted LLM that walks a run through approvals.

Everything runs against the ``centralign_test`` database with a scripted LLM, a tiny browser-less tool registry and no
simulated company (a fake tenant id and entry token stand in for a provisioned environment).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.config import Settings
from worker.db.models import DEFAULT_ORG_SETTINGS, Membership, Org, User
from worker.db.seed import hash_password
from worker.llm.base import LLMResponse, ToolCall, Usage
from worker.llm.fake import ScriptedLLM
from worker.server import create_app
from worker.tools.base import Risk, Tool, ToolContext, ToolResult
from worker.tools.control import FinishTool, UpdatePlanTool
from worker.tools.files import ListFilesTool, WriteFileTool
from worker.tools.interaction import AskUserTool
from worker.tools.registry import ToolRegistry
from worker.vault import Vault, generate_key
from worker.vault_pg import PgVaultBackend

PASSWORD = "Correct-Horse-1"
ENTRY_TOKENS = {"acme": "acme-entry-token-Q7xN3pLm9", "globex": "globex-entry-token-B2vK8rTz4"}
ERP_PASSWORDS = {"acme": "acme-erp-secret-81", "globex": "globex-erp-secret-52"}
FINISH_CALLS = itertools.count(1)
_hash: str | None = None

# slug -> [(local part, role, display name)]
MEMBERS = {
    "acme": [("owner", "owner", "Olivia Owner"), ("admin", "admin", "Adam Admin"), ("operator", "operator", "Omar Operator"),
             ("operator2", "operator", "Olga Operator"), ("approver", "approver", "Alice Approver"),
             ("viewer", "viewer", "Victor Viewer")],
    "globex": [("operator", "operator", "Gina Operator"), ("approver", "approver", "Gus Approver"),
               ("admin", "admin", "Gwen Admin")],
}
MULTI_EMAIL = "multi@acme.test"  # operator in acme (oldest membership), approver in globex


def password_hash() -> str:
    """One argon2 hash for every test user (hashing is deliberately slow)."""
    global _hash
    if _hash is None:
        _hash = hash_password(PASSWORD)
    return _hash


@dataclass
class World:
    sm: async_sessionmaker[AsyncSession]
    vault: Vault
    org_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    user_ids: dict[str, uuid.UUID] = field(default_factory=dict)  # by e-mail

    def email(self, slug: str, local: str) -> str:
        return f"{local}@{slug}.test"


async def seed_world(sm: async_sessionmaker[AsyncSession], *, environments: bool = True) -> World:
    """Two orgs (acme, globex) with users in every role, a multi-org user, and (``environments``) a stand-in
    environment: a tenant id on the org and the vault entries a provisioned org has (entry token, ERP login)."""
    vault = Vault(PgVaultBackend(sm), generate_key())
    world = World(sm=sm, vault=vault)
    async with sm() as session, session.begin():
        for slug, name in (("acme", "Acme Corp"), ("globex", "Globex")):
            org = Org(slug=slug, name=name, settings=dict(DEFAULT_ORG_SETTINGS),
                      sim_tenant_id=f"org-{slug}" if environments else None)
            session.add(org)
            await session.flush()
            world.org_ids[slug] = org.id
        for slug, members in MEMBERS.items():
            for local, role, display in members:
                user = User(email=f"{local}@{slug}.test", display_name=display, password_hash=password_hash())
                session.add(user)
                await session.flush()
                world.user_ids[user.email] = user.id
                session.add(Membership(org_id=world.org_ids[slug], user_id=user.id, role=role))
        multi = User(email=MULTI_EMAIL, display_name="Mia Multi", password_hash=password_hash())
        session.add(multi)
        await session.flush()
        world.user_ids[MULTI_EMAIL] = multi.id
        session.add(Membership(org_id=world.org_ids["acme"], user_id=multi.id, role="operator"))
        await session.flush()  # acme first: the oldest membership is the one signing in picks
        session.add(Membership(org_id=world.org_ids["globex"], user_id=multi.id, role="approver"))
    if environments:
        for slug, org_id in world.org_ids.items():
            owner = str(org_id)
            await vault.put(owner, "sim_entry", "header", {"header_name": "X-Sim-Tenant", "value": ENTRY_TOKENS[slug]})
            await vault.put(owner, "erp", "login", {"username": "ap.clerk", "password": ERP_PASSWORDS[slug]},
                            description=f"{slug} ERP", url="http://localhost:8001/erp")
    return world


# --------------------------------------------------------------------------- scripted runs


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id=f"call_{next(FINISH_CALLS)}", name=name, args=args)


def reply(text: str = "", *calls: ToolCall) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls), usage=Usage(10, 5))


def blocked() -> LLMResponse:
    return reply("", call("finish", status="blocked", summary="Nothing more to do."))


class WireTool(Tool):
    """A stand-in for an irreversible action (like sending an email)."""

    name = "wire_money"
    description = "Send a payment. Cannot be undone."
    parameters = {"type": "object", "properties": {"amount": {"type": "string"}}, "required": ["amount"]}
    risk = Risk.IRREVERSIBLE
    read_only_safe = False

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, observation=f"Sent {args.get('amount')}.")


def small_registry(_settings: Settings) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (ListFilesTool(), WriteFileTool(), WireTool(), UpdatePlanTool(), FinishTool(), AskUserTool()):
        registry.register(tool)
    return registry


class Planner:
    """``llm_factory`` for the app: each run takes the next queued script (a list of responses); with none queued the
    run just finishes as blocked. Every LLM created is kept in ``created`` so tests can read what the model was sent."""

    def __init__(self) -> None:
        self.queue: list[list[LLMResponse]] = []
        self.created: list[ScriptedLLM] = []

    def add(self, *responses: LLMResponse) -> None:
        self.queue.append(list(responses))

    def __call__(self, _settings: Settings) -> ScriptedLLM:
        llm = ScriptedLLM(self.queue.pop(0) if self.queue else [blocked()])
        self.created.append(llm)
        return llm


def write_script() -> list[LLMResponse]:
    """Asks to write a file (a WRITE action), then gives up."""
    return [reply("Writing.", call("write_file", path="note.txt", content="hello")), blocked()]


def wire_script() -> list[LLMResponse]:
    """Asks to send a payment (an IRREVERSIBLE action), then gives up."""
    return [reply("Paying.", call("wire_money", amount="100 EUR")), blocked()]


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    (tmp_path / "workspace").mkdir(exist_ok=True)
    values: dict[str, Any] = dict(
        _env_file=None, AUTH_MODE="db", LLM_PROVIDER="fake", RUNS_DIR=str(tmp_path / "runs"),
        WORKSPACE_DIR=str(tmp_path / "workspace"), VAULT_DIR=str(tmp_path / "vault"), SIM_BASE_URL="http://127.0.0.1:9",
        LLM_MIN_INTERVAL_S=0.0, LOGIN_BACKOFF_BASE_S=0.0)
    values.update(overrides)
    return Settings(**values)


def make_app(world: World, tmp_path: Path, planner: Planner | None = None, **settings_overrides: Any):
    """The server in database mode over the test database; returns ``(app, planner)``."""
    planner = planner or Planner()
    app = create_app(make_settings(tmp_path, **settings_overrides), llm_factory=planner, registry_factory=small_registry,
                     vault=world.vault, sessionmaker=world.sm)
    return app, planner


# --------------------------------------------------------------------------- HTTP and WebSocket drivers


def new_client(app, ip: str = "203.0.113.7") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 51000)), base_url="http://test")


async def sign_in(app, email: str, password: str = PASSWORD, ip: str = "203.0.113.7") -> httpx.AsyncClient:
    """A client holding a valid session cookie of ``email``."""
    client = new_client(app, ip)
    resp = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return client


async def wait_until(predicate, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(0.03)
    return False


async def start_run(client: httpx.AsyncClient, task: str = "Do the thing", **body: Any) -> str:
    resp = await client.post("/api/runs", json={"task": task, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()["run_id"]


async def pending_request(client: httpx.AsyncClient, run_id: str) -> dict:
    """The run's open approval/question (waits for the agent to raise it)."""
    found: dict = {}

    async def has_pending() -> bool:
        data = (await client.get(f"/api/runs/{run_id}")).json()
        if data.get("pending"):
            found.update(data["pending"][0])
        return bool(found)

    assert await wait_until(has_pending), "the run never asked for approval"
    return found


async def finished(client: httpx.AsyncClient, run_id: str, timeout: float = 10.0) -> dict:
    """Wait until the run has ended and its report is stored; returns the run document."""
    last: dict = {}

    async def done() -> bool:
        last.update((await client.get(f"/api/runs/{run_id}")).json())
        return not last.get("active") and bool(last.get("report_url"))

    assert await wait_until(done, timeout), f"run did not finish: {last.get('status')}"
    return last


@dataclass
class WsResult:
    accepted: bool = False
    close_code: int | None = None
    messages: list[dict] = field(default_factory=list)

    @property
    def types(self) -> list[str]:
        return [m["type"] for m in self.messages]


async def ws_connect(app, path: str, cookie: str | None = None, headers: dict[str, str] | None = None,
                     timeout: float = 5.0) -> WsResult:
    """Open a WebSocket on an ASGI app, collect what the server sends and return when it closes (or after
    ``replay_done``, for runs that are still live). httpx cannot speak WebSocket over ASGITransport, so this drives the
    ASGI interface directly."""
    raw = {"host": "test", **(headers or {})}
    if cookie:
        raw["cookie"] = cookie
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "ws", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "", "subprotocols": [],
             "headers": [(k.encode(), v.encode()) for k, v in raw.items()], "client": ("203.0.113.9", 4000),
             "server": ("test", 80)}
    inbox: asyncio.Queue = asyncio.Queue()
    inbox.put_nowait({"type": "websocket.connect"})
    result = WsResult()

    async def receive() -> dict:
        return await inbox.get()

    async def send(message: dict) -> None:
        kind = message["type"]
        if kind == "websocket.accept":
            result.accepted = True
        elif kind == "websocket.send":
            data = json.loads(message["text"])
            result.messages.append(data)
            if data["type"] == "replay_done":
                inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})
        elif kind == "websocket.close":
            result.close_code = message.get("code", 1000)

    task = asyncio.ensure_future(app(scope, receive, send))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        pass  # a live run keeps the socket open; what arrived so far is the result
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return result


def session_cookie(client: httpx.AsyncClient) -> str:
    return "ca_session=" + client.cookies["ca_session"]


async def audit_actions(sm: async_sessionmaker[AsyncSession], org_id: uuid.UUID | None = None) -> list[str]:
    from worker.db.models import AuditLog

    async with sm() as session:
        query = select(AuditLog.action).order_by(AuditLog.id)
        if org_id is not None:
            query = query.where(AuditLog.org_id == org_id)
        return list((await session.scalars(query)).all())
