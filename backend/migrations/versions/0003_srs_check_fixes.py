"""Добавляет users.pd_consent_at, series.overload_threshold, тип PASSWORD_RESET и запрет прямого DELETE из журнала.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

OLD_TYPES = (
    "SUMMARY",
    "CONFIRM_REQUEST",
    "CONFIRM_REPEAT",
    "HOST_RESPONSE",
    "DUE_TOMORROW",
    "ITEM_CHANGED",
    "ITEM_REASSIGNED",
)

# Прямой DELETE из журнала запрещён (BR-19, OQ-17): строки удаляются только каскадом
# из items (удаление черновика FR-15, очистка BR-20). Каскад выполняется внутри триггера
# внешнего ключа, поэтому там pg_trigger_depth() > 1.
ITEM_EVENTS_NO_DELETE = """
CREATE FUNCTION item_events_no_direct_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() <= 1 THEN
        RAISE EXCEPTION 'item_events is append-only: rows go only with their item';
    END IF;
    RETURN OLD;
END;
$$;
CREATE TRIGGER item_events_no_direct_delete BEFORE DELETE ON item_events
    FOR EACH ROW EXECUTE FUNCTION item_events_no_direct_delete();
"""


def upgrade() -> None:
    op.add_column("users", sa.Column("pd_consent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("series", sa.Column("overload_threshold", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_series_overload_threshold"), "series", "overload_threshold >= 1"
    )
    op.execute("ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'PASSWORD_RESET'")
    op.execute(ITEM_EVENTS_NO_DELETE)


def downgrade() -> None:
    op.execute("DROP TRIGGER item_events_no_direct_delete ON item_events")
    op.execute("DROP FUNCTION item_events_no_direct_delete()")
    op.execute("DELETE FROM notifications WHERE type = 'PASSWORD_RESET'")
    op.execute("ALTER TYPE notification_type RENAME TO notification_type_old")
    values = ", ".join(f"'{v}'" for v in OLD_TYPES)
    op.execute(f"CREATE TYPE notification_type AS ENUM ({values})")
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN type TYPE notification_type "
        "USING type::text::notification_type"
    )
    op.execute("DROP TYPE notification_type_old")
    op.drop_constraint(op.f("ck_series_overload_threshold"), "series", type_="check")
    op.drop_column("series", "overload_threshold")
    op.drop_column("users", "pd_consent_at")
