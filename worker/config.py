"""Application settings, loaded from environment variables and an optional ``.env`` file."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration for the worker. Field names match the env var names (case-insensitive)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    LLM_PROVIDER: Literal["gemini", "ollama", "fake"] = "gemini"
    GEMINI_API_KEY: str = ""
    # Fallback chain, tried in order; a model whose free-tier daily quota is gone is skipped for the day.
    GEMINI_MODELS: str = "gemini-3.5-flash,gemini-3.6-flash,gemini-3.7-flash,gemini-2.5-flash,gemini-3.8-flash,gemini-3.5-flash-lite,gemini-2.5-flash-lite"
    # Backward compatible single-model setting: when set, it is tried first, ahead of GEMINI_MODELS.
    GEMINI_MODEL: str = ""
    OLLAMA_MODEL: str = "qwen2.5:3b"
    OLLAMA_URL: str = "http://localhost:11434"
    LLM_MIN_INTERVAL_S: float = 4.0
    # How long to keep retrying the model through a network outage (DNS/connection errors) before giving up.
    LLM_NETWORK_RETRY_S: float = 300.0

    # Environment
    SIM_BASE_URL: str = "http://localhost:8001"
    # Where the agent is told to start: a tenant's entry link (<sim>/t/<token>/), which selects that organization's
    # simulated company. Empty = SIM_BASE_URL (the default tenant). Set per run by evals and the platform.
    SIM_ENTRY_URL: str = ""
    SIM_ADMIN_TOKEN: str = ""  # sent as X-Admin-Token to the sim's /admin endpoints (the sim enforces it when its own SIM_ADMIN_TOKEN is set)
    WORKSPACE_DIR: str = "./workspace"
    RUNS_DIR: str = "./runs"
    HEADLESS: bool = True
    # One shared Chromium per process; each run gets an isolated context from it (see worker/tools/browser_pool.py).
    BROWSER_MAX_CONTEXTS: int = 8           # simultaneously open run contexts; further runs wait for a free slot
    BROWSER_ACQUIRE_TIMEOUT_S: float = 60.0  # how long a run waits for a slot before failing with a clear error

    # Agent policy and guards
    APPROVAL_MODE: Literal["auto", "confirm_writes", "confirm_all"] = "confirm_writes"
    MAX_STEPS: int = 40
    MAX_CONSECUTIVE_FAILURES: int = 4
    RUN_TIMEOUT_S: int = 900

    # Deployment (all opt-in; see README "Deploying a live demo"). Off by default, so local dev is unchanged.
    ACCESS_PASSWORD: str = ""       # set -> every route needs a login (signed session cookie)
    SESSION_SECRET: str = ""        # signs the cookie; a random one is generated per process when empty
    DEPLOY_MODE: bool = False       # server mode: always headless, the dashboard hides the "Headed browser" switch
    PUBLIC_SIM_PROXY: bool = False  # expose /mail /portal /erp /company through this server (single public port)
    MAX_RUNS_PER_DAY: int = 0       # runs per UTC day, 0 = unlimited

    # Credential vault (see worker/vault.py). Without VAULT_KEY a dev key is generated into VAULT_DIR/.vault_key.
    VAULT_KEY: str = ""             # Fernet key (urlsafe base64, 32 bytes); required when DEPLOY_MODE is on
    VAULT_DIR: str = "./.vault"     # where the file-backed vault keeps vault.json

    # PostgreSQL (see worker/db/). Empty = v1 behaviour: file-backed runs and vault, no database.
    DATABASE_URL: str = ""          # postgresql+asyncpg://user:password@host:5432/centralign (never logged unmasked)
    DB_POOL_SIZE: int = 10          # connections kept open per process
    DB_MAX_OVERFLOW: int = 10       # extra connections allowed under load
    DB_POOL_TIMEOUT_S: float = 30.0  # how long a request waits for a free connection

    # Multi-user mode (see worker/auth.py). "auto" = "db" when DATABASE_URL is set, else "file" (the v1 single shared
    # password / no auth). Set "file" to ignore DATABASE_URL for sign-in, "db" to insist on it.
    AUTH_MODE: Literal["auto", "file", "db"] = "auto"
    SESSION_IDLE_TTL_HOURS: float = 12.0     # a session ends after this long without use (sliding)
    SESSION_ABSOLUTE_TTL_DAYS: float = 7.0   # ... and at the latest this long after sign-in
    LOGIN_WINDOW_S: int = 900                # failed-login counting window (fixed buckets)
    LOGIN_MAX_FAILURES_PER_EMAIL: int = 5    # failures per e-mail address and window before sign-in is blocked
    LOGIN_MAX_FAILURES_PER_IP: int = 20      # failures per client address and window before sign-in is blocked
    LOGIN_BACKOFF_BASE_S: float = 0.5        # a failed attempt is answered after base * 2^(failures-1) s (max 8 s)

    # Tools
    HTTP_ALLOWED_HOSTS: str = "localhost,127.0.0.1"

    @field_validator("GEMINI_API_KEY", mode="before")
    @classmethod
    def _strip_key(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @property
    def db_auth(self) -> bool:
        """Are users, sessions and orgs in PostgreSQL (``AUTH_MODE=db``, or ``auto`` with ``DATABASE_URL`` set)?"""
        return self.AUTH_MODE == "db" or (self.AUTH_MODE == "auto" and bool(self.DATABASE_URL.strip()))

    @property
    def sim_admin_headers(self) -> dict[str, str]:
        """Headers for the simulated company's /admin endpoints (empty when no admin token is configured)."""
        return {"X-Admin-Token": self.SIM_ADMIN_TOKEN} if self.SIM_ADMIN_TOKEN else {}

    @property
    def gemini_models(self) -> list[str]:
        """The Gemini fallback chain: GEMINI_MODEL (if set) first, then GEMINI_MODELS, without duplicates."""
        names = [self.GEMINI_MODEL, *self.GEMINI_MODELS.split(",")]
        chain: list[str] = []
        for name in (n.strip() for n in names):
            if name and name not in chain:
                chain.append(name)
        return chain

    @property
    def http_allowed_hosts(self) -> set[str]:
        """HTTP_ALLOWED_HOSTS parsed into a lowercase set of hostnames."""
        return {h.strip().lower() for h in self.HTTP_ALLOWED_HOSTS.split(",") if h.strip()}


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
