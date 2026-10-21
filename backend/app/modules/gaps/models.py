import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, created_at_column, pg_enum


class GapType(StrEnum):
    NO_ASSIGNEE = "no_assignee"
    NO_DUE = "no_due"
    DUE_VAGUE = "due_vague"
    DUE_PAST = "due_past"
    VAGUE_ACTION = "vague_action"


BLOCKING_GAPS = (GapType.NO_ASSIGNEE, GapType.NO_DUE, GapType.DUE_VAGUE, GapType.DUE_PAST)


class GapSource(StrEnum):
    RULE = "rule"
    LLM = "llm"


class Gap(Base):
    """Пробел пункта. Открыт, пока resolved_at и dismissed_at пусты."""

    __tablename__ = "gaps"
    __table_args__ = (
        Index(
            "uq_gaps_item_id_type_open",
            "item_id",
            "type",
            unique=True,
            postgresql_where=text("resolved_at IS NULL AND dismissed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    type: Mapped[GapType] = mapped_column(pg_enum(GapType, "gap_type"))
    source: Mapped[GapSource] = mapped_column(
        pg_enum(GapSource, "gap_source"), default=GapSource.RULE, server_default="rule"
    )
    question: Mapped[str | None] = mapped_column(String(300))
    detected_at: Mapped[datetime] = created_at_column()
    resolved_at: Mapped[datetime | None]
    dismissed_at: Mapped[datetime | None]
