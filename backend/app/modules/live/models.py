import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, created_at_column


class MeetingEvent(Base):
    """Журнал событий панели (FR-14): по нему клиент догружает пропущенное после обрыва."""

    __tablename__ = "meeting_events"

    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    event: Mapped[str]
    item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    data: Mapped[Any] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = created_at_column()
