# CentrAlign Worker: live demo image (single public port, host-agnostic: Cloud Run, Codespaces, any container host).
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

# Dependencies first (better layer caching). playwright is pinned through a constraint so requirements.txt can stay loose.
# --break-system-packages: the image uses the system Python, which is marked externally managed (PEP 668).
COPY requirements.txt /app/requirements.txt
RUN echo "playwright==${PLAYWRIGHT_VERSION}" > /tmp/constraints.txt \
 && python3 -m pip install --break-system-packages -c /tmp/constraints.txt -r requirements.txt \
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
# Secrets are NOT baked in: set GEMINI_API_KEY, ACCESS_PASSWORD and SESSION_SECRET in the host's secret settings.

USER 1000
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8080'), timeout=4)"

CMD ["python3", "deploy/start.py"]
