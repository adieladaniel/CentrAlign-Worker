"""Shared LLM rate limiter: one token bucket per API key, in PostgreSQL, used by every run of every process.

``ResilientLLM`` used to throttle per instance (one instance per run), so N parallel runs on one key fired N calls per
interval and tripped the provider's per-minute limit. ``PgRateLimiter`` replaces that throttle: the bucket lives in the
``llm_limits`` row of the key's SHA-256 hash.

* The bucket holds up to ``capacity`` tokens and refills at ``rate_per_s``; a call takes one token.
* All time arithmetic uses the **database clock** (``clock_timestamp()``, the statement-time variant of ``now()``, which
  would stay frozen at the start of the transaction), so processes with skewed local clocks still agree.
* ``acquire`` takes the row ``FOR UPDATE`` for one short transaction. When the bucket is empty it commits, **sleeps
  outside the transaction** for the computed deficit (plus a little jitter so waiting runs do not wake in lockstep) and
  tries again, so a waiting run never holds a lock or a connection.
* If the database is unreachable the call falls back to a per-instance throttle, so a limiter outage degrades to the
  old behaviour instead of failing the run.

Precision: a call starts at most ``capacity`` calls early and then no faster than ``rate_per_s`` on average. Wake-ups are
jittered by up to 10% of the deficit (+ 5 ms), and each acquisition costs one database round trip (a few ms).
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.db.models import LlmLimit
from worker.llm.resilient import LocalThrottle

log = logging.getLogger(__name__)


class PgRateLimiter:
    """Token bucket for one API key hash, shared through the ``llm_limits`` table (see the module docstring)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        key_hash: str,
        *,
        capacity: float = 2.0,
        rate_per_s: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_wait_s: float = 600.0,
    ):
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be positive (use no limiter for 'unlimited')")
        self.sm = sessionmaker
        self.key_hash = key_hash
        self.capacity = max(1.0, float(capacity))
        self.rate_per_s = float(rate_per_s)
        self._sleep = sleep
        self.max_wait_s = max_wait_s
        self._fallback = LocalThrottle(1.0 / self.rate_per_s, sleep=sleep)

    async def acquire(self) -> None:
        """Return when the caller may start a call: one token has been taken from the shared bucket."""
        waited = 0.0
        while True:
            try:
                deficit = await self._try_take()
            except Exception as exc:  # noqa: BLE001 - never fail a run because the limiter's database is down
                log.warning("shared rate limiter unavailable (%s); using a local throttle", type(exc).__name__)
                await self._fallback.acquire()
                return
            if deficit <= 0 or waited >= self.max_wait_s:
                return
            pause = deficit * random.uniform(1.0, 1.1) + 0.005
            await self._sleep(pause)
            waited += pause

    async def _try_take(self) -> float:
        """Take a token if one is available (returns 0.0), else return the seconds until one will be."""
        async with self.sm() as session, session.begin():
            await session.execute(pg_insert(LlmLimit).values(key_hash=self.key_hash, tokens=self.capacity)
                                  .on_conflict_do_nothing(index_elements=[LlmLimit.key_hash]))
            row = (await session.execute(
                select(LlmLimit.tokens, func.extract("epoch", func.clock_timestamp() - LlmLimit.updated_at))
                .where(LlmLimit.key_hash == self.key_hash).with_for_update())).one()
            elapsed = max(0.0, float(row[1]))
            tokens = min(self.capacity, float(row[0]) + elapsed * self.rate_per_s)
            if tokens < 1.0:
                return (1.0 - tokens) / self.rate_per_s
            await session.execute(update(LlmLimit).where(LlmLimit.key_hash == self.key_hash)
                                  .values(tokens=tokens - 1.0, updated_at=func.clock_timestamp()))
            return 0.0
