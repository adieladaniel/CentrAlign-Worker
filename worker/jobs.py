"""The worker process of database mode: claims queued runs and executes them. ``python -m worker.jobs``.

::

    python -m worker.jobs [--concurrency K] [--worker-id ID] [--drain] [--metrics-port PORT]

One process runs up to K agent runs concurrently in one event loop, sharing one Chromium (``BrowserPool``), each run in
its own browser context. Any number of worker processes (on any machines that reach the database) can run side by side.

* **Claiming** (``jobqueue.claim_next``): a worker takes the oldest, highest-priority queued run whose org still has a free
  slot (``max_concurrent_runs``), sets a *lease*, and builds the run from the database: the org's context (settings, vault
  scope, simulator cookie), a ``PgRunStore``, a ``PgBroker`` for approvals and the LLM. It wakes on ``NOTIFY runs_queued``
  and polls every ``JOB_POLL_S`` as a fallback.
* **Heartbeat**: while a run executes, its lease is extended every third of ``JOB_LEASE_S``. If the heartbeat finds the
  lease gone (the run was reaped and may already run elsewhere), the local run is *fenced* (it can no longer write to
  the database) and cancelled at once.
* **Reaper**: every worker periodically takes back runs whose lease expired (their worker died): they go back to the
  queue and the next worker resumes them from the checkpoint; after ``JOB_MAX_ATTEMPTS`` claims they end as
  ``needs_attention``. It also expires approvals nobody can time out any more.
* **Cancellation**: ``POST /cancel`` sets ``runs.cancel_requested`` and sends ``NOTIFY run_control``; the heartbeat
  re-reads the flag as a fallback.
* **Observability**: every log line of a run carries its run, org, user and worker ids (``worker/logging_setup.py``); the
  worker counts runs, tools, LLM calls, approvals, leases and queue waits (``worker/metrics.py``) and serves them on
  ``127.0.0.1:PORT/metrics`` with ``--metrics-port``; at start it logs a readiness line (database, schema, simulator).
* **Shutdown** (Ctrl+C, SIGTERM, Ctrl+Break): stop claiming, then either hand every running run back to the queue so
  another worker resumes it (default), or with ``--drain`` let them finish. A second signal stops at once.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker import jobqueue, metrics
from worker.agent.pg_broker import ApprovalSignals, PgBroker
from worker.agent.runner import AgentRunner, ResumeError
from worker.agent.toolset import build_registry
from worker.config import Settings, get_settings
from worker.db.session import dispose_engine, get_sessionmaker
from worker.approvals_pg import expire_overdue
from worker.llm.base import LLMClient, LLMError
from worker.health import check_ready
from worker.llm.factory import make_llm
from worker.logging_setup import bind_context, log_context, setup_logging
from worker.memory_store import attach_org_memory, commit_run_memory
from worker.pg_listen import (
    CHANNEL_APPROVAL_RESPONSES, CHANNEL_RUN_CONTROL, CHANNEL_RUNS_QUEUED, PgListener, dsn_of,
)
from worker.run_context import OrgRunContext, build_llm, load_org_run_context, pin_environment, settings_for_run
from worker.store_pg import PgRunStore
from worker.tools.registry import ToolRegistry
from worker.vault import Vault, VaultError
from worker.vault_pg import open_pg_vault

log = logging.getLogger("worker.jobs")


class FencedStore:
    """Wraps a run's store; once ``fenced`` every write is silently dropped.

    A worker that lost its lease (or is handing the run back) must not change the run any more: another worker may be
    executing it. Reads and files still work, so the doomed runner can unwind and close its browser."""

    def __init__(self, inner: PgRunStore):
        self.inner = inner
        self.fenced = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def append_event(self, run_id: str, event_type: str, payload: dict) -> int:
        return 0 if self.fenced else await self.inner.append_event(run_id, event_type, payload)

    async def create_run(self, state: Any) -> None:
        if not self.fenced:
            await self.inner.create_run(state)

    async def save_state(self, state: Any) -> None:
        if not self.fenced:
            await self.inner.save_state(state)

    async def append_step(self, run_id: str, step: Any) -> None:
        if not self.fenced:
            await self.inner.append_step(run_id, step)

    async def upsert_fact(self, run_id: str, fact: Any) -> None:
        if not self.fenced:
            await self.inner.upsert_fact(run_id, fact)

    async def save_runtime(self, run_id: str, runtime: dict) -> None:
        if not self.fenced:
            await self.inner.save_runtime(run_id, runtime)

    async def save_report(self, run_id: str, report: dict, markdown: str) -> None:
        if not self.fenced:
            await self.inner.save_report(run_id, report, markdown)


@dataclass
class RunHandle:
    """A run this worker holds."""

    claim: jobqueue.ClaimedRun
    store: FencedStore
    llm: LLMClient | Exception
    org: OrgRunContext | None = None
    broker: PgBroker | None = None
    runner: AgentRunner | None = None
    task: asyncio.Task | None = None
    beat: asyncio.Task | None = None
    cancel_requested: bool = False
    lost: bool = False  # the lease was taken away
    release: bool = False  # the worker is stopping: hand the run back

    @property
    def run_id(self) -> str:
        return self.claim.id


class Worker:
    """Claims and executes runs. ``await worker.start()``, later ``await worker.stop()``; ``run_forever`` does both."""

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        worker_id: str | None = None,
        concurrency: int | None = None,
        llm_factory: Callable[..., LLMClient] = make_llm,
        registry_factory: Callable[[Settings], ToolRegistry] = build_registry,
        vault: Vault | None = None,
        dsn: str | None = None,
        shutdown_browser: bool = True,
    ):
        self.settings = settings
        self.sm = sessionmaker
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.concurrency = max(1, concurrency or settings.WORKER_CONCURRENCY)
        self.llm_factory, self.registry_factory = llm_factory, registry_factory
        self._vault = vault
        self._dsn = dsn
        self.shutdown_browser = shutdown_browser
        self.lease_s, self.poll_s = settings.JOB_LEASE_S, settings.JOB_POLL_S
        self.reaper_s, self.max_attempts = settings.JOB_REAPER_INTERVAL_S, settings.JOB_MAX_ATTEMPTS
        self.base_store = PgRunStore(sessionmaker, "00000000-0000-0000-0000-000000000000", artifacts_root=settings.RUNS_DIR)
        self.handles: dict[str, RunHandle] = {}
        self.claimed: list[str] = []  # every run this worker claimed, in order (introspection and tests)
        self.recent: OrderedDict[str, RunHandle] = OrderedDict()  # the last few handles, kept after their runs end
        self.peak = 0  # most runs held at once
        self.listener: PgListener | None = None
        self.signals: ApprovalSignals | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._tasks: list[asyncio.Task] = []

    # ----------------------------------------------------------------- lifecycle

    @property
    def vault(self) -> Vault:
        if self._vault is None:
            self._vault = open_pg_vault(self.sm, self.settings)
        return self._vault

    async def start(self) -> None:
        self.listener = PgListener(self._dsn or dsn_of(self.sm), (CHANNEL_RUNS_QUEUED, CHANNEL_RUN_CONTROL, CHANNEL_APPROVAL_RESPONSES),
                                   name=f"worker {self.worker_id}")
        self.listener.subscribe(CHANNEL_RUNS_QUEUED, lambda _payload: self._wake.set())
        self.listener.subscribe(CHANNEL_RUN_CONTROL, self._on_control)
        self.listener.on_resync(self._wake.set)
        await self.listener.start()
        self.signals = ApprovalSignals(self.listener)
        self._tasks = [asyncio.create_task(self._claim_loop(), name="claim-loop"),
                       asyncio.create_task(self._reaper_loop(), name="reaper-loop")]
        log.info("worker %s started (concurrency %d, lease %gs)", self.worker_id, self.concurrency, self.lease_s)

    async def stop(self, *, drain: bool = False) -> None:
        """Stop claiming. Running runs are handed back to the queue (default), or with ``drain`` allowed to finish."""
        self._stopping = True
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        running = [h.task for h in self.handles.values() if h.task is not None]
        if drain:
            log.info("draining %d running run(s)", len(running))
            await asyncio.gather(*running, return_exceptions=True)
        else:
            for handle in list(self.handles.values()):
                self._hand_back(handle)
            await asyncio.gather(*running, return_exceptions=True)
        if self.listener is not None:
            await self.listener.stop()
        if self.shutdown_browser:
            try:
                from worker.tools.browser_pool import shutdown_pool

                await shutdown_pool()
            except Exception:  # noqa: BLE001 - shutdown must finish
                log.exception("shutting down the browser pool failed")
        log.info("worker %s stopped", self.worker_id)

    async def run_forever(self, stop_event: asyncio.Event, force_event: asyncio.Event, *, drain: bool = False) -> None:
        await self.start()
        try:
            await stop_event.wait()
        finally:
            log.info("stopping (%s)", "draining" if drain else "handing running runs back to the queue")
            stopper = asyncio.create_task(self.stop(drain=drain))
            forcer = asyncio.create_task(force_event.wait())
            await asyncio.wait({stopper, forcer}, return_when=asyncio.FIRST_COMPLETED)
            if not stopper.done():  # a second signal: do not wait for the runs
                log.warning("second signal: stopping now")
                await self.stop(drain=False)
                await stopper
            forcer.cancel()

    # ----------------------------------------------------------------- claiming

    def _on_control(self, payload: str) -> None:
        run_id, _, command = payload.partition(":")
        handle = self.handles.get(run_id)
        if handle is not None and command == "cancel":
            self._cancel(handle)

    async def _claim_loop(self) -> None:
        try:
            while not self._stopping:
                self._wake.clear()  # before looking: a NOTIFY that arrives while we look must not be lost
                contended = False
                while len(self.handles) < self.concurrency and not self._stopping:
                    try:
                        claim, contended = await jobqueue.claim_next(
                            self.sm, self.worker_id, lease_s=self.lease_s,
                            default_limit=jobqueue.DEFAULT_MAX_CONCURRENT)
                    except Exception:  # noqa: BLE001 - the database may be restarting: try again after a pause
                        log.exception("claiming failed")
                        contended = False
                        break
                    if claim is None:
                        break
                    await self._launch(claim)
                if self._stopping:
                    return
                try:
                    await asyncio.wait_for(self._wake.wait(), 0.1 if contended else self.poll_s)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _launch(self, claim: jobqueue.ClaimedRun) -> None:
        """Start executing a freshly claimed run. Its org context and LLM are prepared here, in claim order (the claim
        loop is sequential), before the run's own task starts."""
        org: OrgRunContext | None = None
        try:
            org = await load_org_run_context(self.sm, self.vault, self.settings, claim.org_id)
            llm: LLMClient | Exception = build_llm(
                self.llm_factory, self.settings, org_id=str(claim.org_id),
                user_id=str(claim.created_by) if claim.created_by else None, run_id=claim.id, sessionmaker=self.sm,
                vault_scope=org.environment.scope if org.environment else None, org_settings=org.org.settings)
        except Exception as exc:  # noqa: BLE001 - reported on the run by _execute
            llm = exc
        store = FencedStore(self.base_store.for_org(claim.org_id, claim.created_by))
        handle = RunHandle(claim=claim, store=store, llm=llm, org=org, cancel_requested=claim.cancel_requested)
        self.handles[claim.id] = handle
        self.claimed.append(claim.id)
        self.recent[claim.id] = handle
        while len(self.recent) > 30:
            self.recent.popitem(last=False)
        self.peak = max(self.peak, len(self.handles))
        if claim.waited_s is not None and not claim.resume:
            metrics.QUEUE_WAIT.observe(max(0.0, claim.waited_s))
        with log_context(run_id=claim.id, org_id=claim.org_id, user_id=claim.created_by, worker_id=self.worker_id):
            # the run's tasks inherit this context: every log line they write names the run, org, user and worker
            handle.task = asyncio.create_task(self._execute(handle), name=f"run-{claim.id[:8]}")
            handle.beat = asyncio.create_task(self._heartbeat(handle), name=f"beat-{claim.id[:8]}")
            log.info("claimed run %s (org %s, attempt %d%s)", claim.id[:8], str(claim.org_id)[:8], claim.attempts,
                     ", resuming" if claim.resume else "")

    # ----------------------------------------------------------------- executing one run

    async def _execute(self, handle: RunHandle) -> None:
        claim, store = handle.claim, handle.store
        try:
            if isinstance(handle.llm, Exception):
                raise handle.llm
            org = handle.org
            assert org is not None
            if org.environment is None:
                await jobqueue.finish_unrun(store.inner, claim.id, "needs_attention",
                                            "This organization has no simulated company (environment) to work in.")
                return
            run_settings = settings_for_run(self.settings, org_cfg=org.cfg, requested_mode=claim.approval_mode,
                                            headless=bool(claim.options.get("headless", True)))
            handle.broker = PgBroker(self.sm, claim.org_id, claim.id, self.signals, timeout_s=org.cfg["approval_timeout_s"],
                                     poll_s=self.settings.APPROVAL_POLL_S)  # type: ignore[arg-type]
            registry = self.registry_factory(run_settings)
            scope = org.environment.scope
            if claim.resume:
                handle.runner = await AgentRunner.resume(claim.id, handle.llm, registry, run_settings, handle.broker,
                                                         store=store, vault_scope=scope)
            else:
                state = await store.load_state(claim.id)
                if state is None:
                    raise LookupError("the run disappeared")
                handle.runner = AgentRunner(state.task, handle.llm, registry, run_settings, handle.broker, store=store,
                                            state=state, vault_scope=scope, precreated=True)
            pin_environment(handle.runner, org.environment, run_settings)
            memory = attach_org_memory(handle.runner, self.sm, claim.org_id, org.cfg)
            if handle.cancel_requested:
                handle.runner.cancel()
            state = await handle.runner.run()
            if memory is not None and not (handle.lost or handle.release):  # the write gate: only a verified, ended run
                await commit_run_memory(
                    memory, state, created_by=claim.created_by, redactor=handle.runner.redactor, ttl_days=org.cfg["memory_ttl_days"],
                    emit=lambda event_type, payload: store.append_event(claim.id, event_type, payload))
        except asyncio.CancelledError:
            raise
        except ResumeError as err:
            log.info("run %s cannot be resumed: %s", claim.id[:8], err)
        except (LLMError, VaultError, LookupError) as err:
            await self._fail(handle, f"The run could not be started: {err}")
        except Exception as err:  # noqa: BLE001 - a bug must not leave the run 'running' forever
            log.exception("run %s crashed in the worker", claim.id[:8])
            await self._fail(handle, f"Internal worker error: {type(err).__name__}: {err}")
        finally:
            await self._finish(handle)

    async def _fail(self, handle: RunHandle, reason: str) -> None:
        if handle.lost or handle.release:
            return
        try:
            await jobqueue.finish_unrun(handle.store.inner, handle.run_id, "failed", reason)
        except Exception:  # noqa: BLE001
            log.exception("could not record the failure of run %s", handle.run_id[:8])

    async def _finish(self, handle: RunHandle) -> None:
        """The run's task ended: stop its heartbeat, then give the run back (shutdown) or drop the lease."""
        if handle.beat is not None:
            handle.beat.cancel()
        try:
            if handle.release and not handle.lost:
                if await jobqueue.requeue_for_shutdown(self.sm, handle.store.inner, handle.run_id, self.worker_id,
                                                       "worker shutting down"):
                    log.info("handed run %s back to the queue", handle.run_id[:8])
            elif not handle.lost:
                await jobqueue.release_lease(self.sm, handle.run_id, self.worker_id)
        except Exception:  # noqa: BLE001 - the lease simply expires and the reaper deals with the run
            log.exception("releasing run %s failed", handle.run_id[:8])
        self.handles.pop(handle.run_id, None)
        self._wake.set()

    # ----------------------------------------------------------------- heartbeat, cancel, hand back

    async def _heartbeat(self, handle: RunHandle) -> None:
        last_ok = time.monotonic()
        try:
            while True:
                await asyncio.sleep(self.lease_s / 3)
                try:
                    cancel = await jobqueue.heartbeat(self.sm, handle.run_id, self.worker_id, lease_s=self.lease_s)
                except Exception:  # noqa: BLE001
                    log.warning("heartbeat of run %s failed", handle.run_id[:8], exc_info=True)
                    if time.monotonic() - last_ok > self.lease_s:  # the lease has surely expired by now
                        self._lose(handle, "the database could not be reached for a whole lease")
                        return
                    continue
                if cancel is None:
                    self._lose(handle, "the lease is gone")
                    return
                last_ok = time.monotonic()
                if cancel and not handle.cancel_requested:
                    self._cancel(handle)
        except asyncio.CancelledError:
            pass

    def _cancel(self, handle: RunHandle) -> None:
        handle.cancel_requested = True
        if handle.runner is not None:
            handle.runner.cancel()

    def _lose(self, handle: RunHandle, why: str) -> None:
        """The run belongs to someone else (or may soon): stop it without touching the database any more."""
        if handle.lost:
            return
        log.warning("run %s: %s; cancelling it locally", handle.run_id[:8], why)
        metrics.LEASES_LOST.inc()
        handle.lost = True
        handle.store.fenced = True
        if handle.broker is not None:
            handle.broker.keep_pending = True
        self._cancel(handle)
        if handle.task is not None:
            handle.task.cancel()

    def _hand_back(self, handle: RunHandle) -> None:
        handle.release = True
        handle.store.fenced = True  # its last checkpoint is what the next worker resumes from
        if handle.broker is not None:
            handle.broker.keep_pending = True  # an open approval must outlive this worker
        if handle.task is not None:
            handle.task.cancel()

    # ----------------------------------------------------------------- reaper

    async def _reaper_loop(self) -> None:
        try:
            while not self._stopping:
                try:
                    for item in await jobqueue.reap(self.sm, self.base_store, max_attempts=self.max_attempts):
                        log.warning("run %s: worker %s lost (attempt %d) -> %s", item["run_id"][:8], item["worker"],
                                    item["attempts"], item["outcome"])
                    await expire_overdue(self.sm)
                except Exception:  # noqa: BLE001
                    log.exception("reaper sweep failed")
                await asyncio.sleep(self.reaper_s)
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------- command line


def install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event, force: asyncio.Event) -> None:
    """First Ctrl+C / SIGTERM / Ctrl+Break asks for a graceful stop, the next one for an immediate stop.

    ``loop.add_signal_handler`` does not exist on Windows, so this uses ``signal.signal`` there and hops into the loop."""

    def handler(_signum: int, _frame: Any) -> None:
        loop.call_soon_threadsafe(force.set if stop.is_set() else stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):  # not the main thread, or not supported here
            log.debug("cannot handle %s", name)


async def amain(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.db_auth:
        print("worker.jobs needs database mode: set DATABASE_URL (see .env.example) and run scripts/bootstrap.py.")
        return 2
    stop, force = asyncio.Event(), asyncio.Event()
    install_signal_handlers(asyncio.get_running_loop(), stop, force)
    sessionmaker = get_sessionmaker(settings)
    worker = Worker(settings, sessionmaker, worker_id=args.worker_id, concurrency=args.concurrency)
    bind_context(worker_id=worker.worker_id)  # every log line of this process names the worker
    stop_metrics = metrics.serve_metrics(args.metrics_port) if args.metrics_port else None
    try:
        report = await check_ready(settings, sessionmaker)
        log.log(logging.INFO if report["status"] == "ready" else logging.WARNING,
                "worker %s %s: %s%s", worker.worker_id, report["status"],
                ", ".join(f"{name}={value}" for name, value in report["checks"].items()),
                f"; metrics on 127.0.0.1:{args.metrics_port}" if args.metrics_port else "")
        await worker.run_forever(stop, force, drain=args.drain)
    finally:
        if stop_metrics is not None:
            stop_metrics()
        await dispose_engine()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker.jobs", description="Claim and execute queued agent runs.")
    parser.add_argument("--concurrency", type=int, default=None, help="runs executed at once (default WORKER_CONCURRENCY)")
    parser.add_argument("--worker-id", default=None, help="name written on the leases (default host:pid)")
    parser.add_argument("--drain", action="store_true",
                        help="on shutdown let running runs finish instead of handing them back to the queue")
    parser.add_argument("--metrics-port", type=int, default=None, metavar="PORT",
                        help="serve this worker's Prometheus /metrics on 127.0.0.1:PORT (default: off)")
    args = parser.parse_args(argv)
    setup_logging(get_settings())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
