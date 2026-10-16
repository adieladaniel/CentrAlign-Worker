"""PostgreSQL layer: migrations, PgRunStore specifics (events, notify, org isolation), PgVaultBackend, full-text search
and the bootstrap. The RunStore behaviour shared with FileRunStore is tested in test_store.py.

These tests need a reachable server (``DATABASE_URL``); without one they are skipped. They use ``oneforall_test``."""
from __future__ import annotations

import asyncio
import uuid

import asyncpg
import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from worker.agent.state import Fact, RunState, Step
from worker.config import Settings
from worker.db import migrate
from worker.db.bootstrap import bootstrap, drop_database
from worker.db.models import DEFAULT_ORG_SETTINGS, Base, Membership, Org, OrgMemory, Role, Run, User
from worker.db.seed import verify_password
from worker.db.session import DatabaseNotConfigured, get_engine, mask_url, normalize_url
from worker.db.util import parse_ts
from worker.llm.base import LLMResponse, ToolCall, Usage
from worker.llm.fake import ScriptedLLM
from worker.server import create_app
from worker.store_pg import PgRunStore, UnknownRun
from worker.tools.control import FinishTool
from worker.tools.registry import ToolRegistry
from worker.vault import Vault, VaultError, generate_key
from worker.vault_pg import PgVaultBackend

PASSWORD = "S3cret!pw#42 &x"


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------- no database needed


def test_mask_url_hides_the_password_and_normalizes_the_driver():
    for raw in ("postgresql+asyncpg://postgres:hunter2@localhost:5432/oneforall",
                "postgresql://postgres:hunter2@localhost:5432/oneforall", "postgres://postgres:hunter2@localhost/oneforall"):
        masked = mask_url(raw)
        assert "hunter2" not in masked and masked.startswith("postgresql+asyncpg://postgres:***@localhost")
    with pytest.raises(ValueError):
        normalize_url("mysql://u:p@h/db")


def test_no_database_url_means_file_mode(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)  # (CI and the test database runs export it)
    assert Settings(_env_file=None).DATABASE_URL == ""
    monkeypatch.setattr("worker.db.session._engine", None)
    with pytest.raises(DatabaseNotConfigured):
        get_engine(Settings(_env_file=None))


async def test_drop_database_refuses_anything_but_the_project_databases():
    with pytest.raises(ValueError, match="refusing to drop"):
        await drop_database("postgresql+asyncpg://u:p@localhost/postgres")
    with pytest.raises(ValueError, match="refusing to drop"):
        await drop_database("postgresql+asyncpg://u:p@localhost/production")


def test_password_hashes_verify():
    from worker.db.seed import hash_password
    hashed = hash_password("correct horse")
    assert hashed.startswith("$argon2") and verify_password(hashed, "correct horse")
    assert not verify_password(hashed, "wrong") and not verify_password("not-a-hash", "x")


# ---------------------------------------------------------------- migrations and constraints


async def test_migrations_match_the_models_and_roll_back_and_forward(pg_required_engine):
    head = migrate.head_revision()
    assert await migrate.current_revision(pg_required_engine) == head

    async with pg_required_engine.connect() as conn:  # no drift between models.py and the migration scripts
        diff = await conn.run_sync(lambda c: compare_metadata(MigrationContext.configure(c, {"compare_type": True}), Base.metadata))
    assert diff == []

    async def tables() -> set[str]:
        async with pg_required_engine.connect() as conn:
            rows = await conn.execute(text("select tablename from pg_tables where schemaname = 'public'"))
            return {r[0] for r in rows}

    expected = {t.name for t in Base.metadata.sorted_tables}
    assert (await tables()) == expected | {"alembic_version"}
    try:
        await migrate.downgrade(pg_required_engine, "base")
        assert await tables() == {"alembic_version"} and await migrate.current_revision(pg_required_engine) is None
    finally:
        await migrate.upgrade(pg_required_engine)
    assert await tables() == expected | {"alembic_version"} and await migrate.current_revision(pg_required_engine) == head


async def test_schema_has_the_indexes_and_the_generated_search_column(pg_required_engine):
    async with pg_required_engine.connect() as conn:
        indexes = {r[0]: r[1] for r in await conn.execute(text("select indexname, indexdef from pg_indexes where schemaname = 'public'"))}
        generated = (await conn.execute(text(
            "select is_generated, generation_expression from information_schema.columns "
            "where table_name = 'org_memory' and column_name = 'search'"))).one()
    assert "USING gin (search)" in indexes["ix_org_memory_search"]
    assert "WHERE (status = 'queued'::text)" in indexes["ix_runs_queue"] or "WHERE ((status)::text = 'queued'::text)" in indexes["ix_runs_queue"]
    assert "queued_at DESC" in indexes["ix_runs_org_id_queued_at"] and "lease_expires_at" in indexes["ix_runs_lease_expires_at"]
    assert "lower(email)" in indexes["uq_users_email_lower"] and "UNIQUE" in indexes["uq_users_email_lower"]
    assert generated[0] == "ALWAYS" and "to_tsvector" in generated[1]


async def test_constraints_reject_bad_roles_statuses_and_duplicate_emails(pg_sessionmaker):
    async with pg_sessionmaker() as session, session.begin():
        org, user = Org(slug="a", name="A"), User(email="Ann@Example.test", password_hash="x")
        session.add_all([org, user])

    async def fails(*rows) -> None:
        with pytest.raises(IntegrityError):
            async with pg_sessionmaker() as session, session.begin():
                session.add_all(rows)

    await fails(User(email="ann@example.TEST", password_hash="y"))  # case-insensitive unique e-mail
    await fails(Org(slug="a", name="again"))  # unique slug
    await fails(Membership(org_id=org.id, user_id=user.id, role="superuser"))
    await fails(Run(org_id=org.id, task="t", status="exploded"))
    assert {r.value for r in Role} == {"owner", "admin", "operator", "approver", "viewer"}
    async with pg_sessionmaker() as session:
        stored = (await session.scalars(select(Org))).one()
    assert stored.settings == DEFAULT_ORG_SETTINGS  # the application fills the defaults on insert


# ---------------------------------------------------------------- PgRunStore


async def test_event_sequence_is_gapless_and_unique_under_concurrency(pg_sessionmaker, make_org):
    org_id, user_id = await make_org()
    store = PgRunStore(pg_sessionmaker, org_id, user_id)
    run_id = new_id()
    await store.create_run(RunState(run_id=run_id, task="t"))

    # 20 writers through 20 store instances (like 20 processes), all at once
    seqs = await asyncio.gather(*(store.for_org(org_id, user_id).append_event(run_id, "tick", {"n": n}) for n in range(20)))
    assert sorted(seqs) == list(range(1, 21))
    events = await store.read_events(run_id)
    assert [e["seq"] for e in events] == list(range(1, 21)) and sorted(e["payload"]["n"] for e in events) == list(range(20))
    assert await store.append_event(run_id, "next", {}) == 21


async def test_append_event_notifies_listeners_with_run_id_and_seq(pg_required_engine, pg_sessionmaker, make_org):
    org_id, user_id = await make_org()
    store = PgRunStore(pg_sessionmaker, org_id, user_id)
    run_id = new_id()
    await store.create_run(RunState(run_id=run_id, task="t"))

    url = pg_required_engine.url
    raw = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username, password=url.password, database=url.database)
    received: asyncio.Queue[str] = asyncio.Queue()
    try:
        await raw.add_listener("run_events", lambda conn, pid, channel, payload: received.put_nowait(payload))
        seq = await store.append_event(run_id, "thought", {"text": "hi"})
        assert await asyncio.wait_for(received.get(), 5) == f"{run_id}:{seq}"
        assert await store.append_event(run_id, "thought", {}) == 2
        assert await asyncio.wait_for(received.get(), 5) == f"{run_id}:2"
    finally:
        await raw.close()


async def test_a_store_only_sees_and_touches_its_own_orgs_runs(pg_sessionmaker, make_org):
    org_a, user_a = await make_org("acme")
    org_b, user_b = await make_org("globex")
    store_a, store_b = PgRunStore(pg_sessionmaker, org_a, user_a), PgRunStore(pg_sessionmaker, org_b, user_b)
    run_a, run_b = new_id(), new_id()
    state = RunState(run_id=run_a, task="Acme's secret task")
    state.add_step(Step(tool="read_note", observation="acme data"))
    await store_a.create_run(state)
    await store_a.save_runtime(run_a, {"turn": 1})
    await store_a.append_event(run_a, "run_started", {})
    await store_a.save_report(run_a, {"summary": "s"}, "# report")
    await store_b.create_run(RunState(run_id=run_b, task="Globex task"))

    # reads: it simply does not exist for org B
    assert await store_b.load_state(run_a) is None and await store_b.load_runtime(run_a) is None
    assert await store_b.load_report_markdown(run_a) is None and await store_b.read_events(run_a) == []
    assert [r["id"] for r in await store_b.list_runs()] == [run_b] and [r["id"] for r in await store_a.list_runs()] == [run_a]
    assert await store_b.list_runs(org_id=str(org_a)) == [] and len(await store_b.list_runs(org_id=str(org_b))) == 1

    # writes: refused, and nothing changes
    for write in (store_b.append_step(run_a, Step(tool="x")), store_b.upsert_fact(run_a, Fact("k", "v")),
                  store_b.append_event(run_a, "evil", {}), store_b.save_runtime(run_a, {"turn": 99}),
                  store_b.save_report(run_a, {"summary": "pwned"}, "pwned"),
                  store_b.save_state(RunState(run_id=run_a, task="hijacked")),
                  store_b.create_run(RunState(run_id=run_a, task="duplicate id"))):
        with pytest.raises((UnknownRun, IntegrityError)):
            await write
    loaded = await store_a.load_state(run_a)
    assert loaded.task == "Acme's secret task" and len(loaded.steps) == 1 and not loaded.facts
    assert await store_a.load_runtime(run_a) == {"turn": 1} and await store_a.load_report_markdown(run_a) == "# report"
    assert [e["type"] for e in await store_a.read_events(run_a)] == ["run_started"]

    # a scoped store is cheap to make per request, and malformed ids are just "not found"
    assert (await store_a.for_org(org_b, user_b).load_state(run_a)) is None
    assert await store_a.load_state("../../etc/passwd") is None and await store_a.read_events("nope") == []


async def test_nul_characters_and_odd_values_are_stored_safely(pg_sessionmaker, make_org):
    org_id, user_id = await make_org()
    store = PgRunStore(pg_sessionmaker, org_id, user_id)
    run_id = new_id()
    await store.create_run(RunState(run_id=run_id, task="t"))
    await store.append_step(run_id, Step(index=1, observation="a\x00b", args={"x": "c\x00d", "when": parse_ts("2026-01-01T00:00:00+00:00")}))
    await store.append_event(run_id, "e", {"text": "e\x00f"})
    state = await store.load_state(run_id)
    assert state.steps[0].observation == "ab" and state.steps[0].args["x"] == "cd"
    assert (await store.read_events(run_id))[0]["payload"] == {"text": "ef"}


async def test_run_rows_carry_queue_bookkeeping_and_header_updates_keep_it(pg_sessionmaker, make_org):
    org_id, user_id = await make_org()
    store = PgRunStore(pg_sessionmaker, org_id, user_id)
    run_id = new_id()
    state = RunState(run_id=run_id, task="t")
    await store.create_run(state)
    await store.save_runtime(run_id, {"turn": 2, "resume_count": 3})
    state.status = "running"
    await store.save_state(state)
    async with pg_sessionmaker() as session:
        run = (await session.scalars(select(Run))).one()
    assert (run.org_id, run.created_by, run.status, run.resume_count) == (org_id, user_id, "running", 3)
    assert run.runtime == {"turn": 2, "resume_count": 3}  # save_state did not clobber the checkpoint
    assert run.attempts == 0 and run.priority == 0 and not run.cancel_requested and run.lease_owner is None
    assert run.queued_at == parse_ts(state.started_at)


async def test_create_app_accepts_a_pg_store(pg_sessionmaker, make_org, tmp_path):
    org_id, user_id = await make_org()
    store = PgRunStore(pg_sessionmaker, org_id, user_id, artifacts_root=tmp_path / "artifacts")
    (tmp_path / "ws").mkdir()
    settings = Settings(_env_file=None, LLM_PROVIDER="fake", RUNS_DIR=str(tmp_path / "unused"), WORKSPACE_DIR=str(tmp_path / "ws"),
                        SIM_BASE_URL="http://127.0.0.1:9", LLM_MIN_INTERVAL_S=0.0, VAULT_DIR=str(tmp_path / "novault"))

    def registry_factory(_settings):
        registry = ToolRegistry()
        registry.register(FinishTool())
        return registry

    finish = ToolCall(id="c1", name="finish", args={"status": "blocked", "summary": "Nothing to do."})
    llm = ScriptedLLM([LLMResponse(text="", tool_calls=[finish], usage=Usage(1, 1))])
    app = create_app(settings, llm_factory=lambda s: llm, registry_factory=registry_factory, store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        run_id = (await client.post("/api/runs", json={"task": "Idle"})).json()["run_id"]
        for _ in range(200):  # the status is saved before the report, so wait for the report itself
            detail = (await client.get(f"/api/runs/{run_id}")).json()
            if detail.get("report_url"):
                break
            await asyncio.sleep(0.05)
        assert detail["report"]["status"] == "needs_attention"
        assert "Nothing to do." in (await client.get(detail["report_url"])).text
    stored = await PgRunStore(pg_sessionmaker, org_id).load_state(run_id)
    assert stored.status == "needs_attention" and not (tmp_path / "unused").exists()


# ---------------------------------------------------------------- PgVaultBackend


async def test_pg_vault_round_trip_stores_only_ciphertext(pg_sessionmaker):
    vault = Vault(PgVaultBackend(pg_sessionmaker), generate_key())
    info = await vault.put("org-1", "erp", "login", {"username": "ap.clerk", "password": PASSWORD},
                           description="ERP login", url="http://erp.example.com:8001/login")
    assert info["host"] == "erp.example.com" and info["fields"] == ["password", "username"]
    again = await vault.put("org-1", "erp", "login", {"username": "ap.clerk", "password": PASSWORD + "2"},
                            description="ERP login", url="http://erp.example.com:8001/login")
    assert again["created_at"] == info["created_at"]
    await vault.put("org-2", "erp", "api_key", {"api_key": "other-org-key-123"})

    credential = await vault.get("org-1", "erp")
    assert credential.fields == {"username": "ap.clerk", "password": PASSWORD + "2"} and credential.host == "erp.example.com"
    assert [c["name"] for c in await vault.list_public("org-1")] == ["erp"]  # owners are isolated
    assert await vault.get("org-1", "missing") is None

    async with pg_sessionmaker() as session:  # the raw rows: metadata in clear, the secret only as a Fernet token
        rows = (await session.execute(text("select * from credentials order by owner"))).mappings().all()
    assert len(rows) == 2
    dump = repr([dict(r) for r in rows]).encode()
    assert PASSWORD.encode() not in dump and b"ap.clerk" not in dump and b"other-org-key-123" not in dump
    assert isinstance(rows[0]["ciphertext"], bytes) and rows[0]["ciphertext"].startswith(b"gAAAA")
    assert rows[0]["host"] == "erp.example.com" and rows[0]["field_names"] == ["password", "username"]

    wrong = Vault(PgVaultBackend(pg_sessionmaker), generate_key())
    with pytest.raises(VaultError, match="wrong VAULT_KEY"):
        await wrong.get("org-1", "erp")

    new_key = await vault.rotate_key()  # replace_all in one transaction
    assert (await Vault(PgVaultBackend(pg_sessionmaker), new_key).get("org-1", "erp")).fields["password"] == PASSWORD + "2"
    assert await vault.delete("org-1", "erp") and not await vault.delete("org-1", "erp")
    assert [r["owner"] for r in await PgVaultBackend(pg_sessionmaker).all()] == ["org-2"]


# ---------------------------------------------------------------- full-text search on org memory


async def test_org_memory_full_text_search_ranks_the_right_row_first(pg_sessionmaker, make_org):
    org_id, _ = await make_org()
    other_org, _ = await make_org("globex")
    async with pg_sessionmaker() as session, session.begin():
        session.add_all([
            OrgMemory(org_id=org_id, key="erp invoice approval", content="Invoices above 5000 EUR need a second approver in the ERP.", scope="erp"),
            OrgMemory(org_id=org_id, key="portal login", content="The vendor portal asks for a one time code after the password.", scope="portal"),
            OrgMemory(org_id=org_id, key="mail archive", content="Archive supplier mails older than 30 days into the shared folder.", scope="mail"),
            OrgMemory(org_id=org_id, key="payment terms", content="An invoice is due 30 days after receipt.", scope="erp"),
            OrgMemory(org_id=other_org, key="erp invoice approval", content="Globex approves invoices automatically.", scope="erp"),
        ])

    async def search(query: str, org=org_id) -> list[str]:
        async with pg_sessionmaker() as session:
            rows = await session.execute(text(
                "select key from org_memory where org_id = :org and search @@ websearch_to_tsquery('english', :q) "
                "order by ts_rank(search, websearch_to_tsquery('english', :q)) desc"), {"org": org, "q": query})
            return [r[0] for r in rows]

    assert await search("approving invoices in the ERP") == ["erp invoice approval"]
    assert (await search("code for the portal login"))[0] == "portal login"
    assert await search("invoice") == ["erp invoice approval", "payment terms"]  # a hit in the key outranks one in the text
    assert await search("supplier mails archive") == ["mail archive"]
    assert await search("quantum physics") == []
    assert await search("invoices", other_org) == ["erp invoice approval"]  # scoped to the org
    async with pg_sessionmaker() as session:
        await session.execute(text("set enable_seqscan = off"))
        plan = "\n".join(r[0] for r in await session.execute(text(
            "explain select 1 from org_memory where search @@ websearch_to_tsquery('english', 'invoice')")))
    assert "ix_org_memory_search" in plan


# ---------------------------------------------------------------- bootstrap


async def test_bootstrap_is_idempotent(pg_required_engine, pg_sessionmaker):
    url = pg_required_engine.url
    first = await bootstrap(url, demo_password="Demo-Pass-1")
    assert not first.created_database and first.revision == migrate.head_revision()
    assert sorted(first.seed.orgs_created) == ["acme", "globex"] and len(first.seed.users_created) == 7
    assert first.seed.users_created["approver@globex.test"] == "approver" and first.password == "Demo-Pass-1"

    second = await bootstrap(url, demo_password="Another-Pass-2")
    assert second.seed.orgs_created == [] and second.seed.users_created == {} and second.password is None
    assert second.seed.org_ids == first.seed.org_ids and second.seed.memberships_created == 0

    async with pg_sessionmaker() as session:
        counts = {t: (await session.execute(text(f"select count(*) from {t}"))).scalar_one() for t in ("orgs", "users", "memberships")}
        users = (await session.scalars(select(User).order_by(User.email))).all()
        roles = {(m.org_id, m.role) for m in (await session.scalars(select(Membership))).all()}
    assert counts == {"orgs": 2, "users": 7, "memberships": 7}
    assert all(verify_password(u.password_hash, "Demo-Pass-1") for u in users)  # the second run did not change passwords
    assert not any(verify_password(u.password_hash, "Another-Pass-2") for u in users)
    assert {role for _, role in roles} == {"owner", "admin", "operator", "approver", "viewer"}
    assert "Demo-Pass-1" not in repr([u.password_hash for u in users])  # hashed, not stored
