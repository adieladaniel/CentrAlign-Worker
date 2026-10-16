"""usage ledger: index for the per-org daily budget and the usage reports

* ``ix_usage_events_org_id_ts_user_id`` on ``usage_events (org_id, ts, user_id)``: the budget check sums one org's rows
  since midnight (UTC) and the reports group them by day and user; the index lets both run index-only.

Additive only. (The shared rate limiter and model health need no schema change: capacity and refill rate come from the
settings, and ``llm_limits`` / ``model_health`` already hold the bucket and the per-key, per-model state.)

Revision ID: 0004_usage
Revises: 0003_jobs
Create Date: 2026-10-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0004_usage'
down_revision: Union[str, None] = '0003_jobs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_usage_events_org_id_ts_user_id', 'usage_events', ['org_id', 'ts', 'user_id'])


def downgrade() -> None:
    op.drop_index('ix_usage_events_org_id_ts_user_id', table_name='usage_events')
