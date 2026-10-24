import uuid
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.business_days import add_business_days
from app.core.config import get_settings
from app.modules.items.models import Item
from app.modules.notifications import content, repository
from app.modules.notifications.models import NotificationStatus, NotificationType
from app.modules.series.models import Person, Series

REMINDER_HOUR = 9
CHANGED_FIELDS = ("what", "due_date", "done_criteria")


def queue(
    db: Session,
    type_: NotificationType,
    *,
    item_id: uuid.UUID | None,
    meeting_id: uuid.UUID,
    person_id: uuid.UUID,
    dedupe_key: str,
    scheduled_for: datetime,
    payload: dict[str, Any] | None = None,
) -> bool:
    return repository.add_if_new(
        db,
        {
            "type": type_,
            "recipient_person_id": person_id,
            "meeting_id": meeting_id,
            "item_id": item_id,
            "payload": payload,
            "dedupe_key": dedupe_key,
            "scheduled_for": scheduled_for,
        },
    )


def queue_password_reset(
    db: Session,
    user_id: uuid.UUID,
    reset_id: uuid.UUID,
    token: str,
    now: datetime,
    expires_at: datetime,
) -> None:
    """Сам токен лежит только в payload; отправитель не должен его логировать."""
    repository.add_if_new(
        db,
        {
            "id": uuid.uuid4(),
            "type": NotificationType.PASSWORD_RESET,
            "recipient_user_id": user_id,
            "payload": {
                "token": token,
                "url": f"{get_settings().public_base_url.rstrip('/')}/reset-password?token={token}",
                "expires_at": expires_at.isoformat(),
            },
            "dedupe_key": f"{user_id}:PASSWORD_RESET:{reset_id}",
            "scheduled_for": now,
        },
    )


def count_recent_password_resets(db: Session, user_id: uuid.UUID, since: datetime) -> int:
    return repository.count_password_resets_since(db, user_id, since)


def cancel_due_reminder(db: Session, item_id: uuid.UUID) -> None:
    repository.cancel_queued(db, item_id, (NotificationType.DUE_TOMORROW,))


def plan_due_reminder(
    db: Session,
    *,
    item_id: uuid.UUID,
    meeting_id: uuid.UUID,
    person_id: uuid.UUID,
    due_date: date | None,
    version: int,
    tz: str,
    now: datetime,
    payload: dict[str, Any] | None = None,
) -> bool:
    """09:00 рабочего дня перед сроком (BR-8); None, если это время уже прошло."""
    if due_date is None:
        return False
    zone = ZoneInfo(tz)
    at = datetime.combine(add_business_days(due_date, -1), time(REMINDER_HOUR), zone)
    if at <= now:
        return False
    return queue(
        db,
        NotificationType.DUE_TOMORROW,
        item_id=item_id,
        meeting_id=meeting_id,
        person_id=person_id,
        dedupe_key=f"{item_id}:DUE_TOMORROW:{due_date.isoformat()}:{version}",
        scheduled_for=at,
        payload=payload,
    )


def repeat_time(now: datetime, tz: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return datetime.combine(add_business_days(local.date(), 1), local.time(), ZoneInfo(tz))


def cancel_repeat(db: Session, meeting_id: uuid.UUID, person_id: uuid.UUID) -> None:
    repository.cancel_queued_for(db, NotificationType.CONFIRM_REPEAT, meeting_id, person_id)


def plan_reassign_repeat(
    db: Session, item: Item, person_id: uuid.UUID, tz: str, now: datetime
) -> bool:
    """Один повтор на переназначение, если у человека нет ещё не отправленного (BR-10)."""
    if repository.has_unsent_for(db, NotificationType.CONFIRM_REPEAT, item.meeting_id, person_id):
        return False
    return queue(
        db,
        NotificationType.CONFIRM_REPEAT,
        item_id=None,
        meeting_id=item.meeting_id,
        person_id=person_id,
        dedupe_key=f"{item.meeting_id}:CONFIRM_REPEAT:{person_id}:R{item.id}.{item.version}",
        scheduled_for=repeat_time(now, tz),
    )


def queue_host_response(
    db: Session,
    *,
    item: Item,
    series: Series,
    person: Person,
    kind: str,
    event_id: int,
    now: datetime,
    comment: str | None,
) -> None:
    """Ведущего уведомляем сразу, но только про "другой срок" и "не моё"; каждый ответ - отдельное
    письмо (FR-34).
    """
    repository.add_if_new(
        db,
        {
            "type": NotificationType.HOST_RESPONSE,
            "recipient_user_id": series.owner_user_id,
            "meeting_id": item.meeting_id,
            "item_id": item.id,
            "payload": content.host_response(
                item, series, person, kind, comment=comment, due_date=item.due_date
            ),
            "dedupe_key": f"{item.id}:HOST_RESPONSE:{event_id}",
            "scheduled_for": now,
        },
    )


def replan_due_tomorrow(db: Session, item: Item, tz: str) -> None:
    cancel_due_reminder(db, item.id)
    if item.assignee_person_id is None:
        return
    plan_due_reminder(
        db,
        item_id=item.id,
        meeting_id=item.meeting_id,
        person_id=item.assignee_person_id,
        due_date=item.due_date,
        version=item.version,
        tz=tz,
        now=datetime.now(UTC),
    )


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def queue_item_changed(db: Session, item: Item, old: dict[str, object]) -> None:
    """ITEM_CHANGED "было -> стало" для исполнителя (FR-16).

    `old` - значения изменённых полей до правки; `item` уже содержит новые и новую версию. Правки
    до отправки сливаются в одно письмо (BR-16), но только если строку не занял отправитель; иначе
    (отправлена, отменена, занята) создаётся новая.
    """
    person_id = item.assignee_person_id
    if person_id is None:
        return
    row = repository.queued_for_update(db, NotificationType.ITEM_CHANGED, item.id, person_id)
    changes: dict[str, dict[str, str | None]] = dict(
        ((row.payload if row else None) or {}).get("changes") or {}
    )
    for field, before in old.items():
        first = changes[field]["from"] if field in changes else _text(before)
        changes[field] = {"from": first, "to": _text(getattr(item, field))}
    changes = {f: c for f, c in changes.items() if c["from"] != c["to"]}
    key = f"{item.id}:ITEM_CHANGED:{item.version}"
    payload = {"item_id": str(item.id), "meeting_id": str(item.meeting_id), "changes": changes}
    if row is not None:
        if not changes:
            row.status = NotificationStatus.CANCELLED
        row.payload = payload
        row.dedupe_key = key
        db.flush()
        return
    if changes:
        queue(
            db,
            NotificationType.ITEM_CHANGED,
            item_id=item.id,
            meeting_id=item.meeting_id,
            person_id=person_id,
            dedupe_key=key,
            scheduled_for=datetime.now(UTC),
            payload=payload,
        )


def cancel_queued_for_person(db: Session, person_id: uuid.UUID) -> int:
    return repository.cancel_queued_for_person(db, person_id)


def clear_old_payloads(db: Session, cutoff: datetime, limit: int) -> int:
    return repository.clear_payload_before(db, cutoff, limit)
