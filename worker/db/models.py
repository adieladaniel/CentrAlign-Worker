"""SQLAlchemy 2.0 models: the whole v2 schema (identity, runs, approvals, memory, vault, usage, limits, audit).

One module, one ``Base.metadata``; Alembic autogenerates migrations from it. Conventions:

* primary keys are ``uuid4`` UUIDs (append-only event tables use a bigint identity instead);
* every timestamp is ``timestamptz`` with a server default, so any process may insert without a clock of its own;
* flexible structure is JSONB; closed vocabularies are ``text`` plus a CHECK constraint (easier to extend than a
  PostgreSQL enum type);
* every tenant-owned table carries ``org_id`` (directly, or through its run) and is read through an org scope.
"""
from __future__ import annotations

import enum
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Computed, DateTime, Float, ForeignKey, Identity, Index, Integer, LargeBinary,
    MetaData, String, Text, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Role(str, enum.Enum):
    """Membership roles, most to least privileged. ``operator`` starts runs, ``approver`` answers approvals."""

    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    APPROVER = "approver"
    VIEWER = "viewer"


ROLES = tuple(r.value for r in Role)
# The statuses of RunState plus ``queued`` (created, waiting for a worker; see the job queue).
RUN_STATUSES = ("pending", "queued", "running", "waiting_approval", "waiting_user", "verifying",
                "succeeded", "failed", "needs_attention", "cancelled")
APPROVAL_KINDS = ("approval", "question")
APPROVAL_STATUSES = ("pending", "approved", "denied", "answered", "expired", "cancelled")

# Defaults of ``orgs.settings`` (any key may be overridden per org; the application falls back to these).
DEFAULT_ORG_SETTINGS: dict[str, Any] = {
    "approval_mode": "confirm_writes",   # auto | confirm_writes | confirm_all
    "separation_of_duties": True,        # irreversible actions need an approver other than the run's creator
    "approval_timeout_s": 3600,          # a pending approval expires after this long
    "limits": {"runs_per_day": 0, "tokens_per_day": 0},  # 0 = unlimited
    "models": [],                        # model fallback chain; empty = the deployment default
}


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in ({', '.join(repr(v) for v in values)})"


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def now_col() -> Mapped[datetime]:
    """A required timestamptz that defaults to the database clock."""
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def ts_col(nullable: bool = True) -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


def jsonb(empty: Any = None, **kwargs: Any) -> Mapped[Any]:
    """A JSONB column. Python ``None`` is stored as SQL NULL; ``empty`` (a dict or list) is also the server default."""
    server_default = text(f"'{json.dumps(empty)}'::jsonb") if empty is not None else None
    return mapped_column(JSONB(none_as_null=True), server_default=server_default, **kwargs)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


# --------------------------------------------------------------------------- identity and tenancy


class Org(Base):
    """An organisation (tenant). All runs, memory, credentials and usage belong to exactly one org."""

    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(Text)
    settings: Mapped[dict] = jsonb({}, default=lambda: dict(DEFAULT_ORG_SETTINGS))  # keys: DEFAULT_ORG_SETTINGS
    sim_tenant_id: Mapped[str | None] = mapped_column(Text)  # the org's simulated company (provisioned later)
    created_at: Mapped[datetime] = now_col()


class User(Base):
    """A person who can sign in. Belongs to organisations through ``memberships``."""

    __tablename__ = "users"
    __table_args__ = (Index("uq_users_email_lower", func.lower(text("email")), unique=True),)  # case-insensitive

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)  # argon2
    display_name: Mapped[str] = mapped_column(Text, server_default="")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = now_col()
    last_login_at: Mapped[datetime | None] = ts_col()


class Membership(Base):
    """A user's role inside an organisation (a user can belong to several)."""

    __tablename__ = "memberships"
    __table_args__ = (CheckConstraint(_in("role", ROLES), name="role"), Index("ix_memberships_user_id", "user_id"))

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))  # False = deactivated in this org only
    created_at: Mapped[datetime] = now_col()


class AuthSession(Base):
    """A server-side login session. ``id`` is the hash of the random token in the cookie, never the token itself.

    ``expires_at`` slides forward while the session is used (idle timeout); ``absolute_expires_at`` never moves."""

    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user_id", "user_id"), Index("ix_auth_sessions_expires_at", "expires_at"))

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))  # the active org
    created_at: Mapped[datetime] = now_col()
    expires_at: Mapped[datetime] = ts_col(nullable=False)
    absolute_expires_at: Mapped[datetime] = ts_col(nullable=False)
    last_seen_at: Mapped[datetime] = now_col()
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = ts_col()


class LoginAttempt(Base):
    """Failed-login counter per throttle key (e.g. ``ip:1.2.3.4`` or ``email:a@b``) and fixed time window."""

    __tablename__ = "login_attempts"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, server_default="0")


# --------------------------------------------------------------------------- runs


class Run(Base):
    """One agent run: header, queue/lease bookkeeping, runner checkpoint (``runtime``) and the final report.

    Steps, facts and events live in their own append-only tables. ``event_seq`` is the per-run counter that hands
    out gapless event numbers under the row lock."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        Index("ix_runs_org_id_queued_at", "org_id", text("queued_at desc")),
        # The job queue: workers claim the highest priority, oldest queued run (FOR UPDATE SKIP LOCKED).
        Index("ix_runs_queue", text("priority desc"), "queued_at", postgresql_where=text("status = 'queued'")),
        Index("ix_runs_lease_expires_at", "lease_expires_at", postgresql_where=text("lease_owner is not null")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    task: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), server_default="pending")
    approval_mode: Mapped[str | None] = mapped_column(String(24))
    goal_summary: Mapped[str] = mapped_column(Text, server_default="")
    success_criteria: Mapped[list] = jsonb([])
    plan: Mapped[list] = jsonb([])
    usage: Mapped[dict] = jsonb({})
    final_report: Mapped[dict | None] = jsonb()
    runtime: Mapped[dict | None] = jsonb()
    report_markdown: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = now_col()
    started_at: Mapped[datetime | None] = ts_col()
    finished_at: Mapped[datetime | None] = ts_col()
    resume_count: Mapped[int] = mapped_column(Integer, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = ts_col()
    heartbeat_at: Mapped[datetime | None] = ts_col()
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    priority: Mapped[int] = mapped_column(Integer, server_default="0")
    event_seq: Mapped[int] = mapped_column(Integer, server_default="0")  # last event number handed out


class RunStep(Base):
    """One finished step of a run (tool call, approval, question, verifier action or system note). Never updated."""

    __tablename__ = "run_steps"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)  # 1-based position in the run
    kind: Mapped[str] = mapped_column(String(16), server_default="tool")
    thought: Mapped[str] = mapped_column(Text, server_default="")
    thought_is_summary: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    tool: Mapped[str] = mapped_column(Text, server_default="")
    args: Mapped[dict] = jsonb({})
    risk: Mapped[str] = mapped_column(String(16), server_default="read")
    ok: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    observation: Mapped[str] = mapped_column(Text, server_default="")
    error: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list] = jsonb([])
    duration_ms: Mapped[int] = mapped_column(Integer, server_default="0")
    retries: Mapped[int] = mapped_column(Integer, server_default="0")
    ts: Mapped[datetime] = now_col()
    turn: Mapped[int] = mapped_column(Integer, server_default="0")
    tool_call_id: Mapped[str] = mapped_column(Text, server_default="")


class RunFact(Base):
    """A fact the agent discovered during a run, with provenance. ``ord`` keeps first-insertion order on overwrite."""

    __tablename__ = "run_facts"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, server_default="")
    source: Mapped[str] = mapped_column(Text, server_default="")
    step: Mapped[int] = mapped_column(Integer, server_default="0")
    ord: Mapped[int] = mapped_column(BigInteger, Identity())


class RunEvent(Base):
    """The ordered event log of a run (what the dashboard replays and streams). ``seq`` is 1-based and gapless."""

    __tablename__ = "run_events"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = jsonb({})
    ts: Mapped[datetime] = now_col()


class Approval(Base):
    """A durable human decision request: an approval of a risky action, or a question the agent asks the user."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(_in("kind", APPROVAL_KINDS), name="kind"),
        CheckConstraint(_in("status", APPROVAL_STATUSES), name="status"),
        Index("ix_approvals_org_id_status", "org_id", "status"),
        Index("ix_approvals_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = jsonb({})
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    requested_at: Mapped[datetime] = now_col()
    expires_at: Mapped[datetime | None] = ts_col()
    responded_at: Mapped[datetime | None] = ts_col()
    responded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    response: Mapped[dict | None] = jsonb()


# --------------------------------------------------------------------------- memory and vault


class OrgMemory(Base):
    """Long-term, org-wide knowledge written by verified runs and recalled by keyword (full-text) search.

    ``search`` is a generated tsvector over key (weight A), content (B) and scope (C), indexed with GIN."""

    __tablename__ = "org_memory"
    __table_args__ = (Index("ix_org_memory_org_id", "org_id"),
                      Index("ix_org_memory_search", "search", postgresql_using="gin"))

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text, server_default="")  # the app or system it concerns, e.g. "erp"
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(Text, server_default="")  # provenance note: step, page, tool
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    verified: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    use_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_used_at: Mapped[datetime | None] = ts_col()
    expires_at: Mapped[datetime | None] = ts_col()
    created_at: Mapped[datetime] = now_col()
    search: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("setweight(to_tsvector('english', coalesce(key, '')), 'A') || "
                 "setweight(to_tsvector('english', coalesce(content, '')), 'B') || "
                 "setweight(to_tsvector('english', coalesce(scope, '')), 'C')", persisted=True))


class Credential(Base):
    """An encrypted credential of the vault. ``ciphertext`` is a Fernet token: the database never holds plaintext.
    ``owner`` is an org id (as text) or ``default``; the other columns are non-secret metadata."""

    __tablename__ = "credentials"

    owner: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(Text, server_default="")
    url_hint: Mapped[str] = mapped_column(Text, server_default="")
    host: Mapped[str] = mapped_column(Text, server_default="")  # the host the secret may be sent to
    field_names: Mapped[list] = jsonb([])
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = now_col()
    updated_at: Mapped[datetime] = now_col()


# --------------------------------------------------------------------------- usage, limits, audit


class UsageEvent(Base):
    """LLM usage of one run (one row per recording), for per-org and per-user accounting and quotas."""

    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_events_org_id_ts", "org_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    model: Mapped[str] = mapped_column(Text, server_default="")
    calls: Mapped[int] = mapped_column(Integer, server_default="0")
    input_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default="0")
    ts: Mapped[datetime] = now_col()


class LlmLimit(Base):
    """Token-bucket state of the shared LLM rate limiter, one row per API key (hash), updated under ``FOR UPDATE``."""

    __tablename__ = "llm_limits"

    key_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    tokens: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = now_col()


class ModelHealth(Base):
    """Which models are exhausted or failing, per API key (hash) and model, shared by every process."""

    __tablename__ = "model_health"

    key_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(24))  # ok | quota | unavailable | unknown
    until: Mapped[datetime | None] = ts_col()
    reason: Mapped[str] = mapped_column(Text, server_default="")


class AuditLog(Base):
    """Append-only trail of security-relevant actions: logins, role changes, approvals, credential changes."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_org_id_ts", "org_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orgs.id", ondelete="SET NULL"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text, server_default="")
    target_id: Mapped[str] = mapped_column(Text, server_default="")
    details: Mapped[dict] = jsonb({})
    ts: Mapped[datetime] = now_col()
