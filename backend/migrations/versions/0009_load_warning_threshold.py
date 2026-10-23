"""Переименовывает series.overload_threshold в load_warning_threshold, как поле в API (FR-26.7).

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("series", "overload_threshold", new_column_name="load_warning_threshold")
    op.execute(
        "ALTER TABLE series RENAME CONSTRAINT ck_series_overload_threshold "
        "TO ck_series_load_warning_threshold"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE series RENAME CONSTRAINT ck_series_load_warning_threshold "
        "TO ck_series_overload_threshold"
    )
    op.alter_column("series", "load_warning_threshold", new_column_name="overload_threshold")
