"""Live events across processes, for the API.

The agent runs in a worker process; the dashboard talks to an API process. They meet in PostgreSQL: the runner stores each
event in ``run_events`` (numbered by ``seq``) and sends ``NOTIFY run_events '<run_hex>:<seq>'``. Every API process keeps
**one** listening connection (``EventHub``) and fans out to the WebSockets watching a run:

1. a WebSocket subscribes first (``subscribe``), *then* reads the stored events (the replay), *then* streams what the hub
   delivers, skipping any ``seq`` it already sent: no gap and no duplicate, whatever the timing;
2. on a NOTIFY the hub reads the new events of that run from the database (``seq > cursor``) and queues them for
   its subscribers; it does not trust the payload beyond "something happened to this run";
3. a slow poll (``LIVE_FALLBACK_POLL_S``) and a re-read after every reconnection of the listening connection cover a lost
   NOTIFY, so a database restart only pauses the stream.

The hub also lets the approvals inbox hear that something changed in an org (``watch_org``).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from worker.pg_listen import CHANNEL_APPROVAL_RESPONSES, CHANNEL_APPROVALS, CHANNEL_RUN_EVENTS, PgListener
from worker.store_pg import PgRunStore

log = logging.getLogger("worker.live")


@dataclass(eq=False)
class Subscription:
    """One watcher of a run. ``queue`` yields stored events in ``seq`` order, then ``None`` when the run has finished."""

    run_id: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)


@dataclass(eq=False)
class _Feed:
    run_id: str
    store: PgRunStore
    cursor: int | None = None  # newest event already handed to the subscribers
    subs: set[Subscription] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task | None = None
    dirty: bool = False


class EventHub:
    """The API process's single LISTEN connection and the fan-out built on it. Starts on first use."""

    def __init__(self, dsn: str, *, poll_s: float = 5.0):
        self._dsn = dsn
        self.listener = self._new_listener()
        self.poll_s = poll_s
        self._feeds: dict[str, _Feed] = {}
        self._org_watchers: dict[str, set[asyncio.Queue]] = {}
        self._start_lock = asyncio.Lock()
        self._poller: asyncio.Task | None = None
        self._started = False

    # ----------------------------------------------------------------- lifecycle

    def _new_listener(self) -> PgListener:
        return PgListener(self._dsn, (CHANNEL_RUN_EVENTS, CHANNEL_APPROVALS, CHANNEL_APPROVAL_RESPONSES), name="api")

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            self.listener.subscribe(CHANNEL_RUN_EVENTS, self._on_run_event)
            self.listener.subscribe(CHANNEL_APPROVALS, self._on_approval)
            self.listener.subscribe(CHANNEL_APPROVAL_RESPONSES, self._on_approval)
            self.listener.on_resync(self._resync)
            await self.listener.start()
            self._poller = asyncio.create_task(self._poll_loop(), name="live-poll")
            self._started = True

    async def stop(self) -> None:
        async with self._start_lock:
            if not self._started:
                return
            self._started = False
            if self._poller is not None:
                self._poller.cancel()
                await asyncio.gather(self._poller, return_exceptions=True)
            for feed in list(self._feeds.values()):
                if feed.task is not None:
                    feed.task.cancel()
                for sub in feed.subs:
                    sub.queue.put_nowait(None)
            self._feeds.clear()
            await self.listener.stop()
            self.listener = self._new_listener()  # the hub can be started again

    # ----------------------------------------------------------------- runs

    async def subscribe(self, run_id: str, store: PgRunStore) -> Subscription:
        """Start collecting the events of ``run_id`` (which ``store``'s org owns). Read the replay *after* this call."""
        await self.ensure_started()
        feed = self._feeds.get(run_id)
        if feed is None:
            feed = self._feeds[run_id] = _Feed(run_id, store)
        sub = Subscription(run_id)
        feed.subs.add(sub)
        async with feed.lock:
            if feed.cursor is None:  # events up to here are in the caller's replay; later ones arrive by NOTIFY
                feed.cursor = await store.last_event_seq(run_id)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        feed = self._feeds.get(sub.run_id)
        if feed is None:
            return
        feed.subs.discard(sub)
        if not feed.subs:
            if feed.task is not None:
                feed.task.cancel()
            del self._feeds[sub.run_id]

    def _on_run_event(self, payload: str) -> None:
        feed = self._feeds.get(payload.partition(":")[0])
        if feed is not None:
            self._kick(feed)

    def _kick(self, feed: _Feed) -> None:
        if feed.task is not None and not feed.task.done():
            feed.dirty = True
        else:
            feed.task = asyncio.create_task(self._pump(feed), name=f"pump-{feed.run_id[:8]}")

    async def _pump(self, feed: _Feed) -> None:
        try:
            while True:
                feed.dirty = False
                async with feed.lock:
                    if feed.cursor is not None:
                        for event in await feed.store.read_events(feed.run_id, after_seq=feed.cursor):
                            feed.cursor = event["seq"]
                            for sub in list(feed.subs):
                                sub.queue.put_nowait(event)
                            if event["type"] == "run_finished":
                                for sub in list(feed.subs):
                                    sub.queue.put_nowait(None)
                if not feed.dirty:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - e.g. the database is restarting; the poll or the resync retries
            log.warning("reading events of run %s failed", feed.run_id[:8], exc_info=True)

    async def _poll_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.poll_s)
                for feed in list(self._feeds.values()):
                    self._kick(feed)
        except asyncio.CancelledError:
            pass

    def _resync(self) -> None:
        """The listening connection was down: events and approvals may have been missed."""
        log.info("listener reconnected: re-reading %d watched run(s)", len(self._feeds))
        for feed in list(self._feeds.values()):
            self._kick(feed)
        for queues in self._org_watchers.values():
            self._ping(queues)

    # ----------------------------------------------------------------- the approvals inbox

    def watch_org(self, org_id: str) -> asyncio.Queue:
        """A queue that receives a token whenever an approval of the org changes (coalesced: at most one waiting)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._org_watchers.setdefault(org_id, set()).add(queue)
        return queue

    def unwatch_org(self, org_id: str, queue: asyncio.Queue) -> None:
        watchers = self._org_watchers.get(org_id)
        if watchers is not None:
            watchers.discard(queue)
            if not watchers:
                del self._org_watchers[org_id]

    def _on_approval(self, payload: str) -> None:
        queues = self._org_watchers.get(payload.partition(":")[0])
        if queues:
            self._ping(queues)

    @staticmethod
    def _ping(queues: set[asyncio.Queue]) -> None:
        for queue in queues:
            try:
                queue.put_nowait("changed")
            except asyncio.QueueFull:
                pass  # one is already waiting: the reader will refetch anyway
