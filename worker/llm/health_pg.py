"""Model health shared by every process: which Gemini models are out of quota / missing / overloaded, per API key.

``FailoverLLM`` reads health synchronously in a loop (``health.reason(model)``) and the runner reads
``llm.active_model`` the same way, so ``PgModelHealth`` keeps the synchronous ``ModelHealth`` interface and puts a small
in-process cache in front of the ``model_health`` table (keyed by key hash and model):

* **reads** come from the cache; ``await refresh()`` (called by ``FailoverLLM.complete`` before each call) re-reads the
  table at most every ``cache_ttl_s`` seconds (default 3), so a model that another process found exhausted is skipped
  here within a few seconds;
* **writes** (``mark_*``) update the cache at once and are written through to the table in a background task;
  ``await flush()`` (called by ``FailoverLLM`` right after marking) waits for them, so other processes see the change
  without waiting for a cache expiry. A failed write or read is logged and the in-process view keeps working.

Why a cache instead of making every health call async: ``reason()`` is called in tight loops and from synchronous
properties (``active_model``); an async interface would ripple through ``FailoverLLM``, the runner and the dashboard
for no gain, because a few seconds of staleness is harmless (the worst case is one extra call that fails over again).

The quota reset time (next Pacific midnight) is computed once by the writer and stored as an absolute timestamp, so a
reader just compares it with its clock; the processes' clocks only need to agree to within seconds.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.db.models import ModelHealth as ModelHealthRow
from worker.llm.failover import UNAVAILABLE_COOLDOWN_S, ModelHealth, next_quota_reset

log = logging.getLogger(__name__)

_Write = tuple[str, str, str, float]  # ("set", model, reason, usable_again_at) | ("clear", "", "", 0)


class PgModelHealth(ModelHealth):
    """``ModelHealth`` backed by the ``model_health`` table for one API key hash (see the module docstring)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        key_hash: str,
        *,
        clock: Callable[[], float] = time.time,
        cache_ttl_s: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        super().__init__(clock)
        self.sm = sessionmaker
        self.key_hash = key_hash
        self.cache_ttl_s = cache_ttl_s
        self._monotonic = monotonic
        self._loaded_at: float | None = None
        self._pending: list[_Write] = []
        self._writer: asyncio.Task | None = None

    # ---- synchronous interface (cache + write-through)
    def mark_exhausted(self, model: str) -> None:
        super().mark_exhausted(model)
        self._queue(model)

    def mark_missing(self, model: str) -> None:
        super().mark_missing(model)
        self._queue(model)

    def mark_unavailable(self, model: str, cooldown_s: float = UNAVAILABLE_COOLDOWN_S) -> None:
        super().mark_unavailable(model, cooldown_s)
        self._queue(model)

    def clear(self) -> None:
        super().clear()
        self._pending = [w for w in self._pending if w[0] != "set"] + [("clear", "", "", 0.0)]
        self._schedule()

    def _queue(self, model: str) -> None:
        until, why = self._bad[model]
        self._pending.append(("set", model, why, until))
        self._schedule()

    # ---- asynchronous side: refresh and write-through
    async def refresh(self) -> None:
        """Re-read the table unless the cache is younger than ``cache_ttl_s``. Never raises."""
        if self._loaded_at is not None and self._monotonic() - self._loaded_at < self.cache_ttl_s:
            return
        self._loaded_at = self._monotonic()  # set first: concurrent callers share this one read
        try:
            async with self.sm() as session:
                rows = (await session.execute(select(ModelHealthRow.model, ModelHealthRow.status, ModelHealthRow.until)
                                              .where(ModelHealthRow.key_hash == self.key_hash))).all()
        except Exception as exc:  # noqa: BLE001 - keep working from the cache
            log.warning("could not read the shared model health (%s)", type(exc).__name__)
            return
        now = self._clock()
        fresh = {model: (until.timestamp(), status) for model, status, until in rows
                 if until is not None and until.timestamp() > now}
        unsent = {w[1] for w in self._pending if w[0] == "set"}
        for model in unsent:  # a change of ours that the table does not know yet wins over the table
            if model in self._bad:
                fresh[model] = self._bad[model]
        self._bad = fresh

    async def flush(self) -> None:
        """Wait until every ``mark_*`` / ``clear`` so far reached the table. Never raises."""
        writer = self._writer
        if writer is not None and not writer.done():
            await asyncio.gather(writer, return_exceptions=True)
        await self._drain()

    def _schedule(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop: the writes wait for the next flush()
        if self._writer is None or self._writer.done():
            self._writer = loop.create_task(self._drain())

    async def _drain(self) -> None:
        while self._pending:
            write = self._pending[0]  # stays listed while in flight, so refresh() keeps the local change
            try:
                await self._write(write)
            except Exception as exc:  # noqa: BLE001 - other processes miss this change; this one still has it cached
                log.warning("could not write the shared model health (%s)", type(exc).__name__)
            if self._pending and self._pending[0] is write:
                self._pending.pop(0)

    async def _write(self, write: _Write) -> None:
        kind, model, why, until = write
        async with self.sm() as session, session.begin():
            if kind == "clear":
                await session.execute(delete(ModelHealthRow).where(ModelHealthRow.key_hash == self.key_hash))
                return
            values = {"status": why, "until": datetime.fromtimestamp(until, tz=timezone.utc), "reason": why}
            await session.execute(pg_insert(ModelHealthRow).values(key_hash=self.key_hash, model=model, **values)
                                  .on_conflict_do_update(index_elements=[ModelHealthRow.key_hash, ModelHealthRow.model],
                                                         set_=values))


# One PgModelHealth per (database, key) in a process, so every run shares one cache (and one refresh per interval).
_registry: dict[str, tuple[async_sessionmaker, PgModelHealth]] = {}


def shared_health(sessionmaker: async_sessionmaker[AsyncSession], key_hash: str, cache_ttl_s: float = 3.0) -> PgModelHealth:
    """The process-wide ``PgModelHealth`` of ``key_hash`` on this database."""
    entry = _registry.get(key_hash)
    if entry is None or entry[0] is not sessionmaker:
        entry = _registry[key_hash] = (sessionmaker, PgModelHealth(sessionmaker, key_hash, cache_ttl_s=cache_ttl_s))
    return entry[1]
