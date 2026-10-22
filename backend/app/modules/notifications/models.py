import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, SmallInteger, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, created_at_column, pg_enum


class NotificationType(StrEnum):
    SUMMARY = "SUMMARY"
    CONFIRM_REQUEST = "CONFIRM_REQUEST"
    CONFIRM_REPEAT = "CONFIRM_REPEAT"
    HOST_RESPONSE = "HOST_RESPONSE"
    DUE_TOMORROW = "DUE_TOMORROW"
    ITEM_CHANGED = "ITEM_CHANGED"
    ITEM_REASSIGNED = "ITEM_REASSIGNED"
    PASSWORD_RESET = "PASSWORD_RESET"


class NotificationStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Notification(Base):
    """E-mail. Beat sends queued rows whose scheduled_for (UTC) has come."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_status_scheduled_for", "status", "scheduled_for"),
        CheckConstraint(
            "num_nonnulls(recipient_person_id, recipient_user_id) = 1", name="one_recipient"
        ),
        CheckConstraint("attempts >= 0", name="attempts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    type: Mapped[NotificationType] = mapped_column(pg_enum(NotificationType, "notification_type"))
    recipient_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), index=True
    )
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    scheduled_for: Mapped[datetime]
    status: Mapped[NotificationStatus] = mapped_column(
        pg_enum(NotificationStatus, "notification_status"),
        default=NotificationStatus.QUEUED,
        server_default=NotificationStatus.QUEUED.value,
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()
