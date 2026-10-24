import uuid
from datetime import datetime

from sqlalchemy import func, null, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.modules.notifications.models import Notification, NotificationStatus, NotificationType
from app.modules.series.models import Meeting


def queued_for_update(
    db: Session, ntype: NotificationType, item_id: uuid.UUID, person_id: uuid.UUID
) -> Notification | None:
    """Строка очереди получателя; если её занял отправитель, пропускаем (BR-16)."""
    stmt = (
        select(Notification)
        .where(
            Notification.type == ntype,
            Notification.item_id == item_id,
            Notification.recipient_person_id == person_id,
            Notification.status == NotificationStatus.QUEUED,
        )
        .order_by(Notification.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return db.scalar(stmt)


def add_if_new(db: Session, values: dict) -> bool:
    """Второе письмо с тем же dedupe_key не создаётся (BR-5). True, если строку добавили."""
    result = db.execute(
        insert(Notification)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(Notification.id)
    )
    db.flush()
    return result.first() is not None


def cancel_queued(db: Session, item_id: uuid.UUID, types: tuple[NotificationType, ...]) -> None:
    db.execute(
        update(Notification)
        .where(
            Notification.item_id == item_id,
            Notification.type.in_(types),
            Notification.status == NotificationStatus.QUEUED,
        )
        .values(status=NotificationStatus.CANCELLED)
    )
    db.flush()


def count_password_resets_since(db: Session, user_id: uuid.UUID, since: datetime) -> int:
    return db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.type == NotificationType.PASSWORD_RESET,
            Notification.recipient_user_id == user_id,
            Notification.scheduled_for > since,
        )
    )


def cancel_queued_for(
    db: Session, ntype: NotificationType, meeting_id: uuid.UUID, person_id: uuid.UUID
) -> None:
    db.execute(
        update(Notification)
        .where(
            Notification.type == ntype,
            Notification.meeting_id == meeting_id,
            Notification.recipient_person_id == person_id,
            Notification.status == NotificationStatus.QUEUED,
        )
        .values(status=NotificationStatus.CANCELLED)
    )
    db.flush()


def has_unsent_for(
    db: Session, ntype: NotificationType, meeting_id: uuid.UUID, person_id: uuid.UUID
) -> bool:
    return (
        db.scalar(
            select(Notification.id)
            .where(
                Notification.type == ntype,
                Notification.meeting_id == meeting_id,
                Notification.recipient_person_id == person_id,
                Notification.status.in_((NotificationStatus.QUEUED, NotificationStatus.SENDING)),
            )
            .limit(1)
        )
        is not None
    )


def cancel_queued_for_person(db: Session, person_id: uuid.UUID) -> int:
    result = db.execute(
        update(Notification)
        .where(
            Notification.recipient_person_id == person_id,
            Notification.status == NotificationStatus.QUEUED,
        )
        .values(status=NotificationStatus.CANCELLED)
    )
    db.flush()
    return result.rowcount


def release_stuck(db: Session, claimed_before: datetime) -> None:
    """Вернуть в очередь `sending`-строки, брошенные воркером, который умер после захвата."""
    db.execute(
        update(Notification)
        .where(
            Notification.status == NotificationStatus.SENDING,
            Notification.claimed_at < claimed_before,
        )
        .values(status=NotificationStatus.QUEUED, claimed_at=None)
    )
    db.flush()


def claim_due(db: Session, now: datetime, limit: int) -> list[Notification]:
    """SKIP LOCKED: строку, занятую другим воркером, пропускаем (NFR-06)."""
    stmt = (
        select(Notification)
        .where(Notification.status == NotificationStatus.QUEUED, Notification.scheduled_for <= now)
        .order_by(Notification.scheduled_for, Notification.created_at, Notification.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list(db.scalars(stmt))
    for row in rows:
        row.status = NotificationStatus.SENDING
        row.claimed_at = now
    db.flush()
    return rows


def sent_count(
    db: Session, person_id: uuid.UUID, series_id: uuid.UUID, since: datetime, upto: datetime
) -> int:
    """Письма человеку по серии за [since, upto] (BR-11). Строки одного письма (тот же тип и время)
    считаются раз, дайджест отложенных - тоже одно письмо.
    """
    where = (
        Notification.recipient_person_id == person_id,
        Meeting.series_id == series_id,
        Notification.status == NotificationStatus.SENT,
        Notification.sent_at >= since,
        Notification.sent_at <= upto,
    )

    def count(*columns, postponed: bool) -> int:
        letters = (
            select(*columns)
            .join(Meeting, Meeting.id == Notification.meeting_id)
            .where(*where, Notification.postponed.is_(postponed))
            .distinct()
        )
        return db.scalar(select(func.count()).select_from(letters.subquery())) or 0

    return count(
        Notification.type, Notification.sent_at, Notification.meeting_id, postponed=False
    ) + count(Notification.sent_at, postponed=True)


def clear_payload_before(db: Session, cutoff: datetime, limit: int) -> int:
    result = db.execute(
        update(Notification)
        .where(
            Notification.id.in_(
                select(Notification.id)
                .where(
                    Notification.created_at < cutoff,
                    Notification.status.not_in(
                        (NotificationStatus.QUEUED, NotificationStatus.SENDING)
                    ),
                    Notification.payload.is_not(None),
                )
                .limit(limit)
            )
        )
        .values(payload=null())
    )
    db.flush()
    return result.rowcount


def still_claimed(db: Session, ids: list[uuid.UUID], claimed_at: datetime) -> set[uuid.UUID]:
    """Какие из строк этот запуск ещё держит: другой мог освободить их через release_stuck и
    забрать себе.
    """
    result = db.execute(
        update(Notification)
        .where(
            Notification.id.in_(ids),
            Notification.status == NotificationStatus.SENDING,
            Notification.claimed_at == claimed_at,
        )
        .values(claimed_at=claimed_at)
        .returning(Notification.id)
    )
    db.flush()
    return {row[0] for row in result}
