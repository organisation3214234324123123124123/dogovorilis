import hashlib
import hmac
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import Forbidden, Gone, NotFound
from app.modules.analytics import service as analytics_service
from app.modules.confirm import repository
from app.modules.confirm.models import AccessLink
from app.modules.confirm.schemas import (
    DeclineRequest,
    PublicAction,
    PublicItem,
    PublicPage,
    RescheduleRequest,
)
from app.modules.confirm.tokens import new_token, token_hash
from app.modules.items import service as items_service
from app.modules.items.models import Item, ItemStatus

RESCHEDULE_LIMIT = timedelta(days=60)
LINK_MIN_TTL = timedelta(days=14)
LINK_AFTER_DUE = timedelta(days=30)
_ACTIONS: dict[ItemStatus, list[PublicAction]] = {
    ItemStatus.PENDING: ["accept", "reschedule", "decline"],
    ItemStatus.ACCEPTED: ["done", "reschedule", "decline"],
}


def recalc_link_expiry(db: Session, meeting_id: uuid.UUID, person_id: uuid.UUID) -> None:
    """Срок ссылки: большее из «выдача + 14 дней» и «последний открытый срок + 30 дней» (BR-18)."""
    latest = repository.latest_open_due(db, meeting_id, person_id)
    for link in repository.active_links(db, meeting_id, person_id):
        until = link.created_at + LINK_MIN_TTL
        if latest is not None:
            due_end = datetime.combine(latest, datetime.min.time(), UTC)
            until = max(until, due_end + LINK_AFTER_DUE)
        link.expires_at = until
    db.flush()


def csrf_token(token: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, b"csrf:" + token_hash(token), hashlib.sha256).hexdigest()


def _active_link(db: Session, token: str) -> AccessLink:
    link = repository.get_link(db, token_hash(token))
    if link is None:
        raise NotFound("Ссылка не найдена")
    if link.revoked_at is not None or link.expires_at <= datetime.now(UTC):
        _, _, host, _ = repository.link_context(db, link)
        raise Gone("Ссылка устарела", host_name=host.full_name)
    return link


def _public_item(item: Item, today: date) -> PublicItem:
    return PublicItem(
        id=item.id,
        what=item.what,
        due_date=item.due_date,
        done_criteria=item.done_criteria,
        status=item.status,
        is_overdue=items_service.is_overdue(item, today),
        actions=_ACTIONS.get(item.status, []),
        max_due_date=item.due_date + RESCHEDULE_LIMIT if item.due_date else None,
    )


def get_page(db: Session, token: str, notification_type: str | None = None) -> PublicPage:
    """GET ничего не меняет в пунктах, пишется только факт открытия (BR-12, OQ-14)."""
    link = _active_link(db, token)
    meeting, series, host, person = repository.link_context(db, link)
    link.last_used_at = datetime.now(UTC)
    analytics_service.track(
        db,
        "link_opened",
        series_id=series.id,
        meeting_id=meeting.id,
        notification_type=notification_type or "SUMMARY",
    )
    today = items_service.today_in(series.timezone)
    return PublicPage(
        host_name=host.full_name,
        series_title=series.title,
        meeting_start=meeting.planned_start,
        person_name=person.full_name,
        timezone=series.timezone,
        today=today,
        csrf_token=csrf_token(token),
        items=[_public_item(i, today) for i in repository.person_items(db, meeting.id, person.id)],
    )


def _check_post(db: Session, token: str, item_id: uuid.UUID, csrf: str | None) -> AccessLink:
    link = _active_link(db, token)
    if not csrf or not hmac.compare_digest(csrf, csrf_token(token)):
        raise Forbidden("Нет или неверный CSRF-токен", code="CSRF_INVALID")
    meeting, _, _, person = repository.link_context(db, link)
    if item_id not in {i.id for i in repository.person_items(db, meeting.id, person.id)}:
        raise Forbidden("Это не ваш пункт", code="NOT_YOUR_ITEM")
    return link


def _answer(
    db: Session,
    token: str,
    item_id: uuid.UUID,
    csrf: str | None,
    action: PublicAction,
    *,
    due_date: date | None = None,
    comment: str | None = None,
) -> PublicItem:
    link = _check_post(db, token, item_id, csrf)
    link.last_used_at = datetime.now(UTC)
    meeting, series, _, person = repository.link_context(db, link)
    item = items_service.answer_by_link(
        db, person, meeting.id, item_id, action, due_date=due_date, comment=comment
    )
    return _public_item(item, items_service.today_in(series.timezone))


def accept(db: Session, token: str, item_id: uuid.UUID, csrf: str | None) -> PublicItem:
    return _answer(db, token, item_id, csrf, "accept")


def reschedule(
    db: Session, token: str, item_id: uuid.UUID, csrf: str | None, body: RescheduleRequest
) -> PublicItem:
    return _answer(
        db, token, item_id, csrf, "reschedule", due_date=body.due_date, comment=body.comment
    )


def decline(
    db: Session, token: str, item_id: uuid.UUID, csrf: str | None, body: DeclineRequest
) -> PublicItem:
    return _answer(db, token, item_id, csrf, "decline", comment=body.comment)


def done(db: Session, token: str, item_id: uuid.UUID, csrf: str | None) -> PublicItem:
    return _answer(db, token, item_id, csrf, "done")


def issue_link(
    db: Session, person_id: uuid.UUID, meeting_id: uuid.UUID, now: datetime
) -> AccessLink:
    """На пару (человек, встреча) действует одна ссылка; выдавая новую, отзываем прежнюю."""
    previous = repository.active_link(db, person_id, meeting_id)
    if previous is not None:
        previous.revoked_at = now
        db.flush()
    expires = now + LINK_MIN_TTL
    due = repository.latest_open_due(db, meeting_id, person_id)
    if due is not None:
        expires = max(expires, datetime.combine(due, datetime.min.time(), UTC) + LINK_AFTER_DUE)
    link_id = uuid.uuid4()
    return repository.add_link(
        db,
        AccessLink(
            id=link_id,
            token_hash=token_hash(new_token()),
            person_id=person_id,
            meeting_id=meeting_id,
            expires_at=expires,
        ),
    )


def issue_token(db: Session, link_id: uuid.UUID) -> str:
    token = new_token()
    repository.add_token(db, link_id, token_hash(token))
    return token


def ensure_link(
    db: Session, person_id: uuid.UUID, meeting_id: uuid.UUID, now: datetime
) -> AccessLink:
    """Новую ссылку выдаём, только если действующей нет или она истекла."""
    link = repository.active_link(db, person_id, meeting_id)
    if link is not None and link.expires_at > now:
        return link
    return issue_link(db, person_id, meeting_id, now)


def revoke_person_links(db: Session, person_id: uuid.UUID, now: datetime) -> int:
    return repository.revoke_person_links(db, person_id, now)


def purge_expired_links(db: Session, cutoff: datetime, limit: int) -> int:
    return repository.delete_expired_before(db, cutoff, limit)
