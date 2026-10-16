"""LLM usage accounting and per-organisation daily budgets (database mode).

* ``UsageRecorder`` is the ``usage`` hook of ``ResilientLLM``: after every LLM call it inserts one ``usage_events`` row
  (org, user, run, model, calls, input/output tokens). It is deliberately forgiving: a failure is logged by the caller and
  never fails the run, and a row whose run or user reference is rejected is still stored without it, so the org's budget
  stays accurate.
* ``QuotaGuard`` is the ``quota`` hook: before each call it compares the org's usage today (UTC day) with the org's
  ``max_tokens_per_day`` / ``max_llm_calls_per_day`` and raises ``QuotaExceededError`` when one is used up. The totals
  and limits are cached for a few seconds (one small query per refresh, not per call) and bumped locally by every call
  this process records, so the check is cheap and a single process notices its own spending at once; other processes'
  spending shows up at the next refresh. One call can overshoot the budget (the check happens before the call).
  If the database cannot be read the guard lets the call through (and logs): accounting trouble must not stop work.
* ``usage_report`` / ``usage_totals`` aggregate the ledger for the admin endpoints (``worker/usage_api.py``).

Tokens are input + output tokens. 0 as a limit means unlimited.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import Date, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.config import Settings
from worker.db.models import Org, UsageEvent, User
from worker.llm.base import QuotaExceededError
from worker.org_settings import llm_budget

log = logging.getLogger(__name__)

RECORD_TIMEOUT_S = 5.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_day_start(now: datetime | None = None) -> datetime:
    """Midnight (UTC) of the day containing ``now``: the start of the budget period."""
    return (now or utc_now()).astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


# --------------------------------------------------------------------------- budget check


class QuotaGuard:
    """Cached daily-budget check for one organisation (see the module docstring)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        org_id: uuid.UUID | str,
        *,
        ttl_s: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utc_now,
    ):
        self.sm = sessionmaker
        self.org_id = _as_uuid(org_id)
        self.ttl_s = ttl_s
        self._monotonic = monotonic
        self._now = now
        self._loaded_at: float | None = None
        self._day: date | None = None
        self._calls = 0
        self._tokens = 0
        self.max_tokens = 0
        self.max_calls = 0

    async def check(self) -> None:
        """Raise ``QuotaExceededError`` when the org has used up its budget for today."""
        await self._refresh()
        if self.max_tokens and self._tokens >= self.max_tokens:
            raise QuotaExceededError(f"Organization daily LLM budget reached ({self._tokens:,} of "
                                     f"{self.max_tokens:,} tokens); resets at 00:00 UTC")
        if self.max_calls and self._calls >= self.max_calls:
            raise QuotaExceededError(f"Organization daily LLM budget reached ({self._calls:,} of "
                                     f"{self.max_calls:,} LLM calls); resets at 00:00 UTC")

    def note(self, calls: int, tokens: int) -> None:
        """Add a call this process just made to the cached totals (the next refresh replaces them with the database's)."""
        if self._day == self._now().date():
            self._calls += calls
            self._tokens += tokens

    async def _refresh(self) -> None:
        today = self._now().date()
        fresh = self._loaded_at is not None and self._day == today and self._monotonic() - self._loaded_at < self.ttl_s
        if fresh:
            return
        self._loaded_at = self._monotonic()  # set first: concurrent callers share this one read
        try:
            async with self.sm() as session:
                settings = await session.scalar(select(Org.settings).where(Org.id == self.org_id))
                calls, tokens = await usage_totals(session, self.org_id, utc_day_start(self._now()))
        except Exception as exc:  # noqa: BLE001 - fail open: accounting trouble must not stop the run
            log.warning("could not read the org's LLM usage (%s); not enforcing the budget this time", type(exc).__name__)
            return
        budget = llm_budget(settings)
        self.max_tokens, self.max_calls = budget["max_tokens_per_day"], budget["max_llm_calls_per_day"]
        self._day, self._calls, self._tokens = today, calls, tokens


# One guard per (database, org) in a process, so parallel runs of an org see each other's spending without a refresh.
_guards: dict[str, tuple[async_sessionmaker, QuotaGuard]] = {}


def guard_for(sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID | str, deployment: Settings) -> QuotaGuard:
    """The process-wide ``QuotaGuard`` of ``org_id`` on this database."""
    key = str(org_id)
    entry = _guards.get(key)
    if entry is None or entry[0] is not sessionmaker:
        entry = _guards[key] = (sessionmaker, QuotaGuard(sessionmaker, org_id, ttl_s=deployment.LLM_QUOTA_CACHE_S))
    return entry[1]


# --------------------------------------------------------------------------- recording


class UsageRecorder:
    """Writes one ``usage_events`` row per LLM call of one run (the ``usage`` hook of ``ResilientLLM``)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        org_id: uuid.UUID | str,
        user_id: uuid.UUID | str | None = None,
        run_id: uuid.UUID | str | None = None,
        guard: QuotaGuard | None = None,
        timeout_s: float = RECORD_TIMEOUT_S,
    ):
        self.sm = sessionmaker
        self.org_id = _as_uuid(org_id)
        self.user_id = _as_uuid(user_id)
        self.run_id = _as_uuid(run_id)
        self.guard = guard
        self.timeout_s = timeout_s

    async def record(self, *, model: str, calls: int, input_tokens: int, output_tokens: int) -> None:
        """Insert the row (raises on failure; ``ResilientLLM`` logs that and carries on)."""
        if self.guard is not None:
            self.guard.note(calls, input_tokens + output_tokens)
        row = dict(org_id=self.org_id, user_id=self.user_id, run_id=self.run_id, model=model[:200], calls=calls,
                   input_tokens=input_tokens, output_tokens=output_tokens)
        try:
            await asyncio.wait_for(self._insert(row), self.timeout_s)
        except IntegrityError:  # the run row is not stored (yet) or the user is gone: keep the org-level numbers
            log.warning("usage row for a run/user that is not stored; recording it without them")
            await asyncio.wait_for(self._insert({**row, "run_id": None, "user_id": None}), self.timeout_s)

    async def _insert(self, row: dict[str, Any]) -> None:
        async with self.sm() as session, session.begin():
            session.add(UsageEvent(**row))


# --------------------------------------------------------------------------- reading


async def usage_totals(
    session: AsyncSession, org_id: uuid.UUID, since: datetime, user_id: uuid.UUID | None = None
) -> tuple[int, int]:
    """``(calls, tokens)`` of the org (or one user of it) from ``since`` on."""
    query = select(func.coalesce(func.sum(UsageEvent.calls), 0),
                   func.coalesce(func.sum(UsageEvent.input_tokens + UsageEvent.output_tokens), 0)
                   ).where(UsageEvent.org_id == org_id, UsageEvent.ts >= since)
    if user_id is not None:
        query = query.where(UsageEvent.user_id == user_id)
    calls, tokens = (await session.execute(query)).one()
    return int(calls), int(tokens)


def _day_rows(rows: list, first: date, last: date) -> list[dict[str, Any]]:
    """One entry per day from ``first`` to ``last`` (days without usage are zeros), oldest first."""
    by_day = {row.day: row for row in rows}
    out = []
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        row = by_day.get(day)
        inp, outp = (int(row.input_tokens), int(row.output_tokens)) if row else (0, 0)
        out.append({"day": day.isoformat(), "calls": int(row.calls) if row else 0, "input_tokens": inp,
                    "output_tokens": outp, "tokens": inp + outp})
    return out


async def usage_report(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    *,
    days: int = 7,
    top: int = 10,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The org's usage for the last ``days`` UTC days: per-day totals, top users and models, and today against the budget."""
    now = now or utc_now()
    today = now.astimezone(timezone.utc).date()
    first = today - timedelta(days=days - 1)
    since = utc_day_start(now) - timedelta(days=days - 1)
    day = cast(func.timezone("UTC", UsageEvent.ts), Date).label("day")
    tokens = func.sum(UsageEvent.input_tokens + UsageEvent.output_tokens)
    in_window = (UsageEvent.org_id == org_id, UsageEvent.ts >= since)
    async with sessionmaker() as session:
        org_settings = await session.scalar(select(Org.settings).where(Org.id == org_id))
        daily = (await session.execute(
            select(day, func.sum(UsageEvent.calls).label("calls"), func.sum(UsageEvent.input_tokens).label("input_tokens"),
                   func.sum(UsageEvent.output_tokens).label("output_tokens")).where(*in_window).group_by(day))).all()
        by_user = (await session.execute(
            select(UsageEvent.user_id, User.email, User.display_name, func.sum(UsageEvent.calls), tokens.label("tokens"))
            .join(User, User.id == UsageEvent.user_id, isouter=True).where(*in_window)
            .group_by(UsageEvent.user_id, User.email, User.display_name)
            .order_by(tokens.desc()).limit(top))).all()
        by_model = (await session.execute(
            select(UsageEvent.model, func.sum(UsageEvent.calls), tokens.label("tokens")).where(*in_window)
            .group_by(UsageEvent.model).order_by(tokens.desc()).limit(top))).all()
    daily_rows = _day_rows(daily, first, today)
    return {
        "days": days,
        "daily": daily_rows,
        "today": daily_rows[-1],
        "limits": llm_budget(org_settings),
        "resets_at": (utc_day_start(now) + timedelta(days=1)).isoformat(),
        "by_user": [{"user_id": str(uid) if uid else None, "email": email, "name": name or email, "calls": int(calls),
                     "tokens": int(tok)} for uid, email, name, calls, tok in by_user],
        "by_model": [{"model": model or "(unknown)", "calls": int(calls), "tokens": int(tok)} for model, calls, tok in by_model],
    }


async def user_usage_today(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: uuid.UUID, user_id: uuid.UUID, *, now: datetime | None = None,
) -> dict[str, Any]:
    """What one user's runs used today (UTC day) in the org, with the org's limits for context."""
    now = now or utc_now()
    async with sessionmaker() as session:
        org_settings = await session.scalar(select(Org.settings).where(Org.id == org_id))
        row = (await session.execute(
            select(func.coalesce(func.sum(UsageEvent.calls), 0), func.coalesce(func.sum(UsageEvent.input_tokens), 0),
                   func.coalesce(func.sum(UsageEvent.output_tokens), 0))
            .where(UsageEvent.org_id == org_id, UsageEvent.user_id == user_id,
                   UsageEvent.ts >= utc_day_start(now)))).one()
    calls, inp, outp = (int(v) for v in row)
    return {"day": now.astimezone(timezone.utc).date().isoformat(), "calls": calls, "input_tokens": inp,
            "output_tokens": outp, "tokens": inp + outp,
            "limits": llm_budget(org_settings),
            "resets_at": (utc_day_start(now) + timedelta(days=1)).isoformat()}
