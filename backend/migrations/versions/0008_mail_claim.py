"""Добавляет захват писем воркером (`sending`, `claimed_at`) и флаг `postponed` (NFR-06, BR-11).

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE notification_status ADD VALUE IF NOT EXISTS 'sending'")
    op.add_column("notifications", sa.Column("claimed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "notifications",
        sa.Column("postponed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("notifications", "postponed")
    op.drop_column("notifications", "claimed_at")
