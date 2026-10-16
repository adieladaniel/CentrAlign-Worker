"""job queue and durable approvals: run options and resume flag, approval consumption

* ``runs.options``: how the run was requested (e.g. ``{"headless": true}``), read by the worker that claims it.
* ``runs.resume_requested``: set when a run is put back in the queue after an interruption, so the next worker continues
  it from its checkpoint instead of starting it over.
* ``approvals.consumed_at``: when the waiting run took the answer. A run that crashed and resumes re-attaches to its
  open approval, or to an answer given while no worker was waiting, and must not take the same answer twice.

* ``ix_runs_org_id_active``: partial index for the per-org concurrency count made when a run is claimed.

Additive only.

Revision ID: 0003_jobs
Revises: 0002
Create Date: 2026-10-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0003_jobs'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('runs', sa.Column('options', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
                                    server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column('runs', sa.Column('resume_requested', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('approvals', sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_runs_org_id_active', 'runs', ['org_id'], unique=False, postgresql_where=sa.text(
        "status in ('running', 'waiting_approval', 'waiting_user', 'verifying')"))


def downgrade() -> None:
    op.drop_index('ix_runs_org_id_active', table_name='runs', postgresql_where=sa.text(
        "status in ('running', 'waiting_approval', 'waiting_user', 'verifying')"))
    op.drop_column('approvals', 'consumed_at')
    op.drop_column('runs', 'resume_requested')
    op.drop_column('runs', 'options')
