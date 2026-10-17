# OneForAll Agent: one image, two shapes (host-agnostic: Cloud Run, Codespaces, any container host).
#
#   * Single-user demo (no DATABASE_URL): deploy/start.py runs the simulated company and the API on one public port.
#   * Multi-user (DATABASE_URL = PostgreSQL): the same command also runs the migration (MIGRATE_ON_START=1), re-creates the
#     orgs' simulated companies (the sim's data is ephemeral) and starts WORKER_PROCESSES job workers. ROLE chooses what a
#     container runs: all (default), api, worker or sim. See docs/DEPLOY.md for the Google Cloud Run setup.
#
# Base: Microsoft's official Playwright image. The tag MUST match the `playwright` Python package version, because the
# package drives the exact Chromium revision bundled in the image (here Playwright 1.63.0 on Ubuntu 24.04 "noble").
# To upgrade: change PLAYWRIGHT_VERSION and the tag together.
ARG PLAYWRIGHT_VERSION=1.63.0
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble
ARG PLAYWRIGHT_VERSION

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first (better layer caching), pinned by constraints.txt: the exact versions CI tests, playwright included
# (it must equal PLAYWRIGHT_VERSION, which the assertion below checks).
# --break-system-packages: the image uses the system Python, which is marked externally managed (PEP 668).
COPY requirements.txt constraints.txt /app/
RUN python3 -m pip install --break-system-packages -c constraints.txt -r requirements.txt \
 && python3 -c "import playwright, importlib.metadata as m; assert m.version('playwright') == '${PLAYWRIGHT_VERSION}', m.version('playwright')" \
 && python3 -m playwright install chromium

# Some hosts (and good practice) require a non-root user; we use uid 1000. The Playwright image already ships a user with that uid
# ("ubuntu" on noble); create one only if it is missing. All writable state lives under /data (ephemeral).
RUN (getent passwd 1000 >/dev/null || useradd -m -u 1000 user) \
 && mkdir -p /data/runs /data/sim /data/home \
 && chown -R 1000:1000 /data

COPY --chown=1000:1000 . /app
# The sim writes workspace/credentials.json next to the code, and the agent reads it from WORKSPACE_DIR: same folder.
RUN mkdir -p /app/workspace && chown 1000:1000 /app /app/workspace

ENV HOME=/data/home \
    ROLE=all \
    DEPLOY_MODE=1 \
    PUBLIC_SIM_PROXY=1 \
    HEADLESS=true \
    PORT=8080 \
    SIM_PORT=8001 \
    SIM_BASE_URL=http://127.0.0.1:8001 \
    RUNS_DIR=/data/runs \
    SIM_DATA_DIR=/data/sim \
    WORKSPACE_DIR=/app/workspace \
    MAX_RUNS_PER_DAY=15
# Secrets are NOT baked in. Single-user: GEMINI_API_KEY, ACCESS_PASSWORD, SESSION_SECRET, VAULT_KEY. Multi-user: GEMINI_API_KEY,
# DATABASE_URL, SESSION_SECRET, VAULT_KEY, METRICS_TOKEN. Set them in the host's secret settings (Cloud Run: Secret Manager).

USER 1000
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=4)"

CMD ["python3", "deploy/start.py"]
