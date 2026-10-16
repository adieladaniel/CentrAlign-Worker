"""One dedicated PostgreSQL connection per process that LISTENs on a few channels and survives disconnects.

Channels used by the platform (``NOTIFY channel, 'payload'``; the payload is a short string, never data):

=================  ==============================  ===========================================================
channel            payload                          sent when
=================  ==============================  ===========================================================
``run_events``     ``<run_hex>:<seq>``              an event was stored (``PgRunStore.append_event``)
``runs_queued``    ``<run_hex>`` or ``slot``        a run was enqueued, re-queued, or a worker finished one
``run_control``    ``<run_hex>:cancel``             someone asked a running run to stop
``approvals``      ``<org_hex>:<approval_id>``      an approval or question was raised
``approval_responses`` ``<org_hex>:<approval_id>``  it was answered, expired or cancelled
=================  ==============================  ===========================================================

A notification is only a wake-up: every consumer re-reads the database (and also polls slowly), so a lost or duplicated
NOTIFY costs latency, never correctness. When the connection drops, ``PgListener`` reconnects with a growing delay and
calls the registered resync hooks, so consumers can re-read whatever they missed while it was down.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import asyncpg
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker.db.session import normalize_url

log = logging.getLogger("worker.listen")

CHANNEL_RUN_EVENTS = "run_events"
CHANNEL_RUNS_QUEUED = "runs_queued"
CHANNEL_RUN_CONTROL = "run_control"
CHANNEL_APPROVALS = "approvals"
CHANNEL_APPROVAL_RESPONSES = "approval_responses"

Callback = Callable[[str], None]
ResyncHook = Callable[[], "Awaitable[None] | None"]


def dsn_for(url: str | URL) -> str:
    """A plain ``postgresql://`` DSN for asyncpg from a SQLAlchemy URL or ``DATABASE_URL`` (contains the password:
    never log it)."""
    return normalize_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def dsn_of(sessionmaker: async_sessionmaker[AsyncSession]) -> str:
    """The DSN of the database a sessionmaker is bound to."""
    return dsn_for(sessionmaker.kw["bind"].url)


class PgListener:
    """LISTENs on ``channels`` over its own connection and hands each payload to the callbacks of that channel.

    Callbacks run in the event loop and must be quick and non-blocking (``queue.put_nowait``, ``event.set()``...).
    """

    def __init__(self, dsn: str, channels: Iterable[str], *, name: str = "listener", keepalive_s: float = 15.0,
                 initial_delay_s: float = 0.2, max_delay_s: float = 5.0):
        self._dsn = dsn
        self.channels = tuple(channels)
        self.name = name
        self.keepalive_s = keepalive_s
        self.initial_delay_s, self.max_delay_s = initial_delay_s, max_delay_s
        self._callbacks: dict[str, list[Callback]] = {channel: [] for channel in self.channels}
        self._resync: list[ResyncHook] = []
        self._task: asyncio.Task | None = None
        self._conn: asyncpg.Connection | None = None
        self._connected = asyncio.Event()
        self._stopping = False
        self.connects = 0  # successful connections so far (2+ means it had to reconnect)

    # ----------------------------------------------------------------- registration

    def subscribe(self, channel: str, callback: Callback) -> Callable[[], None]:
        """Call ``callback(payload)`` for every NOTIFY on ``channel``. Returns the function that removes it."""
        self._callbacks[channel].append(callback)

        def unsubscribe() -> None:
            if callback in self._callbacks[channel]:
                self._callbacks[channel].remove(callback)

        return unsubscribe

    def on_resync(self, hook: ResyncHook) -> None:
        """Call ``hook()`` after every *re*connection (notifications may have been missed while disconnected)."""
        self._resync.append(hook)

    # ----------------------------------------------------------------- lifecycle

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def backend_pid(self) -> int | None:
        """The server process of the listening connection (tests terminate it to simulate a dropped connection)."""
        return self._conn.get_server_pid() if self._conn is not None and not self._conn.is_closed() else None

    async def start(self, wait_s: float = 10.0) -> None:
        """Start listening; returns once connected, or after ``wait_s`` (it keeps retrying in the background)."""
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name=f"pg-listen-{self.name}")
        try:
            await asyncio.wait_for(self._connected.wait(), wait_s)
        except TimeoutError:
            log.warning("%s: the database is not reachable yet; will keep trying", self.name)

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._close()

    async def _close(self) -> None:
        conn, self._conn = self._conn, None
        self._connected.clear()
        if conn is not None and not conn.is_closed():
            try:
                await asyncio.wait_for(conn.close(), 3)
            except Exception:  # noqa: BLE001
                conn.terminate()

    # ----------------------------------------------------------------- the connection loop

    def _on_notify(self, _conn: Any, _pid: int, channel: str, payload: str) -> None:
        for callback in list(self._callbacks.get(channel, ())):
            try:
                callback(payload)
            except Exception:  # noqa: BLE001 - one broken consumer must not silence the others
                log.exception("%s: a %s callback failed", self.name, channel)

    async def _run(self) -> None:
        delay = self.initial_delay_s
        while not self._stopping:
            closed = asyncio.Event()
            try:
                conn = await asyncpg.connect(self._dsn, timeout=10)
                try:
                    conn.add_termination_listener(lambda _c, event=closed: event.set())  # bind this iteration's event
                    for channel in self.channels:
                        await conn.add_listener(channel, self._on_notify)
                except BaseException:
                    conn.terminate()
                    raise
                self._conn = conn
                self.connects += 1
                self._connected.set()
                delay = self.initial_delay_s
                if self.connects > 1:
                    log.info("%s: reconnected; resyncing", self.name)
                    await self._run_resync()
                while not closed.is_set():
                    try:
                        await asyncio.wait_for(closed.wait(), self.keepalive_s)
                    except TimeoutError:
                        await asyncio.wait_for(conn.execute("select 1"), 10)  # a silently dead connection raises here
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - OSError, asyncpg errors, timeouts: reconnect
                if not self._stopping:
                    log.warning("%s: connection lost (%s); reconnecting", self.name, type(exc).__name__)
            await self._close()
            if self._stopping:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_delay_s)

    async def _run_resync(self) -> None:
        for hook in list(self._resync):
            try:
                result = hook()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                log.exception("%s: a resync hook failed", self.name)
