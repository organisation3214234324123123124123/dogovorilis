import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, created_at_column, pg_enum


class ItemStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    ACCEPTED = "accepted"
    DISPUTED = "disputed"
    DONE = "done"
    CANCELLED = "cancelled"


OPEN_STATUSES = (ItemStatus.PENDING, ItemStatus.ACCEPTED)


class ItemSource(StrEnum):
    MANUAL = "manual"
    ASR = "asr"


class DoneBy(StrEnum):
    ASSIGNEE = "assignee"
    HOST = "host"
    TRACKER = "tracker"


class ActorType(StrEnum):
    HOST = "host"
    ASSIGNEE = "assignee"
    SYSTEM = "system"
    TRACKER = "tracker"


class ItemAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    ACCEPTED_IN_MEETING = "accepted_in_meeting"
    SENT = "sent"
    ACCEPTED = "accepted"
    RESCHEDULED = "rescheduled"
    DISPUTED = "disputed"
    REASSIGNED = "reassigned"
    DONE = "done"
    CANCELLED = "cancelled"
    REOPENED = "reopened"


class CommentKind(StrEnum):
    NOT_MINE = "not_mine"
    RESCHEDULE = "reschedule"


class Item(Base):
    """Пункт: одна договорённость встречи. Просрочка вычисляется, это не статус."""

    __tablename__ = "items"
    __table_args__ = (
        Index("ix_items_series_id_status", "series_id", "status"),
        Index(
            "ix_items_due_date_open",
            "due_date",
            postgresql_where=text("status IN ('pending', 'accepted')"),
        ),
        CheckConstraint("reschedule_count >= 0", name="reschedule_count"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("(status = 'done') = (done_at IS NOT NULL)", name="done_at"),
        CheckConstraint("(status = 'cancelled') = (cancelled_at IS NOT NULL)", name="cancelled_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    series_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"))
    agenda_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agenda_items.id", ondelete="SET NULL")
    )
    raw_text: Mapped[str] = mapped_column(String(1000))
    what: Mapped[str | None] = mapped_column(String(500))
    assignee_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="RESTRICT"), index=True
    )
    due_date: Mapped[date | None]
    due_text: Mapped[str | None] = mapped_column(String(100))
    done_criteria: Mapped[str | None] = mapped_column(String(500))
    manual_fields: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=list, server_default="{}"
    )
    source: Mapped[ItemSource] = mapped_column(
        pg_enum(ItemSource, "item_source"),
        default=ItemSource.MANUAL,
        server_default=ItemSource.MANUAL.value,
    )
    suggested: Mapped[bool] = mapped_column(default=False, server_default="false")
    status: Mapped[ItemStatus] = mapped_column(
        pg_enum(ItemStatus, "item_status"),
        default=ItemStatus.DRAFT,
        server_default=ItemStatus.DRAFT.value,
    )
    accepted_in_meeting: Mapped[bool] = mapped_column(default=False, server_default="false")
    accepted_at: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    done_at: Mapped[datetime | None]
    done_by: Mapped[DoneBy | None] = mapped_column(pg_enum(DoneBy, "item_done_by"))
    cancelled_at: Mapped[datetime | None]
    reschedule_count: Mapped[int] = mapped_column(default=0, server_default="0")
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ItemEvent(Base):
    """Журнал пункта, только INSERT (BR-19): в data только id, статусы и даты."""

    __tablename__ = "item_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    actor_type: Mapped[ActorType] = mapped_column(pg_enum(ActorType, "item_actor_type"))
    # Без SET NULL: он сделал бы UPDATE в журнале, куда только дописывают.
    # Людей обезличивают, а не удаляют.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    actor_person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("persons.id"))
    action: Mapped[ItemAction] = mapped_column(pg_enum(ItemAction, "item_action"))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = created_at_column()


class ItemComment(Base):
    """Свободный текст исполнителя; чистится по BR-20 и при обезличивании."""

    __tablename__ = "item_comments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    author_person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE")
    )
    kind: Mapped[CommentKind] = mapped_column(pg_enum(CommentKind, "item_comment_kind"))
    text: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = created_at_column()
    anonymized_at: Mapped[datetime | None]
