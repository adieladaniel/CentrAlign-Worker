"""The job queue of database mode: SQL for enqueueing, claiming, leasing and reaping runs.

State machine of ``runs.status``::

    (API)  enqueue --------------------> queued
    (worker) claim ---------------------> running <--> waiting_approval | waiting_user | verifying
                                              |  the runner moves between these three itself
                                              +--> succeeded | failed | needs_attention | cancelled   (final)
    worker lost (lease expired) ---------> queued again, ``resume_requested`` when the run had started
                                           (attempts exhausted: needs_attention, "worker lost too many times")
    worker shutting down ----------------> queued again with ``resume_requested`` (the claim is not counted)
    user cancels a queued run -----------> cancelled;  a running run: ``cancel_requested`` + NOTIFY ``run_control``

Rules enforced here, in SQL, so any number of API and worker processes agree:

* **Quota** per org and UTC day is counted when a run is enqueued, under a per-org advisory lock, in the transaction that
  inserts the run: concurrent requests cannot both squeeze under the limit.
* **Concurrency** per org (``max_concurrent_runs``) is checked when a run is claimed, under the same kind of lock: a run
  stays ``queued`` until one of its org's slots frees up. Runs of different orgs never wait for each other's limits.
* **Claiming** uses ``FOR UPDATE SKIP LOCKED``, so two workers never take the same run; the oldest, highest-priority
  claimable run goes first.
* **Leases**: a claimed run belongs to its worker until ``lease_expires_at`` (database clock), which the worker extends
  while it lives. A run whose lease expired is taken back by ``reap`` (any worker may do it).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker import metrics
from worker.agent.report import build_report, render_markdown
from worker.agent.state import TERMINAL_STATUSES, utcnow
from worker.approvals_pg import ApprovalStore
from worker.db.models import Run, RunEvent
from worker.db.util import clean_text, maybe_uuid
from worker.pg_listen import CHANNEL_RUN_CONTROL, CHANNEL_RUN_EVENTS, CHANNEL_RUNS_QUEUED
from worker.store_pg import PgRunStore

log = logging.getLogger("worker.jobqueue")

ACTIVE_STATUSES = ("running", "waiting_approval", "waiting_user", "verifying")  # a worker holds the run
LIVE_STATUSES = ("queued", *ACTIVE_STATUSES)  # not finished
DEFAULT_MAX_CONCURRENT = 2
LOST_TOO_OFTEN = "The worker running this run was lost too many times, so it was not started again."


class QuotaExceeded(Exception):
    """The org already queued its daily allowance of runs."""

    def __init__(self, limit: int):
        super().__init__(f"daily run limit of {limit} reached")
        self.limit = limit


@dataclass
class ClaimedRun:
    """A run a worker has just taken."""

    id: str  # hex
    org_id: uuid.UUID
    created_by: uuid.UUID | None
    task: str
    approval_mode: str | None
    options: dict
    resume: bool  # continue from the checkpoint instead of starting
    attempts: int
    cancel_requested: bool
    waited_s: float | None = None  # time in the queue before this claim (database clock)


def _notify(channel: str, payload: str):
    return select(func.pg_notify(channel, payload))


def _lock_key(kind: str, org_id: uuid.UUID) -> dict[str, str]:
    return {"k": f"{kind}:{org_id}"}



# --------------------------------------------------------------------------- enqueue


async def enqueue_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    *,
    task: str,
    approval_mode: str | None,
    options: dict[str, Any] | None = None,
    priority: int = 0,
    daily_limit: int = 0,
) -> str:
    """Insert a ``queued`` run (and its first event) and wake the workers. Raises ``QuotaExceeded`` when the org has
    already queued ``daily_limit`` runs today (UTC; 0 = unlimited); the check and the insert are one transaction."""
    run_id = uuid.uuid4()
    async with sessionmaker() as session, session.begin():
        if daily_limit:
            await session.execute(text("select pg_advisory_xact_lock(hashtextextended(:k, 0))"), _lock_key("enqueue", org_id))
            today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            used = await session.scalar(select(func.count()).select_from(Run).where(Run.org_id == org_id, Run.queued_at >= today))
            if (used or 0) >= daily_limit:
                raise QuotaExceeded(daily_limit)
        await session.execute(insert(Run).values(
            id=run_id, org_id=org_id, created_by=user_id, task=clean_text(task), status="queued",
            approval_mode=approval_mode, options=options or {}, priority=priority, event_seq=1))
        await session.execute(insert(RunEvent).values(run_id=run_id, seq=1, type="run_queued", payload={
            "run_id": run_id.hex, "task": clean_text(task), "approval_mode": approval_mode, "priority": priority}))
        await session.execute(_notify(CHANNEL_RUN_EVENTS, f"{run_id.hex}:1"))
        await session.execute(_notify(CHANNEL_RUNS_QUEUED, run_id.hex))
    return run_id.hex


# --------------------------------------------------------------------------- claim and lease

_ACTIVE = {"active": list(ACTIVE_STATUSES)}

_CANDIDATES = text("""
select id, org_id from (
    select distinct on (r.org_id) r.id, r.org_id, r.priority, r.queued_at
    from runs r join orgs o on o.id = r.org_id
    where r.status = 'queued'
      and (select count(*) from runs a where a.org_id = r.org_id and a.status = any(:active))
          < coalesce((o.settings->>'max_concurrent_runs')::int, :default_limit)
    order by r.org_id, r.priority desc, r.queued_at, r.id
) heads
order by priority desc, queued_at
limit 20
""")

_ROOM = text("""
select (select count(*) from runs a where a.org_id = :org and a.status = any(:active))
       < coalesce((select (o.settings->>'max_concurrent_runs')::int from orgs o where o.id = :org), :default_limit)
""")

_CLAIM = text("""
update runs set status = 'running', lease_owner = :worker, heartbeat_at = now(),
                lease_expires_at = now() + make_interval(secs => :lease),
                attempts = attempts + 1, started_at = coalesce(started_at, now()), resume_requested = false
where id = (select id from runs where org_id = :org and status = 'queued'
            order by priority desc, queued_at, id for update skip locked limit 1)
returning id, org_id, created_by, task, approval_mode, options, attempts, cancel_requested,
          (select resume_requested from runs old where old.id = runs.id) as was_resume,
          extract(epoch from now() - queued_at) as waited_s
""")


async def claim_next(sessionmaker: async_sessionmaker[AsyncSession], worker_id: str, *, lease_s: float,
                     default_limit: int = DEFAULT_MAX_CONCURRENT) -> tuple[ClaimedRun | None, bool]:
    """Take the best claimable queued run for ``worker_id``: ``(run, contended)``. ``contended`` is True when no run
    was taken but some org was skipped because another claimer held its lock right then (worth retrying at once)."""
    contended = False
    async with sessionmaker() as session, session.begin():
        heads = (await session.execute(_CANDIDATES, {**_ACTIVE, "default_limit": default_limit})).all()
        for _run_id, org_id in heads:
            if not await session.scalar(text("select pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                                        _lock_key("claim", org_id)):
                contended = True
                continue
            if not await session.scalar(_ROOM, {**_ACTIVE, "org": org_id, "default_limit": default_limit}):
                continue  # another claimer filled the org's last slot a moment ago
            row = (await session.execute(_CLAIM, {"worker": worker_id, "lease": lease_s, "org": org_id})).first()
            if row is None:
                continue
            return ClaimedRun(id=row.id.hex, org_id=row.org_id, created_by=row.created_by, task=row.task,
                              approval_mode=row.approval_mode, options=row.options or {}, attempts=row.attempts,
                              cancel_requested=row.cancel_requested, resume=bool(row.was_resume),
                              waited_s=float(row.waited_s)), False
    return None, contended


async def heartbeat(sessionmaker: async_sessionmaker[AsyncSession], run_id: str, worker_id: str, *,
                    lease_s: float) -> bool | None:
    """Extend the lease. Returns whether cancellation was requested, or None when the lease is no longer ours (the run
    was taken away, finished by someone else, or deleted).

    A run that simply *ended* is not a lost lease: the runner saves the final status a moment before the worker's task
    stops its heartbeat (report, events and organization memory are still being written), and a beat that lands in that
    window finds the run no longer active. That answers ``False`` (nothing to extend, nothing to cancel); calling it
    "lost" would fence the worker's last writes, skip the memory commit and leave the lease set on a finished run. Only a
    final status with the lease still in this worker's name counts: a run the reaper took back (re-queued, or given up
    on, both clear the owner) is lost."""
    async with sessionmaker() as session, session.begin():
        row = (await session.execute(text("""
            update runs set lease_expires_at = now() + make_interval(secs => :lease), heartbeat_at = now()
            where id = :id and lease_owner = :worker and status = any(:active)
            returning cancel_requested"""), {"id": uuid.UUID(run_id), "worker": worker_id, "lease": lease_s, **_ACTIVE})).first()
        if row is None:
            over = (await session.execute(text("""
                select 1 from runs where id = :id and status = any(:final) and lease_owner = :worker"""),
                {"id": uuid.UUID(run_id), "worker": worker_id, "final": list(TERMINAL_STATUSES)})).first()
            return False if over is not None else None
    return bool(row.cancel_requested)


async def release_lease(sessionmaker: async_sessionmaker[AsyncSession], run_id: str, worker_id: str) -> None:
    """The worker is done with the run (it reached a final status): drop the lease and tell the workers a slot is free."""
    async with sessionmaker() as session, session.begin():
        await session.execute(text("update runs set lease_owner = null, lease_expires_at = null "
                                   "where id = :id and lease_owner = :worker"), {"id": uuid.UUID(run_id), "worker": worker_id})
        await session.execute(_notify(CHANNEL_RUNS_QUEUED, "slot"))


async def requeue_for_shutdown(sessionmaker: async_sessionmaker[AsyncSession], store: PgRunStore, run_id: str,
                               worker_id: str, reason: str) -> bool:
    """Hand a run back to the queue because its worker is stopping (the claim does not count as an attempt)."""
    async with sessionmaker() as session, session.begin():
        row = (await session.execute(text("""
            update runs set status = 'queued', resume_requested = (runtime is not null), lease_owner = null,
                            lease_expires_at = null, attempts = greatest(attempts - 1, 0)
            where id = :id and lease_owner = :worker and status = any(:active) returning id"""),
            {"id": uuid.UUID(run_id), "worker": worker_id, **_ACTIVE})).first()
        if row is not None:
            await session.execute(_notify(CHANNEL_RUNS_QUEUED, run_id))
    if row is None:
        return False
    metrics.REQUEUED.labels("shutdown").inc()
    await store.append_event(run_id, "run_requeued", {"run_id": run_id, "reason": reason, "worker": worker_id})
    return True


# --------------------------------------------------------------------------- reaper

_REAP = text("""
with expired as (
    select id, lease_owner as old_owner from runs
    where status = any(:active) and lease_expires_at < now()
    order by lease_expires_at limit 50 for update skip locked
)
update runs r set
    status = case when r.attempts < :max_attempts then 'queued' else 'needs_attention' end,
    resume_requested = (r.runtime is not null),
    finished_at = case when r.attempts < :max_attempts then null else now() end,
    lease_owner = null, lease_expires_at = null
from expired
where r.id = expired.id
returning r.id, r.org_id, r.attempts, r.status, expired.old_owner, r.resume_requested
""")


async def reap(sessionmaker: async_sessionmaker[AsyncSession], base_store: PgRunStore, *, max_attempts: int) -> list[dict]:
    """Take back runs whose worker stopped renewing the lease: re-queue them (to be resumed), or give up on those that
    already used all their attempts. Safe to call from every worker at once. Returns what it did."""
    async with sessionmaker() as session, session.begin():
        rows = (await session.execute(_REAP, {**_ACTIVE, "max_attempts": max_attempts})).all()
        if any(r.status == "queued" for r in rows):
            await session.execute(_notify(CHANNEL_RUNS_QUEUED, "reaped"))
    done = []
    for row in rows:
        run_id, store = row.id.hex, base_store.for_org(row.org_id)
        info = {"run_id": run_id, "attempts": row.attempts, "worker": row.old_owner, "resume": bool(row.resume_requested)}
        try:
            if row.status == "queued":
                metrics.REQUEUED.labels("worker_lost").inc()
                await store.append_event(run_id, "run_requeued", {**info, "reason": "worker_lost"})
                done.append({**info, "outcome": "requeued"})
            else:
                await finish_unrun(store, run_id, "needs_attention", LOST_TOO_OFTEN)
                done.append({**info, "outcome": "given_up"})
        except Exception:  # noqa: BLE001 - one broken run must not stop the sweep
            log.exception("reaping run %s failed", run_id)
    return done


# --------------------------------------------------------------------------- finishing runs nobody is executing


async def finish_unrun(store: PgRunStore, run_id: str, status: str, reason: str) -> bool:
    """Give a run that no runner is executing its final report and ``run_finished`` event. The run's header may already
    say ``status`` (the SQL that decided it did so); pending approvals are cancelled. False if it had a report already."""
    state = await store.load_state(run_id)
    if state is None or state.final_report:
        return False
    runtime = await store.load_runtime(run_id) or {}
    approvals = {"requested": 0, "approved": 0, "denied": 0, **(runtime.get("approvals") or {})}
    report = build_report(state, status=status, claim=runtime.get("claim"), verdict=None, reason=reason,
                          approvals=approvals, run_dir=store.artifact_dir(run_id))
    state.status, state.final_report, state.finished_at = status, report, utcnow()
    await store.save_state(state)
    await store.save_report(run_id, report, render_markdown(state, report))
    await ApprovalStore(store.sessionmaker, store.org_id).cancel_for_run(run_id, reason)
    await store.append_event(run_id, "run_finished", {"run_id": run_id, "status": status, "report": report})
    metrics.run_finished_unrun(status)
    return True


# --------------------------------------------------------------------------- cancel (API side)


async def request_cancel(sessionmaker: async_sessionmaker[AsyncSession], store: PgRunStore, run_id: str) -> str | None:
    """Stop a run of ``store``'s org. A queued run is cancelled at once (``"cancelled"``); a claimed one is flagged and
    its worker told (``"requested"``). None when the run is unknown, foreign or already finished."""
    rid = maybe_uuid(run_id)
    if rid is None:
        return None
    outcome = None
    async with sessionmaker() as session, session.begin():
        if (await session.execute(text("update runs set status = 'cancelled', finished_at = now(), resume_requested = false "
                                       "where id = :id and org_id = :org and status = 'queued' returning id"),
                                  {"id": rid, "org": store.org_id})).first():
            outcome = "cancelled"
        elif (await session.execute(text("update runs set cancel_requested = true where id = :id and org_id = :org "
                                         "and status = any(:active) returning id"),
                                    {"id": rid, "org": store.org_id, **_ACTIVE})).first():
            outcome = "requested"
            await session.execute(_notify(CHANNEL_RUN_CONTROL, f"{rid.hex}:cancel"))
    if outcome == "cancelled":
        await finish_unrun(store, rid.hex, "cancelled", "Cancelled by the user while the run was waiting in the queue.")
    return outcome


# --------------------------------------------------------------------------- reads for the API


async def run_rows(sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID, *, limit: int = 200,
                   with_worker: bool = False) -> list[dict]:
    """The org's runs, newest first, with queue position (queued runs: 1 = next in the org's own queue) and, for
    admins (``with_worker``), the worker that holds each active run."""
    async with sessionmaker() as session:
        rows = (await session.execute(text("""
            select id, task, status, created_by, queued_at, started_at, finished_at, lease_owner, attempts, resume_count,
                   resume_requested, priority
            from runs where org_id = :org order by queued_at desc, id limit :limit"""), {"org": org_id, "limit": limit})).all()
        queued = (await session.execute(text("""
            select id from runs where org_id = :org and status = 'queued' order by priority desc, queued_at, id"""),
            {"org": org_id})).scalars().all()
    position = {rid: n for n, rid in enumerate(queued, start=1)}
    out = []
    for r in rows:
        item = {"id": r.id.hex, "task": r.task, "status": r.status,
                "started_at": (r.started_at or r.queued_at).astimezone(timezone.utc).isoformat(),
                "queued_at": r.queued_at.astimezone(timezone.utc).isoformat(),
                "finished_at": r.finished_at.astimezone(timezone.utc).isoformat() if r.finished_at else None,
                "active": r.status in LIVE_STATUSES, "queue_position": position.get(r.id),
                "attempts": r.attempts, "resumed": bool(r.resume_count or r.resume_requested)}
        if with_worker:
            item["worker"] = r.lease_owner if r.status in ACTIVE_STATUSES else None
        out.append(item)
    return out


async def run_queue_info(sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID, run_id: str,
                         with_worker: bool = False) -> dict:
    """Queue position, attempts and (admins) the worker of one run."""
    rid = maybe_uuid(run_id)
    async with sessionmaker() as session:
        row = (await session.execute(text("select status, lease_owner, attempts, priority, queued_at from runs "
                                          "where id = :id and org_id = :org"), {"id": rid, "org": org_id})).first()
        position = None
        if row is not None and row.status == "queued":
            position = await session.scalar(text("""
                select count(*) + 1 from runs where org_id = :org and status = 'queued' and id <> :id
                  and (priority > :p or (priority = :p and (queued_at < :q or (queued_at = :q and id < :id))))"""),
                {"org": org_id, "id": rid, "p": row.priority, "q": row.queued_at})
    if row is None:
        return {}
    info = {"queue_position": position, "attempts": row.attempts, "active": row.status in LIVE_STATUSES}
    if with_worker:
        info["worker"] = row.lease_owner if row.status in ACTIVE_STATUSES else None
    return info


async def org_has_live_runs(sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID) -> bool:
    async with sessionmaker() as session:
        return bool(await session.scalar(text("select exists(select 1 from runs where org_id = :org and status = any(:live))"),
                                         {"org": org_id, "live": list(LIVE_STATUSES)}))


async def newest_live_run(sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID) -> str | None:
    async with sessionmaker() as session:
        found = await session.scalar(text("select id from runs where org_id = :org and status = any(:live) "
                                          "order by queued_at desc limit 1"), {"org": org_id, "live": list(LIVE_STATUSES)})
    return found.hex if found else None


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
