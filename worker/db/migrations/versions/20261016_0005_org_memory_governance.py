"""organization memory: one row per key, update time, and durable run facts

* ``uq_org_memory_org_key_lower`` on ``org_memory (org_id, lower(key))``: saving a known key updates its row instead of
  adding a duplicate.
* ``org_memory.updated_at``: when the content was last written (shown as the age of a memory).
* ``run_facts.durable`` / ``run_facts.scope``: the agent marks a fact as a candidate for organization memory; it is only
  written to ``org_memory`` after the run passed independent verification (``worker/memory_store.py``).

Additive only.

Revision ID: 0005_memory
Revises: 0004_usage
Create Date: 2026-10-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0005_memory'
down_revision: str | None = '0004_usage'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('org_memory', sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                                          nullable=False))
    op.execute("update org_memory set updated_at = created_at")
    # Rows written before this migration could repeat a key: keep the newest of each.
    op.execute("delete from org_memory a using org_memory b where a.org_id = b.org_id and lower(a.key) = lower(b.key) "
               "and (a.created_at, a.id) < (b.created_at, b.id)")
    op.create_index('uq_org_memory_org_key_lower', 'org_memory', ['org_id', sa.literal_column('lower(key)')], unique=True)
    op.add_column('run_facts', sa.Column('durable', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('run_facts', sa.Column('scope', sa.Text(), server_default='', nullable=False))


def downgrade() -> None:
    op.drop_column('run_facts', 'scope')
    op.drop_column('run_facts', 'durable')
    op.drop_index('uq_org_memory_org_key_lower', table_name='org_memory')
    op.drop_column('org_memory', 'updated_at')
