"""Load test and proof of scale: many organizations, users and runs against a real local stack.

    python scripts/loadtest.py --orgs 3 --users 4 --runs 30 --workers 2 --concurrency 4
    python scripts/loadtest.py --profile stress --orgs 5 --runs 60 --workers 3 --concurrency 4 --chaos-rate 0.2 --kill-worker-at 50%

What it starts (all as subprocesses of this script, on ports from ``--base-port``, never 8000/8001):

* a throwaway database ``oneforall_load`` (created, migrated, dropped at the end unless ``--keep-db``; the dev database is never touched),
* the simulated company with a temporary data dir (one isolated tenant per org),
* the API (``uvicorn worker.server:app``) and N worker processes (``python -m worker.jobs --concurrency K``),
  all with ``LLM_PROVIDER=policy``: a deterministic stand-in for the model (``worker/llm/policy.py``) that really drives
  the browser, the vault logins, the approval gate, verification and organization memory. No model quota is used.

Who uses it (asyncio, real HTTP logins and WebSockets through the API): operators submit runs at a Poisson or fixed
arrival rate; approvers of the SAME org watch the approvals inbox (push stream plus polling) and approve after a random
human delay; viewers open run pages and live event streams (the fan-out load) and keep probing other orgs' runs; a fraction of
runs get chaos flags (a one-time ERP HTTP 500, an expiring ERP session); optionally one worker is killed mid-test.

What it measures: outcomes, ground-truth correctness (each org's ERP is read through the sim's admin API and compared with the
invoices the org's tasks asked for), end-to-end latency, queue wait, approval wait, throughput, worker memory (psutil) and
contexts, LLM limiter waits, requeues and resumes, errors by type, and tenant/org isolation (every response of every
simulated user is scanned for another org's identifiers, and explicit cross-org probes must be refused).

Output: a console table, ``--out`` JSON (one entry per ``--profile``, merged into the file) and a Markdown summary next to it.
Everything it started is stopped by PID at the end, also on Ctrl+C.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import psutil  # noqa: E402
import websockets  # noqa: E402
from sqlalchemy import text  # noqa: E402

from evals.checkers import bill_mismatches, bills_for  # noqa: E402
from evals.simstate import build_sim_state, latest_invoice  # noqa: E402
from worker.config import get_settings  # noqa: E402
from worker.db import migrate  # noqa: E402
from worker.db.bootstrap import create_database, drop_database  # noqa: E402
from worker.db.models import DEFAULT_ORG_SETTINGS, Membership, Org, User  # noqa: E402
from worker.db.seed import hash_password  # noqa: E402
from worker.db.session import make_engine, make_sessionmaker, mask_url, normalize_url  # noqa: E402
from worker.vault import generate_key  # noqa: E402

IS_WINDOWS = os.name == "nt"
PASSWORD = "Load-Test-" + secrets.token_hex(4)  # one throwaway password for every simulated user
VENDORS = ["Company X Ltd", "Globex Corporation", "Initech Supplies", "Umbrella Logistics", "Stark Components"]
TASK_SUFFIX = ", extract the amount and due date, enter it into our internal system, and tell me once it is done."
FINAL = ("succeeded", "failed", "needs_attention", "cancelled")
CHAOS_CHOICES = (["erp_submit_500_once"], ["erp_session_expiry"], ["erp_submit_500_once", "erp_session_expiry"])
UUID_RE = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}|[0-9a-f]{32}")


def log(message: str) -> None:
    print(f"[loadtest {time.strftime('%H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------- small statistics


def pct(values: list[float], p: float) -> float | None:
    """The p-th percentile (0-100) by linear interpolation; None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * p / 100
    low, high = math.floor(pos), math.ceil(pos)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def summary(values: list[float], digits: int = 2) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {"n": len(values), "mean": round(sum(values) / len(values), digits), "p50": round(pct(values, 50), digits),
            "p95": round(pct(values, 95), digits), "max": round(max(values), digits)}


def histogram_percentile(buckets: list[tuple[float, float]], p: float) -> float | None:
    """Percentile estimate from cumulative Prometheus buckets ``[(le, count)]`` (linear inside the bucket)."""
    buckets = sorted(buckets)
    total = buckets[-1][1] if buckets else 0
    if not total:
        return None
    target, prev_le, prev_count = total * p / 100, 0.0, 0.0
    for le, count in buckets:
        if count >= target:
            if math.isinf(le):
                return prev_le
            return prev_le + (le - prev_le) * ((target - prev_count) / max(count - prev_count, 1e-9))
        prev_le, prev_count = le, count
    return prev_le


def parse_range(text_value: str) -> tuple[float, float]:
    low, _, high = text_value.partition("-")
    return float(low), float(high or low)


# --------------------------------------------------------------------------- configuration


@dataclass
class Config:
    profile: str
    orgs: int
    users: int
    runs: int
    workers: int
    concurrency: int
    chaos_rate: float
    think_ms: float
    approve_delay_ms: tuple[float, float]
    arrival: str
    arrival_rate: float
    kill_worker_at: str | None
    kill_mode: str
    restart_api_at: str | None
    respawn_after_s: float
    llm_rate: float
    llm_burst: float
    org_concurrency: int
    watchers: int
    dup_rate: float
    lease_s: float
    reaper_s: float
    timeout_s: float
    keep_db: bool
    out: Path
    base_port: int
    seed: int
    db_name: str

    @property
    def api_port(self) -> int:
        return self.base_port

    @property
    def sim_port(self) -> int:
        return self.base_port + 1

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    @property
    def sim_url(self) -> str:
        return f"http://127.0.0.1:{self.sim_port}"

    def metrics_port(self, index: int) -> int:
        return 9601 + index


def parse_args(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", default="run", help="name of this run in the results file (default: run)")
    p.add_argument("--orgs", type=int, default=3)
    p.add_argument("--users", type=int, default=4, help="users per org, at least 4 (admin, operator, approver, viewer; extra users are added as operator/approver/viewer in turn)")
    p.add_argument("--runs", type=int, default=30, help="runs submitted in total, spread over the orgs")
    p.add_argument("--workers", type=int, default=2, help="worker processes")
    p.add_argument("--concurrency", type=int, default=4, help="runs each worker executes at once")
    p.add_argument("--chaos-rate", type=float, default=0.0, help="fraction of runs that get a chaos flag armed while they run")
    p.add_argument("--think-ms", type=float, default=800.0, help="simulated model latency per LLM call (jittered +-40%%)")
    p.add_argument("--approve-delay-ms", default="500-3000", help="human delay before an approver answers, 'lo-hi'")
    p.add_argument("--arrival", choices=("poisson", "fixed", "burst"), default="poisson", help="how run submissions are spread in time")
    p.add_argument("--arrival-rate", type=float, default=0.5, help="runs submitted per second (poisson/fixed)")
    p.add_argument("--kill-worker-at", default=None, metavar="50%|40s", help="kill worker 1 (and its browsers) when this share of the runs has finished, or after this many seconds")
    p.add_argument("--kill-mode", choices=("kill", "stop"), default="kill", help="kill: hard-kill the worker and its browsers (a crash); stop: Ctrl+Break, a graceful shutdown that hands its runs back")
    p.add_argument("--restart-api-at", default=None, metavar="50%|40s", help="hard-restart the API process at this point (pending approvals must survive)")
    p.add_argument("--respawn-after-s", type=float, default=0.0, help="start a replacement worker this long after the kill (0 = never)")
    p.add_argument("--llm-rate", type=float, default=25.0, help="shared LLM calls per second (one token bucket for all runs, like one API key)")
    p.add_argument("--llm-burst", type=float, default=25.0, help="burst size of that bucket")
    p.add_argument("--org-concurrency", type=int, default=4, help="max_concurrent_runs of every org")
    p.add_argument("--watchers", type=int, default=2, help="viewer WebSocket event streams opened per run")
    p.add_argument("--dup-rate", type=float, default=0.1, help="fraction of runs that repeat an earlier task of their org (the bill exists or is being created)")
    p.add_argument("--lease-s", type=float, default=15.0, help="JOB_LEASE_S (a lost worker's runs are taken back after this)")
    p.add_argument("--reaper-s", type=float, default=5.0, help="JOB_REAPER_INTERVAL_S")
    p.add_argument("--timeout-s", type=float, default=1500.0, help="give up waiting for the runs after this long")
    p.add_argument("--keep-db", action="store_true", help="do not drop the throwaway database at the end")
    p.add_argument("--out", default="docs/loadtest-results.json")
    p.add_argument("--base-port", type=int, default=8601, help="API port; the simulator takes the next one, worker metrics start at 9601")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--db-name", default="oneforall_load", help="must start with oneforall_load")
    a = p.parse_args(argv)
    if a.users < 4:
        p.error("--users must be at least 4")
    if not a.db_name.startswith("oneforall_load"):
        p.error("--db-name must start with 'oneforall_load' (the dev database is never used)")
    if a.base_port in (8000, 8001) or a.base_port + 1 in (8000, 8001):
        p.error("ports 8000/8001 belong to the dev stack")
    return Config(profile=a.profile, orgs=a.orgs, users=a.users, runs=a.runs, workers=a.workers, concurrency=a.concurrency,
                  chaos_rate=a.chaos_rate, think_ms=a.think_ms, approve_delay_ms=parse_range(a.approve_delay_ms), arrival=a.arrival,
                  arrival_rate=a.arrival_rate, kill_worker_at=a.kill_worker_at, kill_mode=a.kill_mode, restart_api_at=a.restart_api_at,
                  respawn_after_s=a.respawn_after_s,
                  llm_rate=a.llm_rate, llm_burst=a.llm_burst, org_concurrency=a.org_concurrency, watchers=a.watchers,
                  dup_rate=a.dup_rate, lease_s=a.lease_s, reaper_s=a.reaper_s, timeout_s=a.timeout_s, keep_db=a.keep_db,
                  out=(ROOT / a.out) if not os.path.isabs(a.out) else Path(a.out), base_port=a.base_port, seed=a.seed,
                  db_name=a.db_name)


# --------------------------------------------------------------------------- the processes


def kill_tree(pid: int) -> None:
    """Kill a process we started and everything it started (browsers included), by PID."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    victims = root.children(recursive=True) + [root]
    for proc in victims:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=5)


class Stack:
    """The throwaway database and the processes of the stack under test. ``kill_all`` is safe to call at any time."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tmp = Path(tempfile.mkdtemp(prefix="oneforall_load_"))
        (self.tmp / "logs").mkdir()
        self.procs: dict[str, subprocess.Popen] = {}
        self._logs: list[Any] = []
        self.admin_token = secrets.token_urlsafe(16)
        base = normalize_url(get_settings().DATABASE_URL.strip())  # server and credentials of the dev database ...
        self.db_url = base.set(database=cfg.db_name)  # ... but our own database
        self.vault_key = generate_key()
        self.db_created = False

    # ---- environment of the children
    def env(self, **extra: str) -> dict[str, str]:
        c = self.cfg
        env = {k: v for k, v in os.environ.items() if k not in ("ACCESS_PASSWORD", "GEMINI_API_KEY", "DEPLOY_MODE")}
        env.update(
            DATABASE_URL=self.db_url.render_as_string(hide_password=False), AUTH_MODE="db", LLM_PROVIDER="policy",
            POLICY_THINK_MS=str(c.think_ms), LLM_REFILL_PER_S=str(c.llm_rate), LLM_BUCKET_CAPACITY=str(c.llm_burst),
            SIM_BASE_URL=c.sim_url, SIM_ADMIN_TOKEN=self.admin_token, WORKSPACE_DIR=str(self.tmp / "workspace"),
            RUNS_DIR=str(self.tmp / "runs"), VAULT_DIR=str(self.tmp / "vault"), VAULT_KEY=self.vault_key,
            SESSION_SECRET=secrets.token_urlsafe(16), HEADLESS="true", JOB_LEASE_S=str(c.lease_s),
            JOB_REAPER_INTERVAL_S=str(c.reaper_s), WORKER_CONCURRENCY=str(c.concurrency),
            BROWSER_MAX_CONTEXTS=str(max(8, c.concurrency + 2)), LOG_FORMAT="json", LOG_LEVEL="INFO", QUEUE_SAMPLE_S="5",
            PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", HTTP_ALLOWED_HOSTS="127.0.0.1,localhost",
        )
        env.update(extra)
        return env

    def spawn(self, name: str, args: list[str], **extra_env: str) -> subprocess.Popen:
        logfile = open(self.tmp / "logs" / f"{name}.log", "ab")
        self._logs.append(logfile)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0  # Ctrl+C of this console must not hit the children
        proc = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=self.env(**extra_env), stdout=logfile,
                                stderr=subprocess.STDOUT, creationflags=flags)
        self.procs[name] = proc
        log(f"started {name} (pid {proc.pid})")
        return proc

    async def wait_http(self, url: str, name: str, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if self.procs[name].poll() is not None:
                    raise RuntimeError(f"{name} exited early with code {self.procs[name].returncode}; see {self.tmp / 'logs' / (name + '.log')}")
                try:
                    if (await client.get(url)).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.4)
        raise RuntimeError(f"{name} did not become healthy within {timeout:.0f}s ({url})")

    def start_worker(self, index: int) -> subprocess.Popen:
        return self.spawn(f"worker{index + 1}", ["-m", "worker.jobs", "--worker-id", f"lw-{index + 1}", "--concurrency",
                                                  str(self.cfg.concurrency), "--metrics-port", str(self.cfg.metrics_port(index))])

    def stop(self, name: str, grace: float = 10.0) -> None:
        proc = self.procs.get(name)
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGINT)
            proc.wait(timeout=grace)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        kill_tree(proc.pid)  # whatever is left of it, children included

    def kill_all(self) -> None:
        for name in reversed(list(self.procs)):
            self.stop(name, grace=8.0)
        for logfile in self._logs:
            try:
                logfile.close()
            except OSError:
                pass

    async def create_database(self) -> None:
        await drop_database(self.db_url)  # a leftover of an interrupted earlier run
        await create_database(self.db_url)
        self.db_created = True
        engine = make_engine(self.db_url)
        try:
            await migrate.upgrade(engine)
        finally:
            await engine.dispose()

    async def drop_database(self) -> None:
        if self.db_created:
            await drop_database(self.db_url)
            self.db_created = False


# --------------------------------------------------------------------------- recording what the simulated users see


class Stats:
    """Latency and outcome of every API call the simulated users made, by route."""

    def __init__(self) -> None:
        self.latency: dict[str, list[float]] = defaultdict(list)
        self.errors: Counter[str] = Counter()
        self.error_samples: dict[str, str] = {}

    @staticmethod
    def route(method: str, path: str) -> str:
        return f"{method} " + UUID_RE.sub("{id}", path.split("?")[0])

    def record(self, method: str, path: str, status: int, seconds: float, detail: str = "", expected: tuple[int, ...] = ()) -> None:
        route = self.route(method, path)
        self.latency[route].append(seconds)
        if (status >= 400 or status == 0) and status not in expected:  # a refusal the test asked for is not an error
            key = f"{route} -> {status}"
            self.errors[key] += 1
            self.error_samples.setdefault(key, detail[:160])

    def table(self) -> dict[str, dict]:
        return {route: {**summary(values, 3), "errors": sum(n for k, n in self.errors.items() if k.startswith(route + " ->"))}
                for route, values in sorted(self.latency.items())}


class IsolationGuard:
    """Scans everything a simulated user receives for identifiers of OTHER orgs (ids, run ids, e-mail domains, tenant names)."""

    def __init__(self) -> None:
        self.run_owner: dict[str, int] = {}  # run id (hex) -> org index
        self.org_ids: dict[int, str] = {}
        self.slugs: dict[int, str] = {}
        self.checked = 0
        self.violations: list[str] = []
        self.probes = 0
        self.probe_failures: list[str] = []

    def register_org(self, index: int, org_id: str, slug: str) -> None:
        self.org_ids[index], self.slugs[index] = org_id, slug

    def check(self, org_index: int, where: str, body: str) -> None:
        self.checked += 1
        for other, org_id in self.org_ids.items():
            if other == org_index:
                continue
            slug = self.slugs[other]
            hit = (org_id in body or org_id.replace("-", "") in body
                   or re.search(rf"@{re.escape(slug)}\.test\b", body) or re.search(rf"(?<![\w-])org-{re.escape(slug)}(?![\w-])", body))
            if hit:
                self.violations.append(f"{where}: response of org {org_index} mentions org {other}")
        for run_id in set(UUID_RE.findall(body)):
            owner = self.run_owner.get(run_id.replace("-", ""))
            if owner is not None and owner != org_index:
                self.violations.append(f"{where}: response of org {org_index} contains run {run_id[:8]} of org {owner}")

    def probe_failed(self, message: str) -> None:
        self.probe_failures.append(message)


class Client:
    """One simulated user: a real login and cookie jar, every call timed and scanned."""

    def __init__(self, cfg: Config, org_index: int, email: str, role: str, stats: Stats, guard: IsolationGuard):
        self.cfg, self.org_index, self.email, self.role, self.stats, self.guard = cfg, org_index, email, role, stats, guard
        self.http = httpx.AsyncClient(base_url=cfg.api_url, timeout=45.0, limits=httpx.Limits(max_connections=12))

    async def login(self) -> None:
        for attempt in range(3):
            resp = await self.call("POST", "/api/auth/login", json={"email": self.email, "password": PASSWORD})
            if resp.status_code == 200:
                return
            await asyncio.sleep(1 + attempt)
        raise RuntimeError(f"cannot sign in as {self.email}")

    async def call(self, method: str, path: str, *, expected: tuple[int, ...] = (), **kwargs: Any) -> httpx.Response:
        """One API call. ``expected``: statuses that are the right answer (cross-org probes expect a refusal)."""
        started = time.monotonic()
        try:
            resp = await self.http.request(method, path, **kwargs)
        except httpx.HTTPError as err:
            self.stats.record(method, path, 0, time.monotonic() - started, type(err).__name__)
            return httpx.Response(599, text=type(err).__name__)
        self.stats.record(method, path, resp.status_code, time.monotonic() - started, resp.text if resp.status_code >= 400 else "", expected)
        if "login" not in path:
            self.guard.check(self.org_index, f"{method} {Stats.route(method, path)}", resp.text)
        return resp

    def ws(self, path: str):
        token = self.http.cookies.get("ofa_session") or ""
        return websockets.connect(f"ws://127.0.0.1:{self.cfg.api_port}{path}", additional_headers={"Cookie": f"ofa_session={token}"},
                                  open_timeout=15, max_size=2**22)

    async def close(self) -> None:
        await self.http.aclose()


# --------------------------------------------------------------------------- the simulated population


@dataclass
class RunRec:
    seq: int
    org: int
    task: str
    invoice: str  # the invoice number the task is about (ground truth)
    chaos: list[str]
    run_id: str = ""
    operator: str = ""
    submitted_at: float = 0.0
    submit_status: int = 0
    ws_events: int = 0
    ws_closed_cleanly: bool | None = None


@dataclass
class OrgSim:
    index: int
    slug: str
    org_id: str = ""
    tenant_id: str = ""
    clients: dict[str, list[Client]] = field(default_factory=lambda: defaultdict(list))
    tasks: list[tuple[str, str]] = field(default_factory=list)  # (task text, invoice number)
    runs: list[RunRec] = field(default_factory=list)
    handled: set[str] = field(default_factory=set)  # approval ids an approver of this org already took
    baseline_bills: list[dict] = field(default_factory=list)
    sim: dict = field(default_factory=dict)

    def pick(self, role: str, rng: random.Random) -> Client:
        return rng.choice(self.clients[role])


def user_plan(users: int) -> list[str]:
    """Roles of the users of one org: admin, operator, approver, viewer, then more operators/approvers/viewers in turn."""
    roles = ["admin", "operator", "approver", "viewer"]
    extra = ["approver", "operator", "viewer"]
    return roles + [extra[i % 3] for i in range(users - 4)]


class LoadTest:
    def __init__(self, cfg: Config, stack: Stack):
        self.cfg, self.stack = cfg, stack
        self.rng = random.Random(cfg.seed)
        self.stats, self.guard = Stats(), IsolationGuard()
        self.orgs: list[OrgSim] = []
        self.sm = None
        self.engine = None
        self.stop = asyncio.Event()
        self.t0 = 0.0
        self.samples: list[dict] = []
        self.last_metrics: dict[str, str] = {}
        self.killed: dict[str, Any] = {}
        self.api_restart: dict[str, Any] = {}
        self.armed: set[str] = set()
        self.arm_count = 0
        self.approval_delays: dict[str, float] = {}
        self.notice_lags: list[float] = []
        self.ws_stats = Counter()
        self.ws_problems: list[str] = []
        self.finished_count = 0
        self.db_before: dict = {}
        self.tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ setup

    async def setup(self) -> None:
        cfg, stack = self.cfg, self.stack
        self.engine = make_engine(stack.db_url, pool_size=4, max_overflow=4)
        self.sm = make_sessionmaker(self.engine)
        sim_env = {"SIM_DATA_DIR": str(stack.tmp / "sim"), "SIM_PORT": str(cfg.sim_port), "SIM_HOST": "127.0.0.1"}
        stack.spawn("sim", ["-m", "company_sim"], **sim_env)
        await stack.wait_http(f"{cfg.sim_url}/healthz", "sim")
        stack.spawn("api", ["-m", "uvicorn", "worker.server:app", "--host", "127.0.0.1", "--port", str(cfg.api_port),
                            "--no-access-log", "--log-level", "warning"])
        await stack.wait_http(f"{cfg.api_url}/api/health", "api")
        await self.create_orgs()
        stats_t = time.monotonic()
        await self.provision()
        log(f"orgs provisioned in {time.monotonic() - stats_t:.1f}s")
        await self.login_everyone()
        await self.plan_tasks()
        for i in range(cfg.workers):
            stack.start_worker(i)
        await asyncio.gather(*(stack.wait_http(f"http://127.0.0.1:{cfg.metrics_port(i)}/metrics", f"worker{i + 1}") for i in range(cfg.workers)))
        self.db_before = await self.db_counters()

    async def create_orgs(self) -> None:
        password_hash = hash_password(PASSWORD)  # one hash for everybody: hashing is deliberately slow
        async with self.sm() as session, session.begin():
            for index in range(self.cfg.orgs):
                org = OrgSim(index=index, slug=f"load{index + 1}")
                row = Org(slug=org.slug, name=f"Load Org {index + 1}", settings=dict(DEFAULT_ORG_SETTINGS))
                session.add(row)
                await session.flush()
                org.org_id, org.tenant_id = str(row.id), f"org-{org.slug}"
                self.guard.register_org(index, org.org_id, org.slug)
                counters: Counter[str] = Counter()
                for role in user_plan(self.cfg.users):
                    counters[role] += 1
                    user = User(email=f"{role}{counters[role]}@{org.slug}.test", display_name=f"{role.title()} {counters[role]} ({org.slug})",
                                password_hash=password_hash)
                    session.add(user)
                    await session.flush()
                    session.add(Membership(org_id=row.id, user_id=user.id, role=role))
                    org.clients[role].append(Client(self.cfg, index, user.email, role, self.stats, self.guard))
                self.orgs.append(org)

    async def provision(self) -> None:
        """Through the real admin API: sign the org's admin in, set its limits, create its simulated company and vault entries."""
        async def one(org: OrgSim) -> None:
            admin = org.clients["admin"][0]
            await admin.login()
            body = {"max_concurrent_runs": self.cfg.org_concurrency, "max_runs_per_day": 0, "max_tokens_per_day": 0,
                    "max_llm_calls_per_day": 0, "max_steps": 60, "run_timeout_s": 600, "approval_timeout_s": 900}
            resp = await admin.call("PUT", "/api/admin/settings", json=body)
            if resp.status_code != 200:
                raise RuntimeError(f"settings of {org.slug} refused: {resp.status_code} {resp.text[:200]}")
            resp = await admin.call("POST", "/api/admin/environment/provision")
            if resp.status_code != 200:
                raise RuntimeError(f"provisioning of {org.slug} failed: {resp.status_code} {resp.text[:200]}")
            org.tenant_id = resp.json()["tenant_id"]
        await asyncio.gather(*(one(org) for org in self.orgs))

    async def login_everyone(self) -> None:
        pending = [c for org in self.orgs for role, cs in org.clients.items() for c in cs if role != "admin"]
        sem = asyncio.Semaphore(4)

        async def one(client: Client) -> None:
            async with sem:
                await client.login()
        await asyncio.gather(*(one(c) for c in pending))

    async def sim_state(self, org: OrgSim) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(f"{self.cfg.sim_url}/admin/tenants/{org.tenant_id}/state", headers={"X-Admin-Token": self.stack.admin_token})
            resp.raise_for_status()
            return build_sim_state(resp.json())

    async def plan_tasks(self) -> None:
        """Each org gets distinct work first (the latest invoice of every vendor, then single invoices), then repeats."""
        per_org = [0] * len(self.orgs)
        for i in range(self.cfg.runs):
            per_org[i % len(self.orgs)] += 1
        for org in self.orgs:
            org.sim = await self.sim_state(org)
            org.baseline_bills = org.sim["bills"]
            billed = {b["invoice_number"] for b in org.baseline_bills}
            latest = {v: latest_invoice(org.sim["invoices"], v)["number"] for v in VENDORS}
            candidates: list[tuple[str, str]] = [(f"Find the latest invoice from {v}{TASK_SUFFIX}", n) for v, n in latest.items() if n not in billed]
            used = set(latest.values()) | billed
            candidates += [(f"Find invoice {i['number']} in the vendor portal{TASK_SUFFIX}", i["number"])
                           for i in org.sim["invoices"] if i["number"] not in used and i["portal_visible"]]
            self.rng.shuffle(candidates)
            wanted = per_org[org.index]
            tasks = [candidates[i % len(candidates)] for i in range(wanted)]
            for i in range(1, len(tasks)):  # some runs repeat an earlier task of the same org
                if self.rng.random() < self.cfg.dup_rate:
                    tasks[i] = tasks[self.rng.randrange(i)]
            org.tasks = tasks

    # ------------------------------------------------------------------ the simulated users

    async def submit_all(self) -> None:
        cfg = self.cfg
        order: list[RunRec] = []
        cursors = [0] * len(self.orgs)
        for i in range(cfg.runs):
            org = self.orgs[i % len(self.orgs)]
            task, invoice = org.tasks[cursors[org.index]]
            cursors[org.index] += 1
            chaos = list(self.rng.choice(CHAOS_CHOICES)) if self.rng.random() < cfg.chaos_rate else []
            order.append(RunRec(seq=i, org=org.index, task=task, invoice=invoice, chaos=chaos))
        clock = 0.0
        for rec in order:
            if cfg.arrival == "poisson":
                clock += self.rng.expovariate(cfg.arrival_rate)
            elif cfg.arrival == "fixed":
                clock += 1.0 / cfg.arrival_rate
            await asyncio.sleep(max(0.0, self.t0 + clock - time.monotonic()))
            if self.stop.is_set():
                return
            await self.submit(rec)

    async def submit(self, rec: RunRec) -> None:
        org = self.orgs[rec.org]
        operator = org.pick("operator", self.rng)
        rec.operator, rec.submitted_at = operator.email, time.monotonic()
        org.runs.append(rec)
        resp = await operator.call("POST", "/api/runs", json={"task": rec.task})
        rec.submit_status = resp.status_code
        if resp.status_code != 200:
            return
        rec.run_id = resp.json()["run_id"]
        self.guard.run_owner[rec.run_id] = rec.org
        for k in range(self.cfg.watchers):  # live event streams: the fan-out load
            viewer = org.clients["viewer"][(rec.seq + k) % len(org.clients["viewer"])]
            self.tasks.append(asyncio.create_task(self.watch_run(viewer, rec), name=f"watch-{rec.seq}-{k}"))

    async def watch_run(self, viewer: Client, rec: RunRec) -> None:
        """Follow a run over its WebSocket until it ends: events must arrive in order, belong to the run, and end with run_finished."""
        self.ws_stats["opened"] += 1
        last_seq, last_type = 0, ""
        try:
            async with viewer.ws(f"/api/runs/{rec.run_id}/events") as ws:
                async for raw in ws:
                    self.guard.check(viewer.org_index, "WS run events", raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
                    event = json.loads(raw)
                    if event.get("seq", 0) < 0:  # replay_done marker
                        continue
                    self.ws_stats["events"] += 1
                    rec.ws_events += 1
                    if event["seq"] <= last_seq:
                        self.ws_stats["order_violations"] += 1
                        self.ws_problems.append(f"run {rec.run_id[:8]}: seq {event['seq']} after {last_seq}")
                    last_seq, last_type = event["seq"], event.get("type", "")
                    rid = (event.get("payload") or {}).get("run_id")
                    if rid and rid.replace("-", "") != rec.run_id:
                        self.ws_stats["foreign_events"] += 1
                        self.ws_problems.append(f"run {rec.run_id[:8]}: event for run {rid[:8]}")
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - a dropped stream is a finding, not a crash of the test
            self.ws_stats["errors"] += 1
            self.ws_problems.append(f"run {rec.run_id[:8]}: stream ended with {type(err).__name__}: {str(err)[:80]}")
            return
        rec.ws_closed_cleanly = last_type == "run_finished"
        self.ws_stats["finished" if rec.ws_closed_cleanly else "ended_without_run_finished"] += 1

    async def approver_loop(self, org: OrgSim, client: Client) -> None:
        wake = asyncio.Event()
        lo, hi = self.cfg.approve_delay_ms[0] / 1000, self.cfg.approve_delay_ms[1] / 1000

        async def stream() -> None:
            while not self.stop.is_set():
                try:
                    async with client.ws("/api/approvals/stream") as ws:
                        async for raw in ws:
                            self.guard.check(client.org_index, "WS approvals", raw if isinstance(raw, str) else raw.decode())
                            wake.set()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - reconnect; the polling below keeps the inbox served
                    await asyncio.sleep(1)

        async def answer(item: dict, delay: float) -> None:
            await asyncio.sleep(delay)
            body = ({"request_id": item["request_id"], "approved": True, "note": "load test"} if item["kind"] == "approval"
                    else {"request_id": item["request_id"], "answer": "Stop the task"})
            await client.call("POST", f"/api/runs/{item['run_id']}/respond", json=body)

        self.tasks.append(asyncio.create_task(stream(), name=f"approvals-ws-{client.email}"))
        while not self.stop.is_set():
            resp = await client.call("GET", "/api/approvals?status=pending")
            if resp.status_code == 200:
                for item in resp.json():
                    allowed = item["can_approve"] if item["kind"] == "approval" else item["can_respond"]
                    if item["id"] in org.handled or not allowed:
                        continue
                    org.handled.add(item["id"])
                    delay = self.rng.uniform(lo, hi)
                    self.approval_delays[item["id"]] = delay
                    requested = datetime.fromisoformat(item["requested_at"]).timestamp()
                    self.notice_lags.append(max(0.0, time.time() - requested))
                    self.tasks.append(asyncio.create_task(answer(item, delay), name=f"answer-{item['id'][:8]}"))
            try:
                await asyncio.wait_for(wake.wait(), timeout=2.0)
            except TimeoutError:
                pass
            wake.clear()

    async def viewer_loop(self, org: OrgSim, client: Client) -> None:
        """Browses like a person: the run list, a run's page, the approvals inbox; plus cross-org probes that must be refused."""
        while not self.stop.is_set():
            await asyncio.sleep(self.rng.uniform(0.7, 1.5))
            roll = self.rng.random()
            if roll < 0.3:
                await client.call("GET", "/api/runs")
            elif roll < 0.6 and org.runs:
                rec = self.rng.choice([r for r in org.runs if r.run_id] or [None])
                if rec:
                    await client.call("GET", f"/api/runs/{rec.run_id}")
            elif roll < 0.75:
                await client.call("GET", "/api/approvals?status=all&limit=20")
            else:
                await self.probe(client)

    async def probe(self, client: Client) -> None:
        """Ask for another org's run the way that role could: every attempt must look like 'unknown' (or be forbidden)."""
        foreign = [r for o in self.orgs if o.index != client.org_index for r in o.runs if r.run_id]
        if not foreign:
            return
        rec = self.rng.choice(foreign)
        self.guard.probes += 1
        refused = (404,) if client.role == "viewer" else (403, 404)
        if client.role == "viewer":
            checks = [("GET", f"/api/runs/{rec.run_id}", None), ("GET", f"/runs/{rec.run_id}/files/report.md", None)]
        elif client.role == "approver":
            checks = [("POST", f"/api/runs/{rec.run_id}/respond", {"request_id": "0" * 32, "approved": True})]
        else:
            checks = [("POST", f"/api/runs/{rec.run_id}/cancel", None)]
        for method, path, body in checks:
            resp = await client.call(method, path, expected=refused, **({"json": body} if body else {}))
            if resp.status_code not in refused:
                self.guard.probe_failed(f"{method} {Stats.route(method, path)} as {client.role} -> {resp.status_code}")
        if client.role != "viewer":
            return
        try:
            async with client.ws(f"/api/runs/{rec.run_id}/events") as ws:
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if first.get("type") != "error":
                    self.guard.probe_failed(f"WS foreign run delivered {first.get('type')}")
        except websockets.ConnectionClosed:
            pass  # closed without data: also a refusal
        except Exception as err:  # noqa: BLE001
            self.guard.probe_failed(f"WS foreign run probe failed: {type(err).__name__}")

    async def prober_loop(self, client: Client) -> None:
        """Operators and approvers also try to reach other orgs' runs now and then."""
        while not self.stop.is_set():
            await asyncio.sleep(self.rng.uniform(1.5, 3.0))
            await self.probe(client)

    # ------------------------------------------------------------------ chaos, kill, sampling

    async def chaos_armer(self) -> None:
        """Arm a run's chaos flags in its org's company once the run is executing (flags are per company, so the fault
        hits whichever run of that org reaches the ERP form or its 4th page first)."""
        async with httpx.AsyncClient(timeout=10.0) as http:
            while not self.stop.is_set():
                flagged = {r.run_id: r for o in self.orgs for r in o.runs if r.chaos and r.run_id}
                todo = [rid for rid in flagged if rid not in self.armed]
                if todo:
                    async with self.sm() as session:
                        rows = (await session.execute(text("select replace(id::text,'-','') from runs where replace(id::text,'-','') = any(:ids) "
                                                           "and status in ('running','waiting_approval','verifying')"), {"ids": todo})).scalars().all()
                    for rid in rows:
                        rec = flagged[rid]
                        self.armed.add(rid)
                        self.arm_count += 1
                        body = {flag: True for flag in rec.chaos}
                        resp = await http.post(f"{self.cfg.sim_url}/admin/tenants/{self.orgs[rec.org].tenant_id}/chaos", json=body,
                                               headers={"X-Admin-Token": self.stack.admin_token})
                        if resp.status_code != 200:
                            self.stats.record("POST", "/admin/tenants/{id}/chaos", resp.status_code, 0.0, resp.text)
                await asyncio.sleep(1.0)

    async def wait_until(self, spec: str) -> bool:
        """Wait for '50%' (share of the runs finished) or '40s' (seconds since the start). False if the test ended first."""
        if spec.endswith("%"):
            target = float(spec[:-1]) / 100 * self.cfg.runs
            while not self.stop.is_set() and self.finished_count < target:
                await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(max(0.0, float(spec.rstrip("s")) - (time.monotonic() - self.t0)))
        return not self.stop.is_set()

    async def killer(self) -> None:
        if not self.cfg.kill_worker_at or not await self.wait_until(self.cfg.kill_worker_at):
            return
        async with self.sm() as session:
            held = (await session.execute(text("select replace(id::text,'-',''), status from runs where lease_owner = 'lw-1' "
                                               "and status in ('running','waiting_approval','waiting_user','verifying')"))).all()
        proc = self.stack.procs["worker1"]
        self.killed = {"worker": "lw-1", "mode": self.cfg.kill_mode, "at_s": round(time.monotonic() - self.t0, 1), "pid": proc.pid,
                       "finished_before": self.finished_count, "in_flight": [r[0] for r in held], "statuses": dict(Counter(r[1] for r in held))}
        log(f"{'KILLING' if self.cfg.kill_mode == 'kill' else 'STOPPING'} worker lw-1 (pid {proc.pid}) with {len(held)} run(s) in flight, "
            f"{self.finished_count} runs finished so far")
        if self.cfg.kill_mode == "kill":
            kill_tree(proc.pid)
        else:
            await asyncio.to_thread(self.stack.stop, "worker1", 40.0)
        if self.cfg.respawn_after_s > 0:
            await asyncio.sleep(self.cfg.respawn_after_s)
            log("starting a replacement worker")
            self.stack.procs.pop("worker1", None)
            self.stack.start_worker(0)

    async def api_restarter(self) -> None:
        """Hard-restart the API: sessions and pending approvals live in the database, so the users carry on afterwards."""
        if not self.cfg.restart_api_at or not await self.wait_until(self.cfg.restart_api_at):
            return
        pending = 0
        for _ in range(60):  # pick a moment when somebody is actually waiting for a human
            async with self.sm() as session:
                pending = await session.scalar(text("select count(*) from approvals where status = 'pending'"))
            if pending or self.stop.is_set():
                break
            await asyncio.sleep(0.5)
        started = time.monotonic()
        log(f"RESTARTING the API (hard kill) with {pending} approval(s) pending")
        kill_tree(self.stack.procs["api"].pid)
        self.stack.procs.pop("api")
        self.stack.spawn("api", ["-m", "uvicorn", "worker.server:app", "--host", "127.0.0.1", "--port", str(self.cfg.api_port),
                                 "--no-access-log", "--log-level", "warning"])
        await self.stack.wait_http(f"{self.cfg.api_url}/api/health", "api")
        self.api_restart = {"at_s": round(started - self.t0, 1), "downtime_s": round(time.monotonic() - started, 1), "pending_approvals": int(pending or 0)}
        log(f"API back after {self.api_restart['downtime_s']}s")

    async def sampler(self) -> None:
        """Every 2 s: memory of each process tree (the browsers count), the workers' contexts, queue depth, connections, CPU."""
        known: dict[int, psutil.Process] = {}
        psutil.cpu_percent(None)
        async with httpx.AsyncClient(timeout=1.5) as http:
            while not self.stop.is_set():
                vm = psutil.virtual_memory()
                row: dict[str, Any] = {"t": round(time.monotonic() - self.t0, 1), "cpu": psutil.cpu_percent(None),
                                       "ram_used_mb": round((vm.total - vm.available) / 2**20)}
                for name, popen in list(self.stack.procs.items()):
                    if popen.poll() is None:
                        try:
                            proc = known.setdefault(popen.pid, psutil.Process(popen.pid))
                        except psutil.Error:
                            continue
                        row[name] = await asyncio.to_thread(self.tree_rss, proc)
                        if name.startswith("worker"):  # the Python process alone, to tell its growth from the browsers'
                            try:
                                row[f"{name}_py"] = round(proc.memory_info().rss / 2**20, 1)
                            except psutil.Error:
                                pass
                for index in range(self.cfg.workers):
                    name = f"worker{index + 1}"
                    try:
                        body = (await http.get(f"http://127.0.0.1:{self.cfg.metrics_port(index)}/metrics")).text
                    except httpx.HTTPError:
                        continue
                    self.last_metrics[name] = body
                    row[f"{name}_ctx"] = metric_value(body, "oneforall_browser_contexts_active")
                    row[f"{name}_runs"] = metric_value(body, "oneforall_runs_active")
                try:
                    async with self.sm() as session:
                        row["queued"], row["active"] = (await session.execute(text(
                            "select count(*) filter (where status='queued'), count(*) filter (where status in "
                            "('running','waiting_approval','waiting_user','verifying')) from runs"))).one()
                        row["max_org_active"] = await session.scalar(text(
                            "select coalesce(max(c), 0) from (select count(*) c from runs where status in "
                            "('running','waiting_approval','waiting_user','verifying') group by org_id) t"))
                        row["pg_conns"] = await session.scalar(text("select count(*) from pg_stat_activity where datname = current_database()"))
                except Exception:  # noqa: BLE001 - a missed sample is fine
                    pass
                self.samples.append(row)
                await asyncio.sleep(2.0)

    @staticmethod
    def tree_rss(proc: psutil.Process) -> float | None:
        """Resident memory of a process and all its descendants (the browsers), in MB; None when it is gone."""
        try:
            total = proc.memory_info().rss
            for child in proc.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except psutil.Error:
                    pass
            return round(total / 2**20, 1)
        except psutil.Error:
            return None

    async def db_counters(self) -> dict:
        async with self.sm() as session:
            row = (await session.execute(text("select xact_commit, xact_rollback, deadlocks, temp_files, conflicts from pg_stat_database "
                                              "where datname = current_database()"))).one()
        return dict(row._mapping)

    # ------------------------------------------------------------------ running

    async def finished_runs(self) -> tuple[int, int]:
        async with self.sm() as session:
            row = (await session.execute(text("select count(*) filter (where status = any(:final)), count(*) from runs"),
                                         {"final": list(FINAL)})).one()
        return row[0], row[1]

    async def run(self) -> None:
        cfg = self.cfg
        self.t0 = time.monotonic()
        self.started_at = datetime.now(timezone.utc)
        for org in self.orgs:
            for client in org.clients["approver"]:
                self.tasks.append(asyncio.create_task(self.approver_loop(org, client), name=f"approver-{client.email}"))
                self.tasks.append(asyncio.create_task(self.prober_loop(client), name=f"prober-{client.email}"))
            for client in org.clients["operator"]:
                self.tasks.append(asyncio.create_task(self.prober_loop(client), name=f"prober-{client.email}"))
            for client in org.clients["viewer"]:
                self.tasks.append(asyncio.create_task(self.viewer_loop(org, client), name=f"viewer-{client.email}"))
        for coro in (self.sampler(), self.chaos_armer(), self.killer(), self.api_restarter()):
            self.tasks.append(asyncio.create_task(coro))
        submit = asyncio.create_task(self.submit_all())
        self.tasks.append(submit)
        deadline = self.t0 + cfg.timeout_s
        last_report = 0.0
        while time.monotonic() < deadline:
            done, total = await self.finished_runs()
            self.finished_count = done
            if time.monotonic() - last_report > 10:
                last_report = time.monotonic()
                sample = self.samples[-1] if self.samples else {}
                log(f"submitted {sum(1 for o in self.orgs for r in o.runs if r.run_id)}/{cfg.runs}, finished {done}, queued {sample.get('queued', '?')}, "
                    f"active {sample.get('active', '?')}, cpu {sample.get('cpu', '?')}%")
            if submit.done() and done >= sum(1 for o in self.orgs for r in o.runs if r.run_id) and done >= 1:
                break
            await asyncio.sleep(1.0)
        else:
            log(f"TIMEOUT after {cfg.timeout_s:.0f}s: not every run finished")
        self.duration_s = time.monotonic() - self.t0
        await asyncio.sleep(3.0)  # let the last WebSocket frames and approvals settle
        self.stop.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    # ------------------------------------------------------------------ results

    async def collect(self) -> dict:
        cfg = self.cfg
        async with self.sm() as session:
            runs = (await session.execute(text("""
                select replace(r.id::text,'-','') id, r.org_id::text org_id, r.status, r.queued_at, r.started_at, r.finished_at, r.attempts,
                       r.resume_count, r.final_report, r.lease_owner,
                       extract(epoch from (r.finished_at - r.queued_at)) e2e, extract(epoch from (r.finished_at - r.started_at)) exec,
                       extract(epoch from (r.started_at - r.queued_at)) qwait
                from runs r order by r.queued_at"""))).mappings().all()
            approvals = (await session.execute(text("""
                select id::text, replace(run_id::text,'-','') run_id, kind, status,
                       extract(epoch from (responded_at - requested_at)) waited from approvals"""))).mappings().all()
            events = dict((await session.execute(text("select type, count(*) from run_events where type in "
                                                      "('run_requeued','run_resumed','memory_saved','memory_rejected','approval_requested') group by type"))).all())
            event_total = await session.scalar(text("select count(*) from run_events"))
            usage = (await session.execute(text("select coalesce(sum(calls),0), coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0) from usage_events"))).one()
            per10 = (await session.execute(text("select max(c) from (select sum(calls) c from usage_events group by floor(extract(epoch from ts)/10)) t"))).scalar()
            memories = await session.scalar(text("select count(*) from org_memory"))
        by_run = {r["id"]: r for r in runs}
        status_counts = Counter(r["status"] for r in runs)
        submitted = [r for o in self.orgs for r in o.runs]
        accepted = [r for r in submitted if r.run_id]
        truth = await self.ground_truth(by_run)
        retried = Counter(a["run_id"] for a in approvals if a["kind"] == "approval")
        e2e = [float(r["e2e"]) for r in runs if r["e2e"] is not None]
        first_submit = min((r.submitted_at for r in submitted), default=self.t0)
        finish_times = [r["finished_at"] for r in runs if r["finished_at"]]
        start_ts = min((r["queued_at"] for r in runs), default=None)
        window = (max(finish_times) - start_ts).total_seconds() if finish_times and start_ts else self.duration_s
        succeeded = status_counts.get("succeeded", 0)
        approved_waits = [float(a["waited"]) for a in approvals if a["kind"] == "approval" and a["status"] == "approved" and a["waited"] is not None]
        delays = [self.approval_delays[a["id"]] for a in approvals if a["id"] in self.approval_delays and a["status"] == "approved" and a["waited"] is not None]
        overheads = [float(a["waited"]) - self.approval_delays[a["id"]] for a in approvals
                     if a["id"] in self.approval_delays and a["status"] == "approved" and a["waited"] is not None]
        recovery = self.recovery(by_run, events)
        result = {
            "profile": cfg.profile, "started_at": self.started_at.isoformat(timespec="seconds"), "duration_s": round(self.duration_s, 1),
            "config": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(cfg).items() if k not in ("out", "db_name", "keep_db")},
            "host": host_info(),
            "runs": {
                "submitted": len(submitted), "accepted_by_api": len(accepted), "rejected_at_submit": len(submitted) - len(accepted),
                "succeeded": succeeded, "failed": status_counts.get("failed", 0), "needs_attention": status_counts.get("needs_attention", 0),
                "cancelled": status_counts.get("cancelled", 0),
                "unfinished": sum(n for s, n in status_counts.items() if s not in FINAL),
                "success_rate": round(succeeded / max(len(submitted), 1), 3), **truth["summary"],
            },
            "latency_s": {"end_to_end": summary(e2e), "execution": summary([float(r["exec"]) for r in runs if r["exec"] is not None]),
                          "queue_wait": summary([float(r["qwait"]) for r in runs if r["qwait"] is not None])},
            "throughput": {"window_s": round(window, 1), "runs_per_min_finished": round(sum(status_counts.get(s, 0) for s in FINAL) * 60 / max(window, 1), 2),
                           "runs_per_min_succeeded": round(succeeded * 60 / max(window, 1), 2),
                           "submit_span_s": round(max((r.submitted_at for r in submitted), default=first_submit) - first_submit, 1)},
            "approvals": {"count": len(approved_waits), "wait_s": summary(approved_waits), "human_delay_s": summary(delays),
                          "overhead_s": summary(overheads), "notice_lag_s": summary(self.notice_lags),
                          "runs_with_more_than_one_approval": sum(1 for n in retried.values() if n > 1),
                          "not_approved": Counter(a["status"] for a in approvals if a["status"] != "approved")},
            "llm": {"calls": int(usage[0]), "input_tokens": int(usage[1]), "output_tokens": int(usage[2]), "limiter_rate_per_s": cfg.llm_rate,
                    "peak_calls_per_s_10s_window": round(float(per10 or 0) / 10, 1), "think_ms": cfg.think_ms,
                    "avg_calls_per_run": round(int(usage[0]) / max(succeeded, 1), 1), **self.llm_metrics()},
            "memory": self.memory_summary(), "workers": self.worker_metrics(),
            "queue": {"peak_depth": max((s.get("queued", 0) for s in self.samples), default=0),
                      "peak_active_runs": max((s.get("active", 0) for s in self.samples), default=0),
                      "max_active_runs_of_one_org": max((s.get("max_org_active", 0) for s in self.samples), default=0),
                      "org_concurrency_cap": cfg.org_concurrency,
                      "samples_over_the_org_cap": sum(1 for s in self.samples if s.get("max_org_active", 0) > cfg.org_concurrency)},
            "recovery": recovery, "chaos": {"runs_flagged": sum(1 for r in submitted if r.chaos), "arms": self.arm_count,
                                            "runs_with_retried_save": sum(1 for r in submitted if r.run_id and retried.get(r.run_id, 0) > 1)},
            "errors": {"run_outcomes": truth["reasons"], "client_http": dict(self.stats.errors), "client_http_samples": self.stats.error_samples,
                       "log_lines": self.log_errors()},
            "isolation": {"responses_checked": self.guard.checked, "violations": len(self.guard.violations), "violation_samples": self.guard.violations[:10],
                          "cross_org_probes": self.guard.probes, "probe_failures": len(self.guard.probe_failures), "probe_failure_samples": self.guard.probe_failures[:10],
                          "stray_bills": truth["stray_bills"]},
            "websockets": {**dict(self.ws_stats), "problem_samples": self.ws_problems[:10]},
            "memory_org": {"notes_stored": int(memories or 0), "event_rows": int(event_total or 0), "events_per_run": round(int(event_total or 0) / max(len(runs), 1), 1)},
            "db": await self.db_summary(), "api": self.stats.table(),
            "series": [{k: v for k, v in row.items() if k != "pg_conns"} for row in self.samples[::5]],  # every 10 s
        }
        return result

    def worker_metrics(self) -> dict:
        """Counters from the workers' last successful /metrics scrape (a killed worker contributes its last scrape)."""
        bodies = list(self.last_metrics.values())
        total = lambda name, **labels: sum(metric_sum(b, name, **labels) for b in bodies)  # noqa: E731
        return {"scraped": len(bodies), "leases_lost": int(total("oneforall_worker_leases_lost_total")),
                "requeued_worker_lost": int(total("oneforall_runs_requeued_total", reason="worker_lost")),
                "requeued_shutdown": int(total("oneforall_runs_requeued_total", reason="shutdown")),
                "resumed": int(total("oneforall_runs_resumed_total")), "browser_relaunches": int(total("oneforall_browser_relaunches_total")),
                "tool_calls": int(total("oneforall_tool_calls_total")), "tool_calls_failed": int(total("oneforall_tool_calls_total", ok="false"))}

    def llm_metrics(self) -> dict:
        buckets: dict[float, float] = defaultdict(float)
        total = count = 0.0
        for body in self.last_metrics.values():
            for le, value in metric_buckets(body, "oneforall_llm_ratelimit_wait_seconds"):
                buckets[le] += value
            total += metric_value(body, "oneforall_llm_ratelimit_wait_seconds_sum")
            count += metric_value(body, "oneforall_llm_ratelimit_wait_seconds_count")
        return {"limiter_wait_s": {"n": int(count), "mean": round(total / count, 3) if count else None,
                                   "p50": rounded(histogram_percentile(list(buckets.items()), 50), 3),
                                   "p95": rounded(histogram_percentile(list(buckets.items()), 95), 3)}}

    async def ground_truth(self, by_run: dict[str, Any]) -> dict:
        """Compare each org's ERP with what its runs asked for. A succeeded run is correct only if exactly one faithful bill exists."""
        passed = false_success = duplicates = 0
        stray_total = 0
        reasons: Counter[str] = Counter()
        org_report = []
        for org in self.orgs:
            state = await self.sim_state(org)
            state["baseline_bills"] = org.baseline_bills
            baseline_ids = {b["id"] for b in org.baseline_bills}
            invoices = {i["number"]: i for i in state["invoices"]}
            targeted = {r.invoice for r in org.runs}
            created = [b for b in state["bills"] if b["id"] not in baseline_ids]
            stray = [b for b in created if b["invoice_number"] not in targeted]
            stray_total += len(stray)
            dup_invoices = [n for n in targeted if len(bills_for(state, n)) > 1]
            duplicates += len(dup_invoices)
            for rec in org.runs:
                row = by_run.get(rec.run_id)
                if row is None:
                    continue
                matching = bills_for(state, rec.invoice)
                faithful = len(matching) == 1 and not bill_mismatches(matching[0], invoices[rec.invoice])
                if row["status"] == "succeeded":
                    if faithful:
                        passed += 1
                    else:
                        false_success += 1
                else:
                    report = row["final_report"] or {}
                    reasons[f"{row['status']}: {str(report.get('summary', ''))[:110]}"] += 1
            org_report.append({"org": org.slug, "bills_created": len(created), "targeted_invoices": len(targeted), "stray_bills": len(stray),
                               "invoices_with_duplicate_bills": len(dup_invoices)})
        accepted = sum(1 for o in self.orgs for r in o.runs if r.run_id)
        return {"summary": {"ground_truth_pass": passed, "ground_truth_pass_rate": round(passed / max(accepted, 1), 3), "false_success": false_success,
                            "invoices_with_duplicate_bills": duplicates, "per_org": org_report},
                "reasons": dict(reasons), "stray_bills": stray_total}

    def recovery(self, by_run: dict[str, Any], events: dict) -> dict:
        info: dict[str, Any] = {"requeue_events": int(events.get("run_requeued", 0)), "resume_events": int(events.get("run_resumed", 0)),
                                "runs_resumed": sum(1 for r in by_run.values() if r["resume_count"]),
                                "runs_with_more_than_one_attempt": sum(1 for r in by_run.values() if r["attempts"] > 1)}
        if self.killed:
            flight = self.killed["in_flight"]
            final = Counter((by_run[i]["status"] if i in by_run else "unknown") for i in flight)
            info["killed"] = {**{k: v for k, v in self.killed.items() if k != "in_flight"}, "in_flight_runs": len(flight), "their_final_status": dict(final),
                              "all_in_flight_runs_completed": all(by_run[i]["status"] in FINAL for i in flight if i in by_run)}
        if self.api_restart:
            info["api_restart"] = self.api_restart
        return info

    def memory_summary(self) -> dict:
        out: dict[str, Any] = {}
        for name in ("api", "sim", *(f"worker{i + 1}" for i in range(self.cfg.workers))):
            values = [s[name] for s in self.samples if s.get(name) is not None]
            if values:
                out[name] = {"peak_mb": round(max(values)), "avg_mb": round(sum(values) / len(values)), "min_mb": round(min(values))}
        for i in range(self.cfg.workers):
            name = f"worker{i + 1}"
            pairs = [(s[f"{name}_ctx"], s[name]) for s in self.samples if s.get(name) is not None and f"{name}_ctx" in s]
            if len(pairs) < 5 or name not in out:
                continue
            out[name]["peak_contexts"] = int(max(c for c, _ in pairs))
            slope = regression_slope(pairs)
            if slope is not None:
                out[name]["mb_per_active_context_regression"] = round(slope)
            drift = leak_slope([(row["t"], row[f"{name}_ctx"], row[name]) for row in self.samples
                                if row.get(name) is not None and f"{name}_ctx" in row])
            if drift is not None:
                out[name]["drift_mb_per_min_at_equal_contexts"] = round(drift, 1)
            python_only = leak_slope([(row["t"], row[f"{name}_ctx"], row[f"{name}_py"]) for row in self.samples
                                      if f"{name}_py" in row and f"{name}_ctx" in row])
            if python_only is not None:
                out[name]["python_process_drift_mb_per_min"] = round(python_only, 1)
            half = self.duration_s / 2
            late = leak_slope([(row["t"], row[f"{name}_ctx"], row[name]) for row in self.samples
                               if row["t"] > half and row.get(name) is not None and f"{name}_ctx" in row])
            if late is not None:  # warm-up (caches, the first browser start) is over by then: a leak would still show
                out[name]["drift_second_half_mb_per_min"] = round(late, 1)
            idle = [m for c, m in pairs if c == 0]
            if idle:  # memory above the idle (browser started, no run) level, per open context
                out[name]["mb_per_active_context_over_idle"] = round(sum((m - min(idle)) / c for c, m in pairs if c > 0) / max(sum(1 for c, _ in pairs if c > 0), 1))
        out["system"] = {"cpu_avg_pct": round(sum(s["cpu"] for s in self.samples) / max(len(self.samples), 1), 1),
                         "cpu_peak_pct": max((s["cpu"] for s in self.samples), default=0), "ram_used_peak_mb": max((s["ram_used_mb"] for s in self.samples), default=0)}
        return out

    async def db_summary(self) -> dict:
        after = await self.db_counters()
        before = self.db_before
        return {"max_connections_seen": max((s.get("pg_conns", 0) for s in self.samples), default=0),
                "deadlocks": after["deadlocks"] - before.get("deadlocks", 0),
                "transactions_per_s": round((after["xact_commit"] - before.get("xact_commit", 0)) / max(self.duration_s, 1), 1),
                "rollbacks": after["xact_rollback"] - before.get("xact_rollback", 0)}

    def log_errors(self) -> dict:
        """ERROR and WARNING lines of the stack's own logs (JSON lines), grouped by logger and message."""
        found: Counter[str] = Counter()
        for path in sorted((self.stack.tmp / "logs").glob("*.log")):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.startswith("{"):
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("level") in ("ERROR", "WARNING", "CRITICAL"):
                    message = re.sub(r"[0-9a-f]{8,}|\d+", "N", str(entry.get("msg", "")))[:100]
                    found[f"{path.stem} {entry['level']} {entry.get('logger')}: {message}"] += 1
        return dict(found.most_common(15))


# --------------------------------------------------------------------------- Prometheus text and helpers


def metric_value(body: str, name: str) -> float:
    match = re.search(rf"^{re.escape(name)}(?:\{{[^}}]*\}})? ([0-9.eE+-]+)$", body, re.M)
    return float(match.group(1)) if match else 0.0


def metric_sum(body: str, name: str, **labels: str) -> float:
    """Sum of every series of ``name`` whose labels include ``labels``."""
    total = 0.0
    for raw_labels, value in re.findall(rf"^{re.escape(name)}(\{{[^}}]*\}})? ([0-9.eE+-]+)$", body, re.M):
        if all(f'{k}="{v}"' in raw_labels for k, v in labels.items()):
            total += float(value)
    return total


def metric_buckets(body: str, name: str) -> list[tuple[float, float]]:
    pairs = []
    for le, value in re.findall(rf'^{re.escape(name)}_bucket\{{le="([^"]+)"\}} ([0-9.eE+-]+)$', body, re.M):
        pairs.append((float("inf") if le == "+Inf" else float(le), float(value)))
    return pairs


def rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def regression_slope(pairs: list[tuple[float, float]]) -> float | None:
    n = len(pairs)
    mean_x, mean_y = sum(x for x, _ in pairs) / n, sum(y for _, y in pairs) / n
    var = sum((x - mean_x) ** 2 for x, _ in pairs)
    return None if var == 0 else sum((x - mean_x) * (y - mean_y) for x, y in pairs) / var


def leak_slope(rows: list[tuple[float, float, float]]) -> float | None:
    """Least-squares fit of rss = a + b*contexts + c*minutes; returns c, the growth of a worker's memory over time with the
    number of open browser contexts held constant (a leak shows up here, load does not)."""
    if len(rows) < 12:
        return None
    X = [[1.0, ctx, t / 60.0] for t, ctx, _ in rows]
    y = [rss for _, _, rss in rows]
    xtx = [[sum(r[i] * r[j] for r in X) for j in range(3)] for i in range(3)]
    xty = [sum(r[i] * v for r, v in zip(X, y)) for i in range(3)]
    for col in range(3):  # Gaussian elimination with partial pivoting
        pivot = max(range(col, 3), key=lambda r: abs(xtx[r][col]))
        if abs(xtx[pivot][col]) < 1e-9:
            return None
        xtx[col], xtx[pivot], xty[col], xty[pivot] = xtx[pivot], xtx[col], xty[pivot], xty[col]
        for r in range(col + 1, 3):
            factor = xtx[r][col] / xtx[col][col]
            xtx[r] = [a - factor * b for a, b in zip(xtx[r], xtx[col])]
            xty[r] -= factor * xty[col]
    beta = [0.0] * 3
    for r in (2, 1, 0):
        beta[r] = (xty[r] - sum(xtx[r][c] * beta[c] for c in range(r + 1, 3))) / xtx[r][r]
    return beta[2]


def host_info() -> dict:
    vm = psutil.virtual_memory()
    return {"os": platform.platform(), "cpu": platform.processor(), "logical_cores": psutil.cpu_count(), "physical_cores": psutil.cpu_count(logical=False),
            "ram_gb": round(vm.total / 2**30, 1), "ram_available_gb_at_start": round(vm.available / 2**30, 1), "python": platform.python_version()}


# --------------------------------------------------------------------------- output


def console_table(r: dict) -> str:
    runs, lat, thr, app, llm = r["runs"], r["latency_s"], r["throughput"], r["approvals"], r["llm"]

    def ps(block: dict) -> str:
        if not block.get("n"):
            return "n/a"
        return f"p50 {block['p50']}  p95 {block['p95']}" + (f"  max {block['max']}" if "max" in block else "")
    lines = [
        "", "=" * 78, f"LOAD TEST '{r['profile']}'  ({r['duration_s']}s)  orgs={r['config']['orgs']} runs={r['config']['runs']} workers={r['config']['workers']}x{r['config']['concurrency']}",
        "=" * 78,
        f"runs            submitted {runs['submitted']}  succeeded {runs['succeeded']}  failed {runs['failed']}  needs_attention {runs['needs_attention']}  unfinished {runs['unfinished']}",
        f"ground truth    pass {runs['ground_truth_pass']}/{runs['accepted_by_api']} ({runs['ground_truth_pass_rate']:.0%})  false success {runs['false_success']}  duplicate bills {runs['invoices_with_duplicate_bills']}  stray bills {r['isolation']['stray_bills']}",
        f"end-to-end (s)  {ps(lat['end_to_end'])}", f"execution (s)    {ps(lat['execution'])}", f"queue wait (s)  {ps(lat['queue_wait'])}",
        f"approval wait   {ps(app['wait_s'])}   (human delay {ps(app['human_delay_s'])}, overhead {ps(app['overhead_s'])})",
        f"throughput      {thr['runs_per_min_finished']} runs/min finished, {thr['runs_per_min_succeeded']} succeeded  (window {thr['window_s']}s)",
        f"LLM             {llm['calls']} calls, {llm['input_tokens']} in / {llm['output_tokens']} out tokens, peak {llm['peak_calls_per_s_10s_window']}/s of {llm['limiter_rate_per_s']}/s; limiter wait {ps(llm['limiter_wait_s']) if llm['limiter_wait_s'].get('p50') is not None else 'n/a'}",
    ]
    for name, mem in r["memory"].items():
        if name == "system":
            lines.append(f"system          cpu avg {mem['cpu_avg_pct']}% peak {mem['cpu_peak_pct']}%, RAM used peak {mem['ram_used_peak_mb']} MB")
        else:
            extra = (f", {mem['mb_per_active_context_regression']} MB/context (fit), peak {mem['peak_contexts']} ctx"
                     + (f", drift {mem['drift_mb_per_min_at_equal_contexts']} MB/min (2nd half {mem.get('drift_second_half_mb_per_min')}; python alone {mem.get('python_process_drift_mb_per_min')})" if "drift_mb_per_min_at_equal_contexts" in mem else "")) if "mb_per_active_context_regression" in mem else ""
            lines.append(f"memory {name:<8} peak {mem['peak_mb']} MB, avg {mem['avg_mb']} MB{extra}")
    q, w = r["queue"], r["workers"]
    lines.append(f"queue           peak depth {q['peak_depth']}, peak active {q['peak_active_runs']}, max active of one org {q['max_active_runs_of_one_org']} "
                 f"(cap {q['org_concurrency_cap']}, samples over cap {q['samples_over_the_org_cap']})")
    lines.append(f"workers         {w['tool_calls']} tool calls ({w['tool_calls_failed']} failed), leases lost {w['leases_lost']}, browser relaunches {w['browser_relaunches']}")
    rec = r["recovery"]
    lines.append(f"recovery        requeues {rec['requeue_events']}, resumes {rec['resume_events']}, runs resumed {rec['runs_resumed']}" +
                 (f"; {rec['killed'].get('mode', 'kill')} {rec['killed']['worker']} with {rec['killed']['in_flight_runs']} in flight -> {rec['killed']['their_final_status']}" if "killed" in rec else "") +
                 (f"; API restart {rec['api_restart']['downtime_s']}s with {rec['api_restart']['pending_approvals']} approvals pending" if "api_restart" in rec else ""))
    iso = r["isolation"]
    lines.append(f"isolation       {iso['responses_checked']} responses scanned, {iso['violations']} leaks; {iso['cross_org_probes']} cross-org probes, {iso['probe_failures']} not refused")
    lines.append(f"websockets      {r['websockets'].get('opened', 0)} streams, {r['websockets'].get('events', 0)} events, order violations {r['websockets'].get('order_violations', 0)}, errors {r['websockets'].get('errors', 0)}")
    lines.append(f"database        max {r['db']['max_connections_seen']} connections, {r['db']['transactions_per_s']} tx/s, deadlocks {r['db']['deadlocks']}")
    if r["errors"]["run_outcomes"]:
        lines.append("run problems    " + "; ".join(f"{n}x {reason}" for reason, n in list(r["errors"]["run_outcomes"].items())[:4]))
    if r["errors"]["client_http"]:
        lines.append("http errors     " + "; ".join(f"{n}x {k}" for k, n in list(r["errors"]["client_http"].items())[:5]))
    lines.append("=" * 78)
    return "\n".join(lines)


def markdown(results: dict) -> str:
    out = ["# Load test results", "", "Generated by `scripts/loadtest.py` (see `docs/SCALING.md`). Numbers are from real runs on one machine with a policy LLM (no model quota).", ""]
    for name, r in results["profiles"].items():
        runs, lat, thr, app, llm, c = r["runs"], r["latency_s"], r["throughput"], r["approvals"], r["llm"], r["config"]
        out += [f"## Profile `{name}`", "",
                f"{c['orgs']} orgs x {c['users']} users, {c['runs']} runs, {c['workers']} workers x {c['concurrency']} runs, chaos rate {c['chaos_rate']}, "
                f"think {c['think_ms']:.0f} ms, LLM limit {c['llm_rate']}/s, org concurrency {c['org_concurrency']}"
                + (f", worker kill at {c['kill_worker_at']}" if c.get("kill_worker_at") else "") + f". Started {r['started_at']}, took {r['duration_s']} s.", "",
                "| Metric | Value |", "|---|---|",
                f"| Succeeded / submitted | {runs['succeeded']} / {runs['submitted']} (failed {runs['failed']}, needs attention {runs['needs_attention']}) |",
                f"| Ground-truth pass rate | {runs['ground_truth_pass_rate']:.0%} ({runs['ground_truth_pass']} of {runs['accepted_by_api']}), false successes {runs['false_success']}, duplicate bills {runs['invoices_with_duplicate_bills']} |",
                f"| End-to-end latency p50 / p95 / max | {lat['end_to_end'].get('p50')} / {lat['end_to_end'].get('p95')} / {lat['end_to_end'].get('max')} s |",
                f"| Queue wait p50 / p95 | {lat['queue_wait'].get('p50')} / {lat['queue_wait'].get('p95')} s |",
                f"| Approval wait p50 / p95 | {app['wait_s'].get('p50')} / {app['wait_s'].get('p95')} s (human delay p50 {app['human_delay_s'].get('p50')} s) |",
                f"| Throughput | {thr['runs_per_min_finished']} runs/min finished ({thr['runs_per_min_succeeded']} succeeded) |",
                f"| LLM calls | {llm['calls']} ({llm['avg_calls_per_run']} per run), peak {llm['peak_calls_per_s_10s_window']}/s, limiter wait p95 {llm['limiter_wait_s'].get('p95')} s |",
                f"| Isolation | {r['isolation']['responses_checked']} responses scanned: {r['isolation']['violations']} leaks; {r['isolation']['cross_org_probes']} probes: {r['isolation']['probe_failures']} not refused |",
                f"| Recovery | requeues {r['recovery']['requeue_events']}, resumes {r['recovery']['resume_events']}"
                + (f"; killed {r['recovery']['killed']['worker']} with {r['recovery']['killed']['in_flight_runs']} runs in flight" if "killed" in r["recovery"] else "") + " |",
                ""]
        mem = [f"{k}: peak {v['peak_mb']} MB" for k, v in r["memory"].items() if k != "system"]
        out += ["Memory (process tree incl. browsers): " + ", ".join(mem) + ".", ""]
    return "\n".join(out)


def write_results(cfg: Config, result: dict) -> None:
    results: dict[str, Any] = {"profiles": {}}
    if cfg.out.exists():
        try:
            results = json.loads(cfg.out.read_text(encoding="utf-8"))
            results.setdefault("profiles", {})
        except ValueError:
            pass
    results["profiles"][cfg.profile] = result
    cfg.out.parent.mkdir(parents=True, exist_ok=True)
    cfg.out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    cfg.out.with_suffix(".md").write_text(markdown(results), encoding="utf-8")
    log(f"results written to {cfg.out} and {cfg.out.with_suffix('.md')}")


# --------------------------------------------------------------------------- main


async def amain(cfg: Config) -> int:
    vm = psutil.virtual_memory()
    stack = Stack(cfg)
    log(f"database {mask_url(stack.db_url)}; machine: {psutil.cpu_count()} logical cores, "
        f"{vm.total / 2**30:.1f} GB RAM ({vm.available / 2**30:.1f} GB free)")
    test = LoadTest(cfg, stack)
    result: dict | None = None
    try:
        await stack.create_database()
        log(f"database {cfg.db_name} created and migrated; starting the stack")
        await test.setup()
        log(f"stack ready: {cfg.orgs} orgs, {cfg.orgs * cfg.users} users, {cfg.workers} workers x {cfg.concurrency}; submitting {cfg.runs} runs")
        await test.run()
        result = await test.collect()
        print(console_table(result))
        write_results(cfg, result)
    finally:
        test.stop.set()
        for task in test.tasks:
            task.cancel()
        await asyncio.gather(*test.tasks, return_exceptions=True)
        for org in test.orgs:
            for clients in org.clients.values():
                for client in clients:
                    await client.close()
        if test.engine is not None:
            await test.engine.dispose()
        await asyncio.to_thread(stack.kill_all)
        if cfg.keep_db:
            log(f"keeping database {cfg.db_name} (drop it with: python -c \"...\" or rerun without --keep-db)")
        else:
            try:
                await stack.drop_database()
            except Exception as err:  # noqa: BLE001
                log(f"could not drop {cfg.db_name}: {type(err).__name__}")
        if cfg.keep_db:
            log(f"temp dir kept: {stack.tmp}")
        else:
            shutil.rmtree(stack.tmp, ignore_errors=True)
    if result is None:
        return 1
    bad = result["isolation"]["violations"] or result["isolation"]["probe_failures"] or result["runs"]["false_success"] or result["runs"]["invoices_with_duplicate_bills"]
    return 2 if bad else 0


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    try:
        return asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        log("interrupted; everything that was started has been stopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
