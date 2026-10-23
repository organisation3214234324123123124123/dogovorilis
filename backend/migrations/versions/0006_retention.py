"""Добавляет series.archived_at и разрешает задаче очистки удалять старые item_events (BR-20).

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

NEW_FUNCTION = """
CREATE OR REPLACE FUNCTION item_events_no_direct_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() <= 1 AND coalesce(current_setting('app.retention_cleanup', true), '') <> 'on' THEN
        RAISE EXCEPTION 'item_events is append-only: rows go only with their item';
    END IF;
    RETURN OLD;
END;
$$;
"""

OLD_FUNCTION = """
CREATE OR REPLACE FUNCTION item_events_no_direct_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() <= 1 THEN
        RAISE EXCEPTION 'item_events is append-only: rows go only with their item';
    END IF;
    RETURN OLD;
END;
$$;
"""


def upgrade() -> None:
    op.add_column("series", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE series SET archived_at = now() WHERE status = 'archived'")
    op.create_index("ix_series_archived_at", "series", ["archived_at"])
    op.create_index("ix_item_events_created_at", "item_events", ["created_at"])
    op.execute(NEW_FUNCTION)


def downgrade() -> None:
    op.execute(OLD_FUNCTION)
    op.drop_index("ix_item_events_created_at", table_name="item_events")
    op.drop_index("ix_series_archived_at", table_name="series")
    op.drop_column("series", "archived_at")
