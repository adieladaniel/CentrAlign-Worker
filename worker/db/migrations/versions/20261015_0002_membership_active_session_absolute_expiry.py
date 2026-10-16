"""auth and tenancy: per-org deactivation of members, absolute session expiry

* ``memberships.is_active``: an admin deactivates a user in one organisation without touching their other memberships.
* ``auth_sessions.absolute_expires_at``: the hard end of a session; ``expires_at`` is the sliding idle timeout.

Additive only. Existing sessions get ``created_at + 7 days`` as their absolute end.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('memberships', sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('auth_sessions', sa.Column('absolute_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE auth_sessions SET absolute_expires_at = created_at + interval '7 days'")
    op.alter_column('auth_sessions', 'absolute_expires_at', nullable=False)


def downgrade() -> None:
    op.drop_column('auth_sessions', 'absolute_expires_at')
    op.drop_column('memberships', 'is_active')
