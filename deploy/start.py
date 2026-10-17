"""Container entry point: a role-based supervisor for the simulated company, the API and the job workers.

Usage: ``python deploy/start.py``. The ``ROLE`` environment variable chooses what this container runs:

* ``all`` (default): the simulated company on 127.0.0.1:``SIM_PORT`` (internal), then, in multi-user mode (``DATABASE_URL``
  set), the optional migration (``MIGRATE_ON_START=1``) and the environment resync (``python -m worker.provisioning resync``),
  then the API on ``HOST``:``PORT`` (public), then ``WORKER_PROCESSES`` (default 1) job workers (``python -m worker.jobs``).
  Without a database it is the single-user shape: simulated company and API, no job workers.
* ``api``: the API only (the simulated company is somewhere else, ``SIM_BASE_URL`` points at it).
* ``worker``: job workers only (multi-user mode). A Cloud Run *service* must listen on ``$PORT``, so this role is for other
  hosts (a VM, Kubernetes, Cloud Run worker pools); the all-in-one Cloud Run service uses ``ROLE=all``.
* ``sim``: the simulated company alone, on ``HOST``:``PORT``.

Other environment: ``HOST`` (default 0.0.0.0), ``PORT`` (default 8080), ``SIM_HOST`` (default 127.0.0.1), ``SIM_PORT``
(default 8001). The API reaches the simulated company through ``SIM_BASE_URL`` (defaulted here to match SIM_PORT). With
``ROLE=all`` and no ``SIM_ADMIN_TOKEN`` a random one is generated and given to both, so the simulator's ``/admin`` API is
never open.

Supervision: if any process exits, the rest are stopped and this script exits 1, so the platform restarts the container.
On SIGTERM / SIGINT the job workers stop first (they hand their running runs back to the queue, so another worker, or this
container after its next start, resumes them from the last checkpoint), then the API, then the simulated company.
"""
from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from run import IS_WINDOWS, stop, wait_healthy  # noqa: E402  (reuse the local launcher's helpers)

ROLES = ("all", "api", "worker", "sim")
TRUE_VALUES = ("1", "true", "yes", "on")
# Seconds each group gets to stop gracefully. Cloud Run sends SIGKILL about 10 s after SIGTERM, so the budget is short and
# the job workers get most of it: they are the ones that have something to hand back.
GRACE_S = {"worker": 5.0, "api": 2.0, "sim": 1.0}
STOP_ORDER = ("worker", "api", "sim")


class ConfigError(ValueError):
    """The environment asks for something impossible (unknown role, workers without a database...)."""


@dataclass(frozen=True)
class Step:
    """One thing to do at start-up. ``service`` steps stay running (and are supervised); ``task`` steps must finish with 0.
    ``group`` (sim / api / worker) decides the order of the shutdown; ``health_url`` is waited for after a service starts."""

    name: str
    args: tuple[str, ...]
    kind: str = "service"
    group: str = ""
    health_url: str | None = None


@dataclass
class Plan:
    role: str
    multi_user: bool
    env: dict[str, str]  # environment of every child
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # one-line warnings to print


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in TRUE_VALUES


def plan(env: Mapping[str, str], multi_user: bool) -> Plan:
    """Decide what to start for ``env`` (pure: no processes, no I/O). ``multi_user`` = ``Settings.db_auth``."""
    role = (env.get("ROLE") or "all").strip().lower()
    if role not in ROLES:
        raise ConfigError(f"ROLE must be one of {', '.join(ROLES)}, not '{role}'")
    public_host = env.get("HOST") or "0.0.0.0"
    port = int(env.get("PORT") or "8080")
    sim_host = env.get("SIM_HOST") or "127.0.0.1"
    sim_port = int(env.get("SIM_PORT") or "8001")
    workers = int(env.get("WORKER_PROCESSES") or "1")
    if workers < 0:
        raise ConfigError("WORKER_PROCESSES must not be negative")
    check_host = "127.0.0.1" if public_host in ("0.0.0.0", "::") else public_host

    child = dict(env)
    child["ROLE"] = role
    child.setdefault("FORWARDED_ALLOW_IPS", "*")  # trust the platform's proxy headers (as an env var: click globs a "*" argument on Windows)
    notes: list[str] = []
    if role == "sim":
        child["SIM_HOST"], child["SIM_PORT"] = public_host, str(port)  # the simulator is the public listener
        sim_host, sim_port = public_host, port
    else:
        child["SIM_HOST"], child["SIM_PORT"] = sim_host, str(sim_port)
        child.setdefault("SIM_BASE_URL", f"http://127.0.0.1:{sim_port}")
    if role == "all" and not child.get("SIM_ADMIN_TOKEN", "").strip():
        child["SIM_ADMIN_TOKEN"] = secrets.token_urlsafe(32)  # sim and API share it; it never leaves this container
    elif role == "sim" and not child.get("SIM_ADMIN_TOKEN", "").strip():
        notes.append("SIM_ADMIN_TOKEN is not set: the simulator's /admin API is open to anyone who can reach this port")

    sim_step = Step("company_sim", ("-m", "company_sim"), group="sim",
                    health_url=f"http://{'127.0.0.1' if sim_host in ('0.0.0.0', '::') else sim_host}:{sim_port}/healthz")
    api_step = Step("api", ("-m", "uvicorn", "worker.server:app", "--host", public_host, "--port", str(port), "--proxy-headers",
                            "--log-level", "info"),
                    group="api", health_url=f"http://{check_host}:{port}/healthz")
    job_steps = [Step(f"job worker {n}", ("-m", "worker.jobs"), group="worker") for n in range(1, workers + 1)]

    steps: list[Step] = []
    if role == "sim":
        steps = [sim_step]
    elif role == "api":
        steps = [api_step]
    elif role == "worker":
        if not multi_user:
            raise ConfigError("ROLE=worker needs database mode: set DATABASE_URL")
        steps = job_steps
    else:
        steps = [sim_step]
        if multi_user:
            if _flag(env, "MIGRATE_ON_START"):
                steps.append(Step("migrate", ("-m", "worker.db", "migrate"), kind="task"))
            steps.append(Step("resync environments", ("-m", "worker.provisioning", "resync"), kind="task"))
        steps.append(api_step)
        if multi_user:
            steps += job_steps
    return Plan(role=role, multi_user=multi_user, env=child, steps=steps, notes=notes)


def spawn(name: str, args: tuple[str, ...], env: dict[str, str]) -> subprocess.Popen:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    print(f"[start] launching {name}: {' '.join(args)}", flush=True)
    return subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env, creationflags=flags)


def shutdown(running: list[tuple[Step, subprocess.Popen]]) -> None:
    """Stop the services group by group (job workers, then API, then simulator); a group's processes stop in parallel."""
    for group in STOP_ORDER:
        members = [(step, proc) for step, proc in running if step.group == group]
        threads = [threading.Thread(target=stop, args=(proc, step.name, GRACE_S[group])) for step, proc in members]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()


class Interrupted(Exception):
    """A stop signal arrived (raised from the signal handler, once)."""


def supervise(plan_: Plan) -> int:
    stopping = threading.Event()

    def _terminate(signum, frame):
        if not stopping.is_set():
            stopping.set()
            raise Interrupted

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _terminate)

    running: list[tuple[Step, subprocess.Popen]] = []
    code = 0
    try:
        for note in plan_.notes:
            print(f"[start] warning: {note}", flush=True)
        for step in plan_.steps:
            if step.kind == "task":
                print(f"[start] running {step.name}: {' '.join(step.args)}", flush=True)
                result = subprocess.run([sys.executable, *step.args], cwd=ROOT, env=plan_.env)
                if result.returncode != 0:
                    print(f"[start] {step.name} failed with code {result.returncode}; not starting", flush=True)
                    return 1
                continue
            proc = spawn(step.name, step.args, plan_.env)
            running.append((step, proc))
            if step.health_url:
                wait_healthy(step.health_url, step.name, proc)
                print(f"[start] {step.name} ready", flush=True)
        print(f"[start] role '{plan_.role}' is up: " + ", ".join(step.name for step, _ in running), flush=True)

        while True:
            for step, proc in running:
                if proc.poll() is not None:
                    print(f"[start] {step.name} exited with code {proc.returncode}; shutting down", flush=True)
                    return 1
            time.sleep(0.5)
    except Interrupted:
        print("[start] stop requested", flush=True)
        code = 0
    except RuntimeError as err:
        print(f"[start] {err}", flush=True)
        code = 1
    finally:
        stopping.set()  # a second signal during the shutdown is ignored
        shutdown(running)
        print("[start] stopped", flush=True)
    return code


def main() -> int:
    from worker.config import get_settings

    try:
        plan_ = plan(os.environ, multi_user=get_settings().db_auth)
    except ConfigError as err:
        print(f"[start] {err}", flush=True)
        return 2
    return supervise(plan_)


if __name__ == "__main__":
    sys.exit(main())
