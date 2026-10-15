"""Container entry point: the simulated company on 127.0.0.1:8001 (internal) and the worker on 0.0.0.0:$PORT (public).

Usage: ``python deploy/start.py``. Environment: ``PORT`` (default 8080), ``HOST`` (default 0.0.0.0), ``SIM_PORT``
(default 8001), ``SIM_HOST`` (default 127.0.0.1). The worker reaches the sim through ``SIM_BASE_URL`` (defaulted
here to match SIM_PORT). If either process dies the other is stopped and this script exits non-zero, so the platform
restarts the container. SIGTERM / SIGINT stop both cleanly.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from run import IS_WINDOWS, stop, wait_healthy  # noqa: E402  (reuse the local launcher's helpers)


def spawn(name: str, args: list[str], env: dict[str, str]) -> subprocess.Popen:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    print(f"[start] launching {name}: {' '.join(args)}", flush=True)
    return subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env, creationflags=flags)


def main() -> int:
    public_host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    sim_host = os.environ.get("SIM_HOST", "127.0.0.1")
    sim_port = int(os.environ.get("SIM_PORT", "8001"))
    env = {**os.environ, "SIM_HOST": sim_host, "SIM_PORT": str(sim_port)}
    env.setdefault("SIM_BASE_URL", f"http://127.0.0.1:{sim_port}")
    check_host = "127.0.0.1" if public_host in ("0.0.0.0", "::") else public_host

    def _terminate(signum, frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _terminate)

    procs: list[tuple[str, subprocess.Popen]] = []
    try:
        sim = spawn("simulated company", ["-m", "company_sim"], env)
        procs.append(("company_sim", sim))
        wait_healthy(f"http://127.0.0.1:{sim_port}/healthz", "company_sim", sim)
        print(f"[start] simulated company ready on {sim_host}:{sim_port} (internal)", flush=True)

        worker = spawn("worker", ["-m", "uvicorn", "worker.server:app", "--host", public_host, "--port", str(port),
                                  "--proxy-headers", "--forwarded-allow-ips", "*", "--log-level", "info"], env)
        procs.append(("worker", worker))
        wait_healthy(f"http://{check_host}:{port}/api/health", "worker", worker)
        print(f"[start] worker ready on {public_host}:{port} (public)", flush=True)

        while True:
            for name, proc in procs:
                if proc.poll() is not None:
                    print(f"[start] {name} exited with code {proc.returncode}; shutting down", flush=True)
                    return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("[start] stop requested", flush=True)
        return 0
    except RuntimeError as err:
        print(f"[start] {err}", flush=True)
        return 1
    finally:
        for name, proc in reversed(procs):
            stop(proc, name)
        print("[start] stopped", flush=True)


if __name__ == "__main__":
    sys.exit(main())
