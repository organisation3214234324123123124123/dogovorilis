import uuid
from datetime import date, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.modules.auth.models import User
from app.modules.confirm.models import AccessLink, AccessLinkToken
from app.modules.items.models import OPEN_STATUSES, Item
from app.modules.series.models import Meeting, Person, Series


def get_link(db: Session, token_hash: bytes) -> AccessLink | None:
    """Сначала ищем по токенам писем, потом по собственному хешу ссылки; оба поля под UNIQUE."""
    link = db.scalar(
        select(AccessLink)
        .join(AccessLinkToken, AccessLinkToken.link_id == AccessLink.id)
        .where(AccessLinkToken.token_hash == token_hash)
    )
    if link is not None:
        return link
    return db.scalar(select(AccessLink).where(AccessLink.token_hash == token_hash))


def add_token(db: Session, link_id: uuid.UUID, token_hash: bytes) -> None:
    db.add(AccessLinkToken(link_id=link_id, token_hash=token_hash))
    db.flush()


def link_context(db: Session, link: AccessLink) -> tuple[Meeting, Series, User, Person]:
    meeting, series, host = db.execute(
        select(Meeting, Series, User)
        .join(Series, Series.id == Meeting.series_id)
        .join(User, User.id == Series.owner_user_id)
        .where(Meeting.id == link.meeting_id)
    ).one()
    person = db.get(Person, link.person_id)
    assert person is not None
    return meeting, series, host, person


def person_items(db: Session, meeting_id: uuid.UUID, person_id: uuid.UUID) -> list[Item]:
    stmt = (
        select(Item)
        .where(Item.meeting_id == meeting_id, Item.assignee_person_id == person_id)
        .where(Item.sent_at.is_not(None))
        .order_by(Item.created_at, Item.id)
    )
    return list(db.scalars(stmt))


def active_links(db: Session, meeting_id: uuid.UUID, person_id: uuid.UUID) -> list[AccessLink]:
    stmt = select(AccessLink).where(
        AccessLink.meeting_id == meeting_id,
        AccessLink.person_id == person_id,
        AccessLink.revoked_at.is_(None),
    )
    return list(db.scalars(stmt))


def active_link(db: Session, person_id: uuid.UUID, meeting_id: uuid.UUID) -> AccessLink | None:
    stmt = select(AccessLink).where(
        AccessLink.person_id == person_id,
        AccessLink.meeting_id == meeting_id,
        AccessLink.revoked_at.is_(None),
    )
    return db.scalar(stmt)


def latest_open_due(db: Session, meeting_id: uuid.UUID, person_id: uuid.UUID) -> date | None:
    stmt = select(func.max(Item.due_date)).where(
        Item.meeting_id == meeting_id,
        Item.assignee_person_id == person_id,
        Item.status.in_(OPEN_STATUSES),
    )
    return db.scalar(stmt)


def add_link(db: Session, link: AccessLink) -> AccessLink:
    db.add(link)
    db.flush()
    return link


def revoke_person_links(db: Session, person_id: uuid.UUID, now: datetime) -> int:
    result = db.execute(
        update(AccessLink)
        .where(AccessLink.person_id == person_id, AccessLink.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.flush()
    return result.rowcount


def delete_expired_before(db: Session, cutoff: datetime, limit: int) -> int:
    result = db.execute(
        delete(AccessLink).where(
            AccessLink.id.in_(
                select(AccessLink.id).where(AccessLink.expires_at < cutoff).limit(limit)
            )
        )
    )
    db.flush()
    return result.rowcount
