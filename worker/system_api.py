"""Operations endpoints: ``/healthz``, ``/readyz``, ``/metrics`` and the admin summary ``GET /api/admin/system``.

* ``/healthz``: liveness, always fast, no dependencies.
* ``/readyz``: readiness (database, migrations, simulated company; see ``worker/health.py``), 503 when something is missing.
* ``/metrics``: Prometheus text format of this process (``worker/metrics.py``). Access:

  - a ``Authorization: Bearer <METRICS_TOKEN>`` header (for scrapers), or
  - a signed-in admin or owner (database mode) / the shared-password session (file mode with ``ACCESS_PASSWORD``);
  - with neither and no way to authenticate it is **open only in local development**: ``DEPLOY_MODE`` off, no
    ``METRICS_TOKEN``, no ``ACCESS_PASSWORD``. Any of those turns protection on (401 without credentials, 403 for a
    signed-in user who is not an admin).
* ``GET /api/admin/system`` (admin or owner; database mode): a JSON summary for the Admin page. An org admin sees their own
  org only (queue, runs in flight, runs finished in the last 24 hours, run-time percentiles, the workers holding their
  runs). Admins listed in ``PLATFORM_ADMIN_EMAILS`` see every org.

``QueueDepthSampler`` keeps the ``oneforall_queue_depth`` gauge fresh: every ``QUEUE_SAMPLE_S`` an API process counts the
queued runs per org with one query on the partial index of the queue.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker import metrics
from worker.auth import SESSION_COOKIE, AuthService, OrgContext, require_db_role
from worker.config import Settings
from worker.deploy import AccessControl
from worker.health import check_ready
from worker.jobqueue import ACTIVE_STATUSES, LIVE_STATUSES

log = logging.getLogger("worker.system")

FINISHED_WINDOW_H = 24


# --------------------------------------------------------------------------- queue depth sampling


async def queue_depths(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Queued runs per org slug (one grouped query over the queue's partial index)."""
    async with sessionmaker() as session:
        rows = (await session.execute(text(
            "select o.slug, count(*) from runs r join orgs o on o.id = r.org_id where r.status = 'queued' "
            "group by o.slug"))).all()
    return {slug: int(count) for slug, count in rows}


async def sample_queue_depth(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    depths = await queue_depths(sessionmaker)
    metrics.set_queue_depth(depths)
    return depths


class QueueDepthSampler:
    """Background task that refreshes the queue-depth gauge. ``start()`` in the app's lifespan, ``await stop()`` after."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], interval_s: float):
        self.sm, self.interval_s = sessionmaker, max(1.0, interval_s)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="queue-depth-sampler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await sample_queue_depth(self.sm)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the database may be restarting; try again next time
                log.warning("queue depth sampling failed (%s)", type(exc).__name__)
            await asyncio.sleep(self.interval_s)


# --------------------------------------------------------------------------- health and metrics routes


def install_ops_routes(
    app: FastAPI,
    *,
    settings: Settings,
    service: AuthService | None,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    access: AccessControl | None = None,
) -> None:
    """Register ``/healthz``, ``/readyz`` and ``/metrics`` on ``app`` (the three are reachable without a session; /metrics
    does its own authorization)."""

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        client = app.state.sim_http if getattr(app.state, "sim_http", None) is not None else None
        report = await check_ready(app.state.settings, sessionmaker, sim_client=client)
        return JSONResponse(report, status_code=200 if report["status"] == "ready" else 503, headers={"Cache-Control": "no-store"})

    async def authorize_metrics(request: Request) -> None:
        current: Settings = app.state.settings
        token = current.METRICS_TOKEN
        header = request.headers.get("authorization", "")
        if token and header.lower().startswith("bearer ") and secrets.compare_digest(
                header[7:].strip().encode(), token.encode()):
            return
        signed_in_not_admin = False
        if service is not None:
            ctx = await service.resolve(request.cookies.get(SESSION_COOKIE))
            if ctx is not None and ctx.has_role("admin"):
                return
            signed_in_not_admin = ctx is not None
        elif access is not None and access.enabled and access.is_authenticated(dict(request.cookies)):
            return
        protected = current.DEPLOY_MODE or bool(token) or (service is None and access is not None and access.enabled)
        if not protected:
            return  # local development: no token, no deployment, no password
        if signed_in_not_admin:
            raise HTTPException(status_code=403, detail="Metrics need the admin role or the metrics token.")
        raise HTTPException(status_code=401, detail="Metrics need the admin role or the metrics token.",
                            headers={"WWW-Authenticate": "Bearer"})

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics(request: Request) -> Response:
        await authorize_metrics(request)
        return Response(generate_latest(metrics.REGISTRY), headers={"Content-Type": CONTENT_TYPE_LATEST,
                                                                    "Cache-Control": "no-store"})


# --------------------------------------------------------------------------- GET /api/admin/system


def _round(value: Any, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def build_system_router(*, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])
    admin = require_db_role("admin")

    @router.get("/system")
    async def system(ctx: OrgContext = Depends(admin)) -> dict:
        platform = ctx.email.lower() in settings.platform_admin_emails
        scope_org: uuid.UUID | None = None if platform else ctx.org_id
        org_filter = "" if platform else " and r.org_id = :org"
        params: dict[str, Any] = {} if platform else {"org": scope_org}
        stale_s = max(30.0, 3 * settings.JOB_LEASE_S)
        async with sessionmaker() as session:
            queue_rows = (await session.execute(text(f"""
                select o.slug, o.name,
                       count(*) filter (where r.status = 'queued') as queued,
                       count(*) filter (where r.status in ('running', 'verifying')) as running,
                       count(*) filter (where r.status in ('waiting_approval', 'waiting_user')) as waiting,
                       extract(epoch from now() - min(r.queued_at) filter (where r.status = 'queued')) as oldest_s
                from runs r join orgs o on o.id = r.org_id
                where r.status = any(:live){org_filter}
                group by o.slug, o.name order by o.slug"""), {"live": list(LIVE_STATUSES), **params})).all()
            finished_rows = (await session.execute(text(f"""
                select r.status, count(*) from runs r
                where r.finished_at > now() - make_interval(hours => :hours){org_filter}
                group by r.status"""), {"hours": FINISHED_WINDOW_H, **params})).all()
            timing = (await session.execute(text(f"""
                select count(*) as n,
                       percentile_cont(0.5) within group (order by e) as p50,
                       percentile_cont(0.95) within group (order by e) as p95
                from (select greatest(extract(epoch from r.finished_at - r.started_at), 0) as e from runs r
                      where r.finished_at > now() - make_interval(hours => :hours) and r.started_at is not null
                        and r.status in ('succeeded', 'failed', 'needs_attention'){org_filter}) t"""),
                {"hours": FINISHED_WINDOW_H, **params})).one()
            worker_rows = (await session.execute(text(f"""
                select r.lease_owner, count(*) as runs,
                       extract(epoch from now() - max(r.heartbeat_at)) as heartbeat_age_s
                from runs r
                where r.status = any(:active) and r.lease_owner is not null
                  and r.heartbeat_at > now() - make_interval(secs => :stale){org_filter}
                group by r.lease_owner order by r.lease_owner"""),
                {"active": list(ACTIVE_STATUSES), "stale": stale_s, **params})).all()
        queue = [{"org": r.slug, "name": r.name, "queued": r.queued, "running": r.running, "waiting": r.waiting,
                  "oldest_queued_s": _round(r.oldest_s, 0)} for r in queue_rows]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scope": "platform" if platform else "org",
            "org": None if platform else {"slug": ctx.org_slug, "name": ctx.org_name},
            "queue": queue,
            "totals": {"queued": sum(q["queued"] for q in queue), "running": sum(q["running"] for q in queue),
                       "waiting": sum(q["waiting"] for q in queue)},
            "workers": {"active": len(worker_rows),
                        "list": [{"worker": r.lease_owner, "runs": r.runs, "heartbeat_age_s": _round(r.heartbeat_age_s)}
                                 for r in worker_rows],
                        "note": "workers currently holding a run" + ("" if platform else " of this organization")},
            "finished_24h": {"total": sum(n for _s, n in finished_rows),
                             "by_status": {status: n for status, n in sorted(finished_rows)}},
            "run_seconds_24h": {"n": timing.n, "p50": _round(timing.p50), "p95": _round(timing.p95),
                                "note": "wall time from start to finish, approval waits included"},
        }

    return router
