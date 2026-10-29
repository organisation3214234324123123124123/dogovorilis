"""Создаёт журнал событий панели meeting_events и счётчик meetings.events_version (FR-14).

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meetings",
        sa.Column("events_version", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "meeting_events",
        sa.Column("meeting_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=True),
        sa.Column(
            "data", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("meeting_id", "version"),
    )


def downgrade() -> None:
    op.drop_table("meeting_events")
    op.drop_column("meetings", "events_version")
