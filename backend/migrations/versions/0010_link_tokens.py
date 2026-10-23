"""Создаёт таблицу access_link_tokens: свой случайный токен в каждом письме (FR-30.4).

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_link_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("link_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("octet_length(token_hash) = 32", name="ck_access_link_tokens_token_hash_len"),
        sa.ForeignKeyConstraint(["link_id"], ["access_links.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_access_link_tokens_link_id", "access_link_tokens", ["link_id"])


def downgrade() -> None:
    op.drop_index("ix_access_link_tokens_link_id", table_name="access_link_tokens")
    op.drop_table("access_link_tokens")
