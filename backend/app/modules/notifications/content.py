import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.auth.models import User
from app.modules.items.models import Item, ItemStatus
from app.modules.notifications.models import NotificationType
from app.modules.series.models import Meeting, Person, Series

ACTION_LABELS = {
    "accept": "Принимаю",
    "reschedule": "Нужен другой срок",
    "decline": "Это не моё",
    "done": "Сделано",
}
CONFIRM_ACTIONS = ("accept", "reschedule", "decline")
AGREED_ACTIONS = ("reschedule", "decline")
REMINDER_ACTIONS = ("done", "reschedule")
SUMMARY_ACTIONS = {ItemStatus.PENDING: CONFIRM_ACTIONS, ItemStatus.ACCEPTED: AGREED_ACTIONS}
UNASSIGNED = "не назначен"


def _base() -> str:
    return get_settings().public_base_url.rstrip("/")


def card_url(item_id: uuid.UUID) -> str:
    return f"{_base()}/items/{item_id}"


def buttons(
    actions: tuple[str, ...], kind: NotificationType, item_id: uuid.UUID
) -> list[dict[str, str]]:
    """Кнопка ведёт на страницу с выбранными действием и пунктом (FR-32.4). Рендер подставляет
    `link_id` перед `query`, поэтому сам токен нигде не хранится.
    """
    return [
        {
            "item_id": str(item_id),
            "action": action,
            "label": ACTION_LABELS[action],
            "query": f"?action={action}&item={item_id}&n={kind.value}",
        }
        for action in actions
    ]


def entry(item: Item, assignee: str | None) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "what": item.what or item.raw_text,
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "done_criteria": item.done_criteria,
        "assignee_name": assignee,
        "status": item.status.value,
    }


def _context(db: Session, meeting_id: uuid.UUID) -> tuple[Meeting, Series, User]:
    meeting, series, host = db.execute(
        select(Meeting, Series, User)
        .join(Series, Series.id == Meeting.series_id)
        .join(User, User.id == Series.owner_user_id)
        .where(Meeting.id == meeting_id)
    ).one()
    return meeting, series, host


def _head(meeting: Meeting, series: Series, host: User) -> dict[str, Any]:
    return {
        "series_title": series.title,
        "host_name": host.full_name,
        "timezone": series.timezone,
        "meeting_start": meeting.planned_start.isoformat(),
    }


def meeting_items(db: Session, meeting_id: uuid.UUID) -> list[Item]:
    stmt = (
        select(Item)
        .where(Item.meeting_id == meeting_id, Item.sent_at.is_not(None))
        .where(Item.status != ItemStatus.CANCELLED)
        .order_by(Item.created_at, Item.id)
    )
    return list(db.scalars(stmt))


def _names(db: Session, items: list[Item]) -> dict[uuid.UUID, str]:
    ids = {i.assignee_person_id for i in items if i.assignee_person_id}
    if not ids:
        return {}
    return {p.id: p.full_name for p in db.scalars(select(Person).where(Person.id.in_(ids)))}


def has_items(db: Session, meeting_id: uuid.UUID, person_id: uuid.UUID) -> bool:
    return any(i.assignee_person_id == person_id for i in meeting_items(db, meeting_id))


def pending_of(db: Session, meeting_id: uuid.UUID, person_id: uuid.UUID) -> list[Item]:
    return [
        i
        for i in meeting_items(db, meeting_id)
        if i.assignee_person_id == person_id and i.status == ItemStatus.PENDING
    ]


def summary(
    db: Session, meeting_id: uuid.UUID, person_id: uuid.UUID, link_id: uuid.UUID | None
) -> dict:
    """Решения встречи: у получателя сначала его собственные, у каждого свои кнопки (FR-35)."""
    meeting, series, host = _context(db, meeting_id)
    items = meeting_items(db, meeting_id)
    names = _names(db, items)
    mine = [i for i in items if i.assignee_person_id == person_id]
    rows = [
        {**entry(i, names.get(i.assignee_person_id, UNASSIGNED)), "mine": i in mine}
        for i in [*mine, *(i for i in items if i not in mine)]
    ]
    payload: dict[str, Any] = {**_head(meeting, series, host), "items": rows}
    if link_id and mine:
        payload["link_id"] = str(link_id)
        payload["buttons"] = [
            b
            for i in mine
            for b in buttons(SUMMARY_ACTIONS.get(i.status, ()), NotificationType.SUMMARY, i.id)
        ]
    return payload


def confirm_request(db: Session, item: Item, link_id: uuid.UUID) -> dict:
    meeting, series, host = _context(db, item.meeting_id)
    return {
        **_head(meeting, series, host),
        "item": entry(item, None),
        "link_id": str(link_id),
        "buttons": buttons(CONFIRM_ACTIONS, NotificationType.CONFIRM_REQUEST, item.id),
    }


def confirm_repeat(
    db: Session, meeting_id: uuid.UUID, items: list[Item], link_id: uuid.UUID
) -> dict[str, Any]:
    meeting, series, host = _context(db, meeting_id)
    return {
        **_head(meeting, series, host),
        "items": [entry(i, None) for i in items],
        "link_id": str(link_id),
        "buttons": [
            b
            for i in items
            for b in buttons(CONFIRM_ACTIONS, NotificationType.CONFIRM_REPEAT, i.id)
        ],
    }


def due_tomorrow(db: Session, item: Item, link_id: uuid.UUID) -> dict[str, Any]:
    meeting, series, host = _context(db, item.meeting_id)
    return {
        **_head(meeting, series, host),
        "item": entry(item, None),
        "link_id": str(link_id),
        "buttons": buttons(REMINDER_ACTIONS, NotificationType.DUE_TOMORROW, item.id),
    }


def host_response(
    item: Item,
    series: Series,
    person: Person,
    kind: str,
    *,
    comment: str | None,
    due_date: date | None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "person_name": person.full_name,
        "series_title": series.title,
        "item": entry(item, None),
        "due_date": due_date.isoformat() if due_date else None,
        "comment": comment,
        "card_url": card_url(item.id),
    }
