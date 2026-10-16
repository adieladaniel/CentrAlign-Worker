"""Long-term organization memory: reusable how-to knowledge that future runs of the same organization can recall.

What it is and is not
    ``org_memory`` rows hold short notes about the organization's *systems* ("the ERP new-bill form needs the date as
    YYYY-MM-DD"), never task data. Recall is keyword search (PostgreSQL full text: ``websearch_to_tsquery`` ranked by
    ``ts_rank_cd`` on the generated ``search`` column), not semantic search, by design.

How a note gets in (the write gate)
    During a run the agent marks facts ``durable`` (``remember(durable=true)``). They are only *candidates*, kept in the
    run's own facts, so they survive a resume. ``commit_run_memory`` is the single gate: it writes candidates when, and
    only when, the run ended ``succeeded``, which the runner only reports after the independent verifier passed. Failed,
    needs-attention and cancelled runs write nothing. Candidates are also screened one by one (``screen``): anything
    that contained a vault secret, is too long or too short, looks like task data or like an instruction aimed at the
    agent is rejected and reported. Every outcome is an event on the run (``memory_saved`` / ``memory_rejected``) and
    an audit-log row.

The gate lives here and is called by the worker (``worker/jobs.py``) after ``AgentRunner.run()`` returns, not inside the
runner: the runner stays storage-agnostic (it only knows ``ctx.extras["org_memory"]``, an object with an async
``recall``), and the one place that writes shared, long-lived data sits next to the code that knows the database, the
org and the run's creator.

Governance: a note has provenance (source run, step and source text, creator), a key that is unique per organization
(case-insensitive; saving it again updates the row and bumps the provenance), usage counters, and expires when unused for
``memory_ttl_days`` (an expired note is not recalled but stays visible to admins, who can edit or delete notes).
"""
from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import and_, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.agent.state import RunState, truncate
from worker.db.models import AuditLog, OrgMemory, User
from worker.db.util import clean_text, maybe_uuid, to_uuid

log = logging.getLogger("worker.memory")

MAX_CONTENT_CHARS = 500
MAX_KEY_CHARS = 80
MAX_SCOPE_CHARS = 40
MIN_KEY_CHARS = 3
MIN_WORDS = 3                 # a note needs at least this many words to be a useful hint
MAX_CANDIDATES_PER_RUN = 10   # a runaway (or prompt-injected) run cannot flood the memory
DEFAULT_LIMIT = 5
MAX_QUERY_WORDS = 40
AUTO_ANY_WORDS = 8            # ``auto`` matching goes straight to "any word" for queries longer than this
DEFAULT_TTL_DAYS = 180
SOURCE_LIMIT = 300

# ---- heuristics of the write gate (documented in the module docstring and in ``screen``)
DATA_TOKEN_SHARE = 0.4        # at least this share of whitespace tokens contains a digit ...
DATA_DIGIT_SHARE = 0.3        # ... or at least this share of all non-space characters is a digit: task data
_DIGIT = re.compile(r"\d")
_INSTRUCTION = re.compile(
    r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(instructions?|rules?|prompts?|guidelines?)\b"
    r"|\bsystem prompt\b|\bnew instructions?\b", re.IGNORECASE)
_MARKER = re.compile(r"\[REDACTED:")
_WORD = re.compile(r"\w+", re.UNICODE)

Match = Literal["all", "any", "auto"]
_UNSET: Any = object()


# --------------------------------------------------------------------------- screening


@dataclass
class Candidate:
    """A fact a run marked as durable: a possible long-term note."""

    key: str
    content: str
    scope: str = ""
    source: str = ""


@dataclass
class SaveResult:
    saved: list[dict] = field(default_factory=list)      # [{"key", "id", "created"}]; created=False: an existing note was updated
    rejected: list[dict] = field(default_factory=list)   # [{"key", "reason", "detail"}]


def looks_like_task_data(text: str) -> bool:
    """Is ``text`` mostly numbers, amounts, dates or IDs? Simple on purpose: it fires when at least 40% of the
    whitespace-separated tokens contain a digit, or at least 30% of the characters are digits. "Invoice INI-20502, 1,250.00
    EUR, due 2026-11-03" is task data; "the form needs the date as YYYY-MM-DD" is not."""
    tokens = text.split()
    chars = len("".join(tokens))
    if not tokens:
        return True
    with_digit = sum(1 for token in tokens if _DIGIT.search(token))
    digits = len(_DIGIT.findall(text))
    return with_digit / len(tokens) >= DATA_TOKEN_SHARE or digits / chars >= DATA_DIGIT_SHARE


def screen(candidate: Candidate, redactor: Any | None = None) -> tuple[str, str] | None:
    """``(code, explanation)`` when the candidate must not be stored, else None.

    Checks, in order: a secret (the text contains a redaction marker, or the run's ``redactor`` still finds a secret in
    it), a malformed key, content that is too long or too short, task data (``looks_like_task_data``), and text that reads
    like an instruction to the agent (a note must describe the environment, never give orders)."""
    fields = (candidate.key, candidate.content, candidate.scope, candidate.source)
    if any(_MARKER.search(text) or (redactor and redactor.redact(text) != text) for text in fields):
        return "secret", "it contained a secret (redacted); secrets never enter organization memory"
    if not MIN_KEY_CHARS <= len(candidate.key) <= MAX_KEY_CHARS:
        return "bad_key", f"the key must be {MIN_KEY_CHARS} to {MAX_KEY_CHARS} characters"
    if len(candidate.content) > MAX_CONTENT_CHARS:
        return "too_long", f"longer than {MAX_CONTENT_CHARS} characters"
    if len(candidate.content.split()) < MIN_WORDS:
        return "too_short", f"fewer than {MIN_WORDS} words, too little to be a useful note"
    if len(candidate.scope) > MAX_SCOPE_CHARS:
        return "bad_scope", f"the scope is longer than {MAX_SCOPE_CHARS} characters"
    if looks_like_task_data(candidate.content):
        return "task_data", "it looks like task data (mostly numbers, amounts, dates or IDs), not reusable knowledge"
    if _INSTRUCTION.search(candidate.content) or _INSTRUCTION.search(candidate.key):
        return "instruction", "it reads like an instruction to the agent, not a note about the environment"
    return None


def _clean(candidate: Candidate) -> Candidate:
    one_line = lambda text: " ".join(clean_text(str(text or "")).split())  # noqa: E731
    return Candidate(key=one_line(candidate.key), content=clean_text(str(candidate.content or "")).strip(),
                     scope=one_line(candidate.scope), source=one_line(candidate.source))


# --------------------------------------------------------------------------- queries


def _any_query(text: str) -> str:
    """``text`` as an OR query for ``websearch_to_tsquery``: every distinct word is optional, so a long task text still
    finds the notes that share some of its words (the ranking puts the best overlap first)."""
    words = list(dict.fromkeys(w for w in _WORD.findall(text.lower()) if w != "or"))[:MAX_QUERY_WORDS]
    return " or ".join(words)


def _item(row: OrgMemory, *, now: datetime, created_by_name: str | None = None) -> dict[str, Any]:
    """A memory as plain JSON-friendly data."""
    iso = lambda value: value.isoformat() if value else None  # noqa: E731
    stamp = row.updated_at or row.created_at
    return {
        "id": str(row.id), "key": row.key, "content": row.content, "scope": row.scope, "source": row.source,
        "source_run_id": row.source_run_id.hex if row.source_run_id else None,
        "created_by": str(row.created_by) if row.created_by else None, "created_by_name": created_by_name,
        "verified": row.verified, "use_count": row.use_count, "last_used_at": iso(row.last_used_at),
        "expires_at": iso(row.expires_at), "expired": bool(row.expires_at and row.expires_at <= now),
        "created_at": iso(row.created_at), "updated_at": iso(stamp),
        "age_days": max(0, (now - stamp).days) if stamp else None,
    }


class OrgMemoryStore:
    """The long-term memory of one organization. Every statement is filtered on ``org_id``: a store can neither read nor
    change another organization's notes, whatever id it is given (unknown and foreign ids look the same)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], org_id: str | uuid.UUID):
        self.sm = sessionmaker
        self.org_id = to_uuid(org_id)

    # ----------------------------------------------------------------- reading

    def _live(self) -> Any:
        return or_(OrgMemory.expires_at.is_(None), OrgMemory.expires_at > func.now())

    async def _ranked(self, session: AsyncSession, query: str, mode: str, scope: str | None, limit: int,
                      live_only: bool) -> list[OrgMemory]:
        text_query = _any_query(query) if mode == "any" else query
        if not text_query.strip():
            return []
        tsquery = func.websearch_to_tsquery("english", text_query)
        rank = func.ts_rank_cd(OrgMemory.search, tsquery)
        stmt = select(OrgMemory).where(OrgMemory.org_id == self.org_id, OrgMemory.search.op("@@")(tsquery))
        if live_only:
            stmt = stmt.where(OrgMemory.verified.is_(True), self._live())
        if scope:
            stmt = stmt.where(func.lower(OrgMemory.scope) == scope.strip().lower())
        stmt = stmt.order_by(rank.desc(), OrgMemory.use_count.desc(), OrgMemory.updated_at.desc()).limit(limit)
        return list((await session.scalars(stmt)).all())

    async def recall(self, query: str, limit: int = DEFAULT_LIMIT, scope: str | None = None, *,
                     match: Match = "auto") -> list[dict]:
        """The best-matching live notes for ``query``, most relevant first; each returned note counts as a use
        (``use_count`` + 1, ``last_used_at`` now), which also keeps it from expiring.

        ``match``: ``all`` every word must occur (``websearch_to_tsquery`` semantics: quotes, ``or``, ``-word``), ``any``
        a note matches on any word (for long texts such as a task), ``auto`` (default) tries ``all`` and falls back to
        ``any`` when nothing matched (or at once for a long text). Expired and unverified notes are never returned."""
        if limit <= 0 or not (query or "").strip():
            return []
        async with self.sm() as session, session.begin():
            rows: list[OrgMemory] = []
            if match == "auto" and len(_WORD.findall(query)) > AUTO_ANY_WORDS:  # a long text (a task): nothing matches all of it
                match = "any"
            for mode in {"all": ("all",), "any": ("any",), "auto": ("all", "any")}[match]:
                rows = await self._ranked(session, query, mode, scope, limit, live_only=True)
                if rows:
                    break
            now = datetime.now(timezone.utc)
            items = [_item(r, now=now) for r in rows]  # before the update below, which expires the loaded rows
            if rows:
                await session.execute(update(OrgMemory).where(OrgMemory.org_id == self.org_id, OrgMemory.id.in_([r.id for r in rows]))
                                      .values(use_count=OrgMemory.use_count + 1, last_used_at=func.now()))
            return items

    async def list(self, query: str | None = None, scope: str | None = None, limit: int = 200) -> list[dict]:
        """Notes for the admin page, expired ones included (flagged ``expired``). With ``query`` the best matches come
        first, else the most recently written. Does not count as a use."""
        now = datetime.now(timezone.utc)
        async with self.sm() as session:
            if (query or "").strip():
                rows = await self._ranked(session, query, "all", scope, limit, live_only=False)
                if not rows:
                    rows = await self._ranked(session, query, "any", scope, limit, live_only=False)
            else:
                stmt = select(OrgMemory).where(OrgMemory.org_id == self.org_id)
                if scope:
                    stmt = stmt.where(func.lower(OrgMemory.scope) == scope.strip().lower())
                rows = list((await session.scalars(stmt.order_by(OrgMemory.updated_at.desc(), OrgMemory.key).limit(limit))).all())
            names = await self._names(session, {r.created_by for r in rows if r.created_by})
        return [_item(r, now=now, created_by_name=names.get(r.created_by)) for r in rows]

    async def get(self, memory_id: str | uuid.UUID) -> dict | None:
        mid = maybe_uuid(memory_id)
        if mid is None:
            return None
        async with self.sm() as session:
            row = await session.scalar(select(OrgMemory).where(OrgMemory.id == mid, OrgMemory.org_id == self.org_id))
            if row is None:
                return None
            names = await self._names(session, {row.created_by} if row.created_by else set())
        return _item(row, now=datetime.now(timezone.utc), created_by_name=names.get(row.created_by))

    @staticmethod
    async def _names(session: AsyncSession, user_ids: set) -> dict:
        if not user_ids:
            return {}
        rows = (await session.execute(select(User.id, User.display_name, User.email).where(User.id.in_(user_ids)))).all()
        return {r[0]: r[1] or r[2] for r in rows}

    async def count(self) -> int:
        async with self.sm() as session:
            return await session.scalar(select(func.count()).select_from(OrgMemory).where(OrgMemory.org_id == self.org_id)) or 0

    # ----------------------------------------------------------------- writing

    async def add_candidates(self, run_id: str | None, candidates: list[Candidate], created_by: str | uuid.UUID | None, *,
                             redactor: Any | None = None) -> SaveResult:
        """Screen ``candidates`` (``screen``) and upsert the accepted ones as verified notes of this org, with the run as
        provenance. A key that exists (case-insensitively) is updated, not duplicated: content, scope, source run and
        creator are replaced, ``updated_at`` bumps and an expiry is cleared. One audit row records the outcome.

        This does **not** decide whether the run deserves to write anything; that is ``commit_run_memory``'s job
        (only runs that passed verification). Call it from nowhere else."""
        result, accepted = SaveResult(), []
        for raw in candidates:
            candidate = _clean(raw)
            problem = ("limit", f"more than {MAX_CANDIDATES_PER_RUN} candidates in one run") if len(accepted) >= MAX_CANDIDATES_PER_RUN \
                else screen(candidate, redactor)
            if problem:
                result.rejected.append({"key": truncate(candidate.key, MAX_KEY_CHARS), "reason": problem[0], "detail": problem[1]})
            else:
                accepted.append(candidate)
        run_uuid = maybe_uuid(run_id) if run_id else None
        user_uuid = maybe_uuid(created_by) if created_by else None
        async with self.sm() as session, session.begin():
            for candidate in accepted:
                insert = pg_insert(OrgMemory).values(
                    org_id=self.org_id, key=candidate.key, content=candidate.content, scope=candidate.scope,
                    source_run_id=run_uuid, source=truncate(candidate.source, SOURCE_LIMIT), created_by=user_uuid, verified=True)
                upsert = insert.on_conflict_do_update(
                    index_elements=[OrgMemory.org_id, func.lower(OrgMemory.key)],
                    set_={"content": insert.excluded.content, "scope": insert.excluded.scope,
                          "source_run_id": insert.excluded.source_run_id, "source": insert.excluded.source,
                          "created_by": insert.excluded.created_by, "verified": True, "expires_at": None,
                          "updated_at": func.now()},
                ).returning(OrgMemory.id, OrgMemory.key, literal_column("(xmax = 0)").label("created"))
                row = (await session.execute(upsert)).one()
                result.saved.append({"key": row.key, "id": str(row.id), "created": bool(row.created)})
            if accepted or result.rejected:
                session.add(AuditLog(org_id=self.org_id, user_id=user_uuid, action="memory.save", target_type="run",
                                     target_id=str(run_id or ""), details={"saved": [s["key"] for s in result.saved],
                                                                     "updated": [s["key"] for s in result.saved if not s["created"]],
                                                                     "rejected": result.rejected}))
        return result

    async def update(self, memory_id: str | uuid.UUID, *, content: str | None = None, scope: str | None = None,
                     expires_at: datetime | None = _UNSET) -> tuple[dict, dict] | None:
        """Edit a note (admin). ``expires_at=None`` clears the expiry; leave it out to keep it. Returns
        ``(before, after)`` as items, or None for an unknown or foreign id. Raises ``ValueError`` for bad input."""
        values: dict[str, Any] = {}
        if content is not None:
            content = content.strip()
            if not content or len(content) > MAX_CONTENT_CHARS:
                raise ValueError(f"The content must be 1 to {MAX_CONTENT_CHARS} characters.")
            values["content"] = clean_text(content)
        if scope is not None:
            scope = " ".join(scope.split())
            if len(scope) > MAX_SCOPE_CHARS:
                raise ValueError(f"The scope can be at most {MAX_SCOPE_CHARS} characters.")
            values["scope"] = clean_text(scope)
        if expires_at is not _UNSET:
            values["expires_at"] = expires_at
        if not values:
            raise ValueError("Send content, scope and/or expires_at.")
        before = await self.get(memory_id)
        if before is None:
            return None
        if "content" in values and values["content"] != before["content"]:
            values["updated_at"] = func.now()
        async with self.sm() as session, session.begin():
            await session.execute(update(OrgMemory).where(OrgMemory.id == uuid.UUID(before["id"]), OrgMemory.org_id == self.org_id)
                                  .values(**values))
        after = await self.get(before["id"])
        return (before, after) if after else None

    async def delete(self, memory_id: str | uuid.UUID) -> dict | None:
        """Delete a note; returns it (for the audit trail), or None for an unknown or foreign id."""
        before = await self.get(memory_id)
        if before is None:
            return None
        async with self.sm() as session, session.begin():
            result = await session.execute(OrgMemory.__table__.delete().where(
                OrgMemory.id == uuid.UUID(before["id"]), OrgMemory.org_id == self.org_id))
        return before if result.rowcount else None

    async def expire_stale(self, ttl_days: int = DEFAULT_TTL_DAYS) -> list[str]:
        """Expire (stop recalling, keep visible) every note without an explicit expiry that was last used, or else last
        written, more than ``ttl_days`` days ago. ``ttl_days`` <= 0 expires nothing. Returns the keys expired."""
        if ttl_days <= 0:
            return []
        stale = and_(OrgMemory.org_id == self.org_id, OrgMemory.expires_at.is_(None),
                     func.coalesce(OrgMemory.last_used_at, OrgMemory.updated_at) < func.now() - timedelta(days=ttl_days))
        async with self.sm() as session, session.begin():
            rows = await session.execute(update(OrgMemory).where(stale).values(expires_at=func.now()).returning(OrgMemory.key))
            return sorted(r[0] for r in rows)


# --------------------------------------------------------------------------- the write gate

EventEmitter = Callable[[str, dict], Awaitable[Any]]


def attach_org_memory(runner: Any, sessionmaker: async_sessionmaker[AsyncSession], org_id: str | uuid.UUID,
                      cfg: dict[str, Any]) -> OrgMemoryStore | None:
    """Give a run its organization's memory (``ctx.extras``: recall at the start and the ``recall`` tool), unless the org
    turned memory off (``memory_recall_limit`` 0). Returns the store that ``commit_run_memory`` writes to, or None."""
    limit = int(cfg.get("memory_recall_limit") or 0)
    if limit <= 0:
        return None
    memory = OrgMemoryStore(sessionmaker, org_id)
    runner.ctx.extras["org_memory"], runner.ctx.extras["org_memory_limit"] = memory, limit
    return memory


async def commit_run_memory(store: OrgMemoryStore, state: RunState, *, created_by: str | uuid.UUID | None,
                            redactor: Any | None = None, emit: EventEmitter | None = None,
                            ttl_days: int = DEFAULT_TTL_DAYS) -> SaveResult | None:
    """**The write gate.** Save the durable facts of a finished run to organization memory, if and only if the run ended
    ``succeeded``. The runner reports ``succeeded`` only after the independent verifier passed, so a failed,
    needs-attention or cancelled run (or one whose audit did not pass) writes nothing, however confident its candidates.

    Returns None when nothing was attempted (not succeeded, or no durable facts). Otherwise screens and saves the
    candidates (``OrgMemoryStore.add_candidates``), emits ``memory_saved`` (keys) and ``memory_rejected`` (keys with
    reasons; never the rejected text) through ``emit``, and expires notes unused for ``ttl_days``. Never raises: memory is
    a bonus and must not turn a verified run into a failed one."""
    if state.status != "succeeded":
        return None
    candidates = [Candidate(key=f.key, content=f.value, scope=f.scope, source=_provenance(f)) for f in state.facts.values() if f.durable]
    if not candidates:
        return None
    try:
        result = await store.add_candidates(state.run_id, candidates, created_by, redactor=redactor)
        if emit is not None:
            if result.saved:
                await emit("memory_saved", _scrub({"run_id": state.run_id, "keys": [s["key"] for s in result.saved],
                                                   "created": [s["key"] for s in result.saved if s["created"]],
                                                   "updated": [s["key"] for s in result.saved if not s["created"]]}, redactor))
            if result.rejected:
                await emit("memory_rejected", _scrub({"run_id": state.run_id, "rejected": result.rejected}, redactor))
        if ttl_days > 0:
            await store.expire_stale(ttl_days)
        return result
    except Exception:  # noqa: BLE001
        log.exception("saving organization memory of run %s failed", state.run_id)
        return None


def _provenance(fact: Any) -> str:
    return f"{fact.source}, step {fact.step}" if fact.source and fact.step else fact.source or (f"step {fact.step}" if fact.step else "")


def _scrub(payload: dict, redactor: Any | None) -> dict:
    return redactor.redact_obj(payload) if redactor else payload
