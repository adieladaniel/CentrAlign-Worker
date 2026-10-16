"""PostgreSQL implementation of ``RunStore`` (see ``worker/store.py`` for the contract).

A ``PgRunStore`` is **scoped to one organisation**: every statement filters on ``runs.org_id``, so a store can neither
read nor write another org's runs, whatever run id it is given (unknown and foreign runs look the same). Get one per
request or per claimed run with ``store.for_org(org_id, user_id)``; ``user_id`` is recorded as the creator of runs made
through it.

Layout: the header is the ``runs`` row; steps, facts and events are rows of their own, appended one at a time (never a
document rewrite). ``append_event`` takes the next ``seq`` from ``runs.event_seq`` under the row lock, so concurrent
writers get gapless, unique numbers, and announces it with ``pg_notify('run_events', '<run_id>:<seq>')`` (delivered when
the transaction commits). Artifacts stay in a local directory (``artifacts_root``, default ``RUNS_DIR``).
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.agent.state import Fact, RunState, Step
from worker.config import get_settings
from worker.db.models import Run, RunEvent, RunFact, RunStep, User
from worker.db.util import clean_text, iso_ts, maybe_uuid, parse_ts, to_uuid
from worker.store import RUN_ID_RE

NOTIFY_CHANNEL = "run_events"


class UnknownRun(LookupError):
    """The run does not exist, or belongs to another organisation (the two are deliberately indistinguishable)."""


def _step_row(run_id: uuid.UUID, step: Step) -> dict[str, Any]:
    return {"run_id": run_id, "idx": step.index, "kind": step.kind, "thought": clean_text(step.thought),
            "thought_is_summary": step.thought_is_summary, "tool": step.tool, "args": step.args, "risk": step.risk,
            "ok": step.ok, "observation": clean_text(step.observation), "error": clean_text(step.error),
            "evidence": step.evidence, "duration_ms": step.duration_ms, "retries": step.retries,
            "ts": parse_ts(step.timestamp), "turn": step.turn, "tool_call_id": step.tool_call_id}


def _fact_row(run_id: uuid.UUID, fact: Fact) -> dict[str, Any]:
    return {"run_id": run_id, "key": clean_text(fact.key), "value": clean_text(fact.value),
            "source": clean_text(fact.source), "step": fact.step, "durable": fact.durable, "scope": clean_text(fact.scope)}


def _header(state: RunState) -> dict[str, Any]:
    """The ``runs`` columns that mirror a ``RunState`` (everything except steps and facts)."""
    return {"task": clean_text(state.task), "status": state.status, "goal_summary": clean_text(state.goal_summary),
            "success_criteria": state.success_criteria, "plan": [asdict(item) for item in state.plan],
            "usage": state.usage, "final_report": state.final_report, "started_at": parse_ts(state.started_at),
            "finished_at": parse_ts(state.finished_at)}


class PgRunStore:
    """``RunStore`` over PostgreSQL for one organisation. See the module docstring."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        org_id: str | uuid.UUID,
        user_id: str | uuid.UUID | None = None,
        *,
        artifacts_root: str | Path | None = None,
    ):
        self.sessionmaker = sessionmaker
        self.org_id = to_uuid(org_id)
        self.user_id = to_uuid(user_id) if user_id is not None else None
        self.artifacts_root = Path(artifacts_root) if artifacts_root is not None else Path(get_settings().RUNS_DIR)
        self._owned: set[uuid.UUID] = set()  # runs already confirmed to belong to this org (an org never changes)

    def for_org(self, org_id: str | uuid.UUID, user_id: str | uuid.UUID | None = None) -> PgRunStore:
        """A store for another org/user over the same connection pool (e.g. one per request, or per claimed run)."""
        return PgRunStore(self.sessionmaker, org_id, user_id, artifacts_root=self.artifacts_root)

    # ----------------------------------------------------------------- plumbing

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session, session.begin():
            yield session

    async def _require(self, session: AsyncSession, run_id: str) -> uuid.UUID:
        """The run's UUID, after checking that it exists in this store's org. Raises ``UnknownRun`` otherwise."""
        rid = maybe_uuid(run_id)
        if rid is None:
            raise UnknownRun(f"invalid run id: {run_id!r}")
        if rid not in self._owned:
            found = await session.scalar(select(Run.id).where(Run.id == rid, Run.org_id == self.org_id))
            if found is None:
                raise UnknownRun(f"unknown run: {run_id}")
            self._owned.add(rid)
        return rid

    async def _update_run(self, run_id: str, **values: Any) -> None:
        rid = maybe_uuid(run_id)
        async with self._tx() as session:
            result = await session.execute(update(Run).where(Run.id == rid, Run.org_id == self.org_id).values(**values))
            if rid is None or result.rowcount == 0:
                raise UnknownRun(f"unknown run: {run_id}")

    # ----------------------------------------------------------------- artifacts

    def artifact_dir(self, run_id: str) -> Path:
        if not RUN_ID_RE.match(run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        path = self.artifacts_root / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def artifact_file(self, run_id: str, rel_path: str) -> Path | None:
        """Only checks the path (the caller must have loaded the run through this store first: that is the org check)."""
        if not RUN_ID_RE.match(run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        root = (self.artifacts_root / run_id).resolve()
        try:
            target = (root / rel_path).resolve()
        except (OSError, ValueError):
            return None
        return target if target.is_relative_to(root) and target.is_file() else None

    # ----------------------------------------------------------------- header, steps, facts

    async def create_run(self, state: RunState) -> None:
        rid = to_uuid(state.run_id)
        header = _header(state)
        async with self._tx() as session:
            await session.execute(insert(Run).values(
                id=rid, org_id=self.org_id, created_by=self.user_id, **header,
                **({"queued_at": header["started_at"]} if header["started_at"] else {})))
            if state.steps:
                await session.execute(insert(RunStep), [_step_row(rid, step) for step in state.steps])
            if state.facts:
                await session.execute(insert(RunFact), [_fact_row(rid, fact) for fact in state.facts.values()])
        self._owned.add(rid)

    async def save_state(self, state: RunState) -> None:
        await self._update_run(state.run_id, **_header(state))

    async def append_step(self, run_id: str, step: Step) -> None:
        async with self._tx() as session:
            rid = await self._require(session, run_id)
            await session.execute(insert(RunStep).values(**_step_row(rid, step)))

    async def upsert_fact(self, run_id: str, fact: Fact) -> None:
        async with self._tx() as session:
            rid = await self._require(session, run_id)
            row = _fact_row(rid, fact)
            await session.execute(pg_insert(RunFact).values(**row).on_conflict_do_update(
                index_elements=[RunFact.run_id, RunFact.key],
                set_={"value": row["value"], "source": row["source"], "step": row["step"], "durable": row["durable"],
                      "scope": row["scope"]}))  # ``ord`` keeps its place

    async def load_state(self, run_id: str) -> RunState | None:
        rid = maybe_uuid(run_id)
        if rid is None:
            return None
        async with self._tx() as session:
            run = await session.scalar(select(Run).where(Run.id == rid, Run.org_id == self.org_id))
            if run is None:
                return None
            steps = (await session.scalars(select(RunStep).where(RunStep.run_id == rid).order_by(RunStep.idx))).all()
            facts = (await session.scalars(select(RunFact).where(RunFact.run_id == rid).order_by(RunFact.ord))).all()
        self._owned.add(rid)
        data: dict[str, Any] = {
            "run_id": rid.hex, "task": run.task, "status": run.status, "goal_summary": run.goal_summary,
            "success_criteria": run.success_criteria, "plan": run.plan, "usage": run.usage,
            "final_report": run.final_report, "started_at": iso_ts(run.started_at or run.queued_at),
            "finished_at": iso_ts(run.finished_at),
            "steps": [{"index": s.idx, "kind": s.kind, "thought": s.thought, "thought_is_summary": s.thought_is_summary,
                       "tool": s.tool, "args": s.args, "risk": s.risk, "ok": s.ok, "observation": s.observation,
                       "error": s.error, "evidence": s.evidence, "duration_ms": s.duration_ms, "retries": s.retries,
                       "timestamp": iso_ts(s.ts), "turn": s.turn, "tool_call_id": s.tool_call_id} for s in steps],
            "facts": {f.key: {"key": f.key, "value": f.value, "source": f.source, "step": f.step, "durable": f.durable,
                              "scope": f.scope} for f in facts},
        }
        return RunState.from_dict(data)

    async def list_runs(self, limit: int | None = None, org_id: str | None = None) -> list[dict]:
        if org_id is not None and maybe_uuid(org_id) != self.org_id:
            return []  # a scoped store never lists another org's runs
        query = (select(Run.id, Run.task, Run.status, Run.started_at, Run.queued_at, Run.finished_at)
                 .where(Run.org_id == self.org_id).order_by(Run.queued_at.desc(), Run.id))
        if limit is not None:
            query = query.limit(max(limit, 0))
        async with self._tx() as session:
            rows = (await session.execute(query)).all()
        return [{"id": r.id.hex, "task": r.task, "status": r.status, "started_at": iso_ts(r.started_at or r.queued_at),
                 "finished_at": iso_ts(r.finished_at)} for r in rows]

    # ----------------------------------------------------------------- org-level queries (not part of ``RunStore``)

    async def exists(self, run_id: str) -> bool:
        """Does the run exist in this store's org? (The check before serving anything that belongs to a run.)"""
        rid = maybe_uuid(run_id)
        if rid is None:
            return False
        async with self._tx() as session:
            return await session.scalar(select(Run.id).where(Run.id == rid, Run.org_id == self.org_id)) is not None

    async def count_runs_since(self, since: datetime) -> int:
        """How many runs this org queued at or after ``since`` (the per-org daily quota)."""
        async with self._tx() as session:
            return await session.scalar(select(func.count()).select_from(Run).where(
                Run.org_id == self.org_id, Run.queued_at >= since)) or 0

    async def creators(self, run_ids: list[str]) -> dict[str, dict]:
        """``{run_id_hex: {id, name, email}}`` of the users who started these runs (runs without a creator are absent)."""
        ids = [rid for rid in (maybe_uuid(r) for r in run_ids) if rid is not None]
        if not ids:
            return {}
        query = (select(Run.id, User.id, User.display_name, User.email).join(User, User.id == Run.created_by)
                 .where(Run.org_id == self.org_id, Run.id.in_(ids)))
        async with self._tx() as session:
            rows = (await session.execute(query)).all()
        return {r[0].hex: {"id": str(r[1]), "name": r[2] or r[3], "email": r[3]} for r in rows}

    async def creator_id(self, run_id: str) -> str | None:
        """The id of the user who started the run, or None (unknown run, or the user was deleted)."""
        rid = maybe_uuid(run_id)
        if rid is None:
            return None
        async with self._tx() as session:
            value = await session.scalar(select(Run.created_by).where(Run.id == rid, Run.org_id == self.org_id))
        return str(value) if value else None

    # ----------------------------------------------------------------- events

    async def append_event(self, run_id: str, event_type: str, payload: dict) -> int:
        rid = maybe_uuid(run_id)
        async with self._tx() as session:
            seq = None if rid is None else await session.scalar(
                update(Run).where(Run.id == rid, Run.org_id == self.org_id)
                .values(event_seq=Run.event_seq + 1).returning(Run.event_seq))  # row lock: one writer at a time
            if seq is None:
                raise UnknownRun(f"unknown run: {run_id}")
            await session.execute(insert(RunEvent).values(run_id=rid, seq=seq, type=event_type, payload=payload))
            await session.execute(select(func.pg_notify(NOTIFY_CHANNEL, f"{rid.hex}:{seq}")))
        return seq

    async def last_event_seq(self, run_id: str) -> int:
        """The newest event number of the run (0 for none, unknown or foreign runs)."""
        rid = maybe_uuid(run_id)
        if rid is None:
            return 0
        async with self._tx() as session:
            return await session.scalar(select(Run.event_seq).where(Run.id == rid, Run.org_id == self.org_id)) or 0

    async def read_events(self, run_id: str, after_seq: int = 0) -> list[dict]:
        rid = maybe_uuid(run_id)
        if rid is None:
            return []
        query = (select(RunEvent).join(Run, Run.id == RunEvent.run_id)
                 .where(RunEvent.run_id == rid, Run.org_id == self.org_id).order_by(RunEvent.seq))
        if after_seq > 0:
            query = query.where(RunEvent.seq > after_seq)
        async with self._tx() as session:
            events = (await session.scalars(query)).all()
        return [{"seq": e.seq, "type": e.type, "ts": iso_ts(e.ts), "payload": e.payload} for e in events]

    # ----------------------------------------------------------------- runtime and report

    async def save_runtime(self, run_id: str, runtime: dict) -> None:
        values: dict[str, Any] = {"runtime": runtime}
        if isinstance(runtime.get("resume_count"), int):
            values["resume_count"] = runtime["resume_count"]
        await self._update_run(run_id, **values)

    async def load_runtime(self, run_id: str) -> dict | None:
        rid = maybe_uuid(run_id)
        if rid is None:
            return None
        async with self._tx() as session:
            return await session.scalar(select(Run.runtime).where(Run.id == rid, Run.org_id == self.org_id))

    async def save_report(self, run_id: str, report: dict, markdown: str) -> None:
        await self._update_run(run_id, final_report=report, report_markdown=clean_text(markdown))

    async def load_report_markdown(self, run_id: str) -> str | None:
        rid = maybe_uuid(run_id)
        if rid is None:
            return None
        async with self._tx() as session:
            return await session.scalar(select(Run.report_markdown).where(Run.id == rid, Run.org_id == self.org_id))
