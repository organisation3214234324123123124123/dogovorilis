import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, created_at_column, pg_enum


class CaptureMode(StrEnum):
    MANUAL = "manual"
    AUDIO = "audio"


class SeriesStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MeetingStatus(StrEnum):
    PLANNED = "planned"
    LIVE = "live"
    CLOSING = "closing"
    SENT = "sent"


class ClosureTrigger(StrEnum):
    BUTTON = "button"
    TIMER = "timer"


class AgendaItemStatus(StrEnum):
    OPEN = "open"
    COVERED = "covered"
    MOVED = "moved"


class Organization(Base):
    """Сторона участника. У ведущего ровно одна своя организация (is_own)."""

    __tablename__ = "organizations"
    __table_args__ = (
        Index(
            "uq_organizations_owner_user_id_lower_name",
            "owner_user_id",
            func.lower(text("name")),
            unique=True,
        ),
        Index(
            "uq_organizations_owner_user_id_own",
            "owner_user_id",
            unique=True,
            postgresql_where=text("is_own"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    is_own: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = created_at_column()


class Person(Base):
    """Участник без учётной записи. По запросу обезличивается, но не удаляется (152-ФЗ)."""

    __tablename__ = "persons"
    __table_args__ = (UniqueConstraint("owner_user_id", "email"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    full_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(CITEXT)
    anonymized_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()


OVERLOAD_THRESHOLD_DEFAULT = 5  # BR-9: used while series.overload_threshold is NULL


class Series(Base):
    __tablename__ = "series"
    __table_args__ = (
        Index("ix_series_owner_user_id_status", "owner_user_id", "status"),
        Index("ix_series_archived_at", "archived_at"),
        CheckConstraint("planned_duration_min BETWEEN 15 AND 240", name="planned_duration_min"),
        CheckConstraint("overload_threshold >= 1", name="overload_threshold"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64))
    planned_duration_min: Mapped[int] = mapped_column(SmallInteger)
    capture_mode: Mapped[CaptureMode] = mapped_column(
        pg_enum(CaptureMode, "capture_mode"),
        default=CaptureMode.MANUAL,
        server_default=CaptureMode.MANUAL.value,
    )
    status: Mapped[SeriesStatus] = mapped_column(
        pg_enum(SeriesStatus, "series_status"),
        default=SeriesStatus.ACTIVE,
        server_default=SeriesStatus.ACTIVE.value,
    )
    overload_threshold: Mapped[int | None]
    archived_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()


class SeriesMember(Base):
    __tablename__ = "series_members"

    series_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("series.id", ondelete="CASCADE"), primary_key=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (
        Index("ix_meetings_series_id_planned_start", "series_id", text("planned_start DESC")),
        Index(
            "uq_meetings_series_id_active",
            "series_id",
            unique=True,
            postgresql_where=text("status IN ('live', 'closing')"),
        ),
        CheckConstraint("planned_end > planned_start", name="planned_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    series_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"))
    planned_start: Mapped[datetime]
    planned_end: Mapped[datetime]
    status: Mapped[MeetingStatus] = mapped_column(
        pg_enum(MeetingStatus, "meeting_status"),
        default=MeetingStatus.PLANNED,
        server_default=MeetingStatus.PLANNED.value,
    )
    started_at: Mapped[datetime | None]
    closure_opened_at: Mapped[datetime | None]
    closure_trigger: Mapped[ClosureTrigger | None] = mapped_column(
        pg_enum(ClosureTrigger, "closure_trigger")
    )
    summary_sent_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_at_column()


class MeetingParticipant(Base):
    __tablename__ = "meeting_participants"

    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    present: Mapped[bool] = mapped_column(default=True, server_default="true")


class AgendaItem(Base):
    """Пункт повестки. Не больше 20 на встречу, это проверяет сервис (FR-08)."""

    __tablename__ = "agenda_items"
    __table_args__ = (UniqueConstraint("meeting_id", "position"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(SmallInteger)
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[AgendaItemStatus] = mapped_column(
        pg_enum(AgendaItemStatus, "agenda_item_status"),
        default=AgendaItemStatus.OPEN,
        server_default=AgendaItemStatus.OPEN.value,
    )
