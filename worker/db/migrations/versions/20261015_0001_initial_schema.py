"""initial schema: identity, runs and their children, approvals, org memory, vault, usage, LLM limits, audit

Generated from worker/db/models.py and reviewed: the generated tsvector column + GIN index on org_memory, the partial
indexes of the job queue and leases, and the case-insensitive unique index on users.email are as intended.

Revision ID: 0001
Revises:
Create Date: 2026-10-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('credentials',
    sa.Column('owner', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('url_hint', sa.Text(), server_default='', nullable=False),
    sa.Column('host', sa.Text(), server_default='', nullable=False),
    sa.Column('field_names', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('ciphertext', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('owner', 'name', name=op.f('pk_credentials'))
    )
    op.create_table('llm_limits',
    sa.Column('key_hash', sa.String(length=128), nullable=False),
    sa.Column('tokens', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key_hash', name=op.f('pk_llm_limits'))
    )
    op.create_table('login_attempts',
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('count', sa.Integer(), server_default='0', nullable=False),
    sa.PrimaryKeyConstraint('key', 'window_start', name=op.f('pk_login_attempts'))
    )
    op.create_table('model_health',
    sa.Column('key_hash', sa.String(length=128), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reason', sa.Text(), server_default='', nullable=False),
    sa.PrimaryKeyConstraint('key_hash', 'model', name=op.f('pk_model_health'))
    )
    op.create_table('orgs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('settings', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('sim_tenant_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_orgs')),
    sa.UniqueConstraint('slug', name=op.f('uq_orgs_slug'))
    )
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('email', sa.Text(), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), server_default='', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users'))
    )
    op.create_index('uq_users_email_lower', 'users', [sa.literal_column('lower(email)')], unique=True)
    op.create_table('audit_log',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('action', sa.Text(), nullable=False),
    sa.Column('target_type', sa.Text(), server_default='', nullable=False),
    sa.Column('target_id', sa.Text(), server_default='', nullable=False),
    sa.Column('details', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], name=op.f('fk_audit_log_org_id_orgs'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_audit_log_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log'))
    )
    op.create_index('ix_audit_log_org_id_ts', 'audit_log', ['org_id', 'ts'], unique=False)
    op.create_table('auth_sessions',
    sa.Column('id', sa.String(length=128), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('ip', sa.Text(), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], name=op.f('fk_auth_sessions_org_id_orgs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_auth_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_auth_sessions'))
    )
    op.create_index('ix_auth_sessions_expires_at', 'auth_sessions', ['expires_at'], unique=False)
    op.create_index('ix_auth_sessions_user_id', 'auth_sessions', ['user_id'], unique=False)
    op.create_table('memberships',
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role in ('owner', 'admin', 'operator', 'approver', 'viewer')", name=op.f('ck_memberships_role')),
    sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], name=op.f('fk_memberships_org_id_orgs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_memberships_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('org_id', 'user_id', name=op.f('pk_memberships'))
    )
    op.create_index('ix_memberships_user_id', 'memberships', ['user_id'], unique=False)
    op.create_table('runs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('task', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=24), server_default='pending', nullable=False),
    sa.Column('approval_mode', sa.String(length=24), nullable=True),
    sa.Column('goal_summary', sa.Text(), server_default='', nullable=False),
    sa.Column('success_criteria', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('plan', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('usage', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('final_report', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('runtime', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('report_markdown', sa.Text(), nullable=True),
    sa.Column('queued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resume_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('lease_owner', sa.Text(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('cancel_requested', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('priority', sa.Integer(), server_default='0', nullable=False),
    sa.Column('event_seq', sa.Integer(), server_default='0', nullable=False),
    sa.CheckConstraint("status in ('pending', 'queued', 'running', 'waiting_approval', 'waiting_user', 'verifying', 'succeeded', 'failed', 'needs_attention', 'cancelled')", name=op.f('ck_runs_status')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_runs_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], name=op.f('fk_runs_org_id_orgs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_runs'))
    )
    op.create_index('ix_runs_lease_expires_at', 'runs', ['lease_expires_at'], unique=False, postgresql_where=sa.text('lease_owner is not null'))
    op.create_index('ix_runs_org_id_queued_at', 'runs', ['org_id', sa.literal_column('queued_at desc')], unique=False)
    op.create_index('ix_runs_queue', 'runs', [sa.literal_column('priority desc'), 'queued_at'], unique=False, postgresql_where=sa.text("status = 'queued'"))
    op.create_table('approvals',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('payload', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='pending', nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('responded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('responded_by', sa.UUID(), nullable=True),
    sa.Column('response', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.CheckConstraint("kind in ('approval', 'question')", name=op.f('ck_approvals_kind')),
    sa.CheckConstraint("status in ('pending', 'approved', 'denied', 'answered', 'expired', 'cancelled')", name=op.f('ck_approvals_status')),
    sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], name=op.f('fk_approvals_org_id_orgs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['responded_by'], ['users.id'], name=op.f('fk_approvals_responded_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_approvals_run_id_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_approvals'))
    )
    op.create_index('ix_approvals_org_id_status', 'approvals', ['org_id', 'status'], unique=False)
    op.create_index('ix_approvals_run_id', 'approvals', ['run_id'], unique=False)
    op.create_table('org_memory',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('scope', sa.Text(), server_default='', nullable=False),
    sa.Column('source_run_id', sa.UUID(), nullable=True),
    sa.Column('source', sa.Text(), server_default='', nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('verified', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('use_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('search', postgresql.TSVECTOR(), sa.Computed("setweight(to_tsvector('english', coalesce(key, '')), 'A') || setweight(to_tsvector('english', coalesce(content, '')), 'B') || setweight(to_tsvector('english', coalesce(scope, '')), 'C')", persisted=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_org_memory_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], name=op.f('fk_org_memory_org_id_orgs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_run_id'], ['runs.id'], name=op.f('fk_org_memory_source_run_id_runs'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_org_memory'))
    )
    op.create_index('ix_org_memory_org_id', 'org_memory', ['org_id'], unique=False)
    op.create_index('ix_org_memory_search', 'org_memory', ['search'], unique=False, postgresql_using='gin')
    op.create_table('run_events',
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_run_events_run_id_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('run_id', 'seq', name=op.f('pk_run_events'))
    )
    op.create_table('run_facts',
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', sa.Text(), server_default='', nullable=False),
    sa.Column('source', sa.Text(), server_default='', nullable=False),
    sa.Column('step', sa.Integer(), server_default='0', nullable=False),
    sa.Column('ord', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_run_facts_run_id_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('run_id', 'key', name=op.f('pk_run_facts'))
    )
    op.create_table('run_steps',
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('idx', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), server_default='tool', nullable=False),
    sa.Column('thought', sa.Text(), server_default='', nullable=False),
    sa.Column('thought_is_summary', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('tool', sa.Text(), server_default='', nullable=False),
    sa.Column('args', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('risk', sa.String(length=16), server_default='read', nullable=False),
    sa.Column('ok', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('observation', sa.Text(), server_default='', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('evidence', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('duration_ms', sa.Integer(), server_default='0', nullable=False),
    sa.Column('retries', sa.Integer(), server_default='0', nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('turn', sa.Integer(), server_default='0', nullable=False),
    sa.Column('tool_call_id', sa.Text(), server_default='', nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_run_steps_run_id_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('run_id', 'idx', name=op.f('pk_run_steps'))
    )
    op.create_table('usage_events',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('run_id', sa.UUID(), nullable=True),
    sa.Column('model', sa.Text(), server_default='', nullable=False),
    sa.Column('calls', sa.Integer(), server_default='0', nullable=False),
    sa.Column('input_tokens', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('output_tokens', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], name=op.f('fk_usage_events_org_id_orgs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_usage_events_run_id_runs'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_usage_events_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_usage_events'))
    )
    op.create_index('ix_usage_events_org_id_ts', 'usage_events', ['org_id', 'ts'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_usage_events_org_id_ts', table_name='usage_events')
    op.drop_table('usage_events')
    op.drop_table('run_steps')
    op.drop_table('run_facts')
    op.drop_table('run_events')
    op.drop_index('ix_org_memory_search', table_name='org_memory', postgresql_using='gin')
    op.drop_index('ix_org_memory_org_id', table_name='org_memory')
    op.drop_table('org_memory')
    op.drop_index('ix_approvals_run_id', table_name='approvals')
    op.drop_index('ix_approvals_org_id_status', table_name='approvals')
    op.drop_table('approvals')
    op.drop_index('ix_runs_queue', table_name='runs', postgresql_where=sa.text("status = 'queued'"))
    op.drop_index('ix_runs_org_id_queued_at', table_name='runs')
    op.drop_index('ix_runs_lease_expires_at', table_name='runs', postgresql_where=sa.text('lease_owner is not null'))
    op.drop_table('runs')
    op.drop_index('ix_memberships_user_id', table_name='memberships')
    op.drop_table('memberships')
    op.drop_index('ix_auth_sessions_user_id', table_name='auth_sessions')
    op.drop_index('ix_auth_sessions_expires_at', table_name='auth_sessions')
    op.drop_table('auth_sessions')
    op.drop_index('ix_audit_log_org_id_ts', table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index('uq_users_email_lower', table_name='users')
    op.drop_table('users')
    op.drop_table('orgs')
    op.drop_table('model_health')
    op.drop_table('login_attempts')
    op.drop_table('llm_limits')
    op.drop_table('credentials')
