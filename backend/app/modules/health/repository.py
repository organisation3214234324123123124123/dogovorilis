from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.notifications.models import Notification, NotificationStatus


def email_queue(db: Session, now: datetime) -> tuple[int, int, datetime | None]:
    queued = NotificationStatus.QUEUED
    row = db.execute(
        select(
            func.count().filter(Notification.status == queued),
            func.count().filter(
                Notification.status == NotificationStatus.FAILED,
                Notification.scheduled_for >= now - timedelta(hours=24),
            ),
            func.min(Notification.scheduled_for).filter(Notification.status == queued),
        )
    ).one()
    return row[0], row[1], row[2]


def last_mail_sent_at(db: Session) -> datetime | None:
    return db.scalar(select(func.max(Notification.sent_at)))
