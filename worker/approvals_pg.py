"""Durable approvals: every state change of an approval row, scoped to one organisation.

An approval (or a question to the user) is a row of ``approvals``. Its lifecycle::

    pending --respond--> approved | denied | answered     (first writer wins; a second answer is refused)
    pending --timeout--> expired                          (the waiting worker, or any reaper, marks it)
    pending --cancel---> cancelled                        (the run was cancelled or ended)

Each change commits together with a ``pg_notify`` so every process hears about it: ``approvals`` when a request is
raised, ``approval_responses`` for the other transitions (payload ``<org_hex>:<approval_id>`` for both).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.db.models import Approval, Run, User
from worker.db.util import iso_ts, maybe_uuid, to_uuid
from worker.pg_listen import CHANNEL_APPROVAL_RESPONSES, CHANNEL_APPROVALS

ANSWERED = ("approved", "denied", "answered")
RESOLVED = (*ANSWERED, "expired", "cancelled")


def fingerprint(kind: str, payload: dict) -> str:
    """Identifies *what* is being asked, so a resumed run that asks the same thing finds its still-open request."""
    basis = {"kind": kind, "tool": payload.get("tool"), "args": payload.get("args"), "question": payload.get("question")}
    return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()


def minutes_text(seconds: float) -> str:
    """'45 s', '1 min', '60 min': how long an approval was open, for messages."""
    seconds = max(0, round(seconds))
    return f"{seconds} s" if seconds < 60 else f"{max(1, round(seconds / 60))} min"


def as_dict(row: Approval) -> dict[str, Any]:
    return {"id": str(row.id), "run_id": row.run_id.hex, "org_id": str(row.org_id), "kind": row.kind,
            "payload": row.payload, "status": row.status, "requested_at": iso_ts(row.requested_at),
            "expires_at": iso_ts(row.expires_at), "responded_at": iso_ts(row.responded_at),
            "response": row.response, "consumed_at": iso_ts(row.consumed_at),
            "responded_by": str(row.responded_by) if row.responded_by else None}


def _notify(channel: str, org_id: uuid.UUID, approval_id: uuid.UUID):
    return select(func.pg_notify(channel, f"{org_id.hex}:{approval_id}"))


class ApprovalStore:
    """Approvals of one organisation. Every statement filters on ``approvals.org_id``."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], org_id: str | uuid.UUID):
        self.sm = sessionmaker
        self.org_id = to_uuid(org_id)

    # ----------------------------------------------------------------- reads

    async def get(self, approval_id: str | uuid.UUID, run_id: str | None = None) -> dict | None:
        aid = maybe_uuid(approval_id)
        if aid is None:
            return None
        query = select(Approval).where(Approval.id == aid, Approval.org_id == self.org_id)
        if run_id is not None:
            rid = maybe_uuid(run_id)
            if rid is None:
                return None
            query = query.where(Approval.run_id == rid)
        async with self.sm() as session:
            row = await session.scalar(query)
        return as_dict(row) if row else None

    async def state(self, approval_id: uuid.UUID) -> dict | None:
        """What a waiting broker needs: status, response, and whether the deadline has passed (by the database clock)."""
        async with self.sm() as session:
            row = (await session.execute(
                select(Approval.status, Approval.response, Approval.requested_at, Approval.expires_at,
                       (Approval.expires_at <= func.now()).label("past_deadline"),
                       func.extract("epoch", Approval.expires_at - func.now()).label("seconds_left"))
                .where(Approval.id == approval_id, Approval.org_id == self.org_id))).first()
        if row is None:
            return None
        return {"status": row.status, "response": row.response, "past_deadline": bool(row.past_deadline),
                "seconds_left": float(row.seconds_left) if row.seconds_left is not None else None,
                "open_s": ((row.expires_at - row.requested_at).total_seconds() if row.expires_at else None)}

    async def pending_for_run(self, run_id: str) -> list[dict]:
        rid = maybe_uuid(run_id)
        if rid is None:
            return []
        async with self.sm() as session:
            rows = (await session.scalars(select(Approval).where(
                Approval.run_id == rid, Approval.org_id == self.org_id, Approval.status == "pending")
                .order_by(Approval.requested_at))).all()
        return [as_dict(r) for r in rows]

    async def list(self, status: str | None = "pending", limit: int = 100) -> list[dict]:
        """The org's approvals (newest first for resolved ones, oldest first for pending), with the run's task and starter."""
        query = (select(Approval, Run.task, Run.created_by, User.display_name, User.email)
                 .join(Run, Run.id == Approval.run_id).outerjoin(User, User.id == Run.created_by)
                 .where(Approval.org_id == self.org_id).limit(max(1, min(limit, 500))))
        if status and status != "all":
            query = query.where(Approval.status == status)
        query = query.order_by(Approval.requested_at if status == "pending" else Approval.requested_at.desc())
        async with self.sm() as session:
            rows = (await session.execute(query)).all()
        out = []
        for row, task, creator_id, name, email in rows:
            item = as_dict(row)
            item["run_task"] = task
            item["run_started_by"] = ({"id": str(creator_id), "name": name or email, "email": email}
                                      if creator_id else None)
            out.append(item)
        return out

    # ----------------------------------------------------------------- writes

    async def create(self, run_id: str, kind: str, payload: dict, timeout_s: float | None) -> dict:
        rid = to_uuid(run_id)
        expires = None if not timeout_s else func.now() + func.make_interval(0, 0, 0, 0, 0, 0, float(timeout_s))
        async with self.sm() as session, session.begin():
            row = Approval(run_id=rid, org_id=self.org_id, kind=kind, payload=payload, status="pending",
                           expires_at=expires)
            session.add(row)
            await session.flush()
            await session.refresh(row)
            await session.execute(_notify(CHANNEL_APPROVALS, self.org_id, row.id))
        return as_dict(row)

    async def respond(self, approval_id: uuid.UUID, status: str, response: dict, user_id: uuid.UUID | None) -> dict | None:
        """Record the answer, only if the approval is still pending and not past its deadline (first writer wins).
        Returns the updated row, or None when it was already resolved or expired."""
        async with self.sm() as session, session.begin():
            row = await session.scalar(
                update(Approval).where(Approval.id == approval_id, Approval.org_id == self.org_id,
                                       Approval.status == "pending",
                                       (Approval.expires_at.is_(None)) | (Approval.expires_at > func.now()))
                .values(status=status, response=response, responded_by=user_id, responded_at=func.now())
                .returning(Approval))
            if row is None:
                return None
            await session.execute(_notify(CHANNEL_APPROVAL_RESPONSES, self.org_id, approval_id))
        return as_dict(row)

    async def _close(self, session: AsyncSession, where: list, status: str, response: dict | None) -> list[uuid.UUID]:
        ids = (await session.scalars(
            update(Approval).where(Approval.org_id == self.org_id, Approval.status == "pending", *where)
            .values(status=status, response=response, responded_at=func.now()).returning(Approval.id))).all()
        for approval_id in ids:
            await session.execute(_notify(CHANNEL_APPROVAL_RESPONSES, self.org_id, approval_id))
        return list(ids)

    async def expire(self, approval_id: uuid.UUID) -> bool:
        """Mark a pending approval expired (idempotent). True if this call did it."""
        async with self.sm() as session, session.begin():
            return bool(await self._close(session, [Approval.id == approval_id], "expired", None))

    async def cancel(self, approval_id: uuid.UUID, reason: str) -> bool:
        async with self.sm() as session, session.begin():
            return bool(await self._close(session, [Approval.id == approval_id], "cancelled", {"reason": reason}))

    async def cancel_for_run(self, run_id: str, reason: str, *, keep: uuid.UUID | None = None) -> int:
        """Cancel the run's pending approvals (all of them, or all but ``keep``)."""
        rid = maybe_uuid(run_id)
        if rid is None:
            return 0
        where: list = [Approval.run_id == rid]
        if keep is not None:
            where.append(Approval.id != keep)
        async with self.sm() as session, session.begin():
            return len(await self._close(session, where, "cancelled", {"reason": reason}))

    async def consume(self, approval_id: uuid.UUID) -> None:
        """The waiting run has taken the answer."""
        async with self.sm() as session, session.begin():
            await session.execute(update(Approval).where(Approval.id == approval_id, Approval.org_id == self.org_id,
                                                         Approval.consumed_at.is_(None)).values(consumed_at=func.now()))

    async def find_reusable(self, run_id: str, kind: str, payload: dict) -> dict | None:
        """An earlier request of this run for the same thing that the run has not finished with: still pending, or
        answered while no worker was waiting. A resumed run re-attaches to it instead of asking twice."""
        rid = to_uuid(run_id)
        wanted = fingerprint(kind, payload)
        async with self.sm() as session:
            rows = (await session.scalars(select(Approval).where(
                Approval.run_id == rid, Approval.org_id == self.org_id, Approval.kind == kind,
                (Approval.status == "pending") | (Approval.status.in_(ANSWERED) & Approval.consumed_at.is_(None)))
                .order_by(Approval.requested_at.desc()))).all()
        for row in rows:
            if fingerprint(row.kind, row.payload) == wanted:
                return as_dict(row)
        return None


async def expire_overdue(sessionmaker: async_sessionmaker[AsyncSession], grace_s: float = 10.0) -> int:
    """Expire pending approvals of every org whose deadline passed ``grace_s`` ago and that no live broker closed (its
    worker died). Used by the reapers."""
    async with sessionmaker() as session, session.begin():
        rows = (await session.execute(
            update(Approval).where(Approval.status == "pending", Approval.expires_at.is_not(None),
                                   Approval.expires_at < func.now() - func.make_interval(0, 0, 0, 0, 0, 0, float(grace_s)))
            .values(status="expired", responded_at=func.now()).returning(Approval.org_id, Approval.id))).all()
        for org_id, approval_id in rows:
            await session.execute(_notify(CHANNEL_APPROVAL_RESPONSES, org_id, approval_id))
    return len(rows)

