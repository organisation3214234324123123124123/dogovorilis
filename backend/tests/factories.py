import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    AccessLink,
    AgendaItem,
    AnalyticsEvent,
    Gap,
    Item,
    ItemEvent,
    Meeting,
    MeetingParticipant,
    Notification,
    Organization,
    Person,
    Series,
    SeriesMember,
    User,
)
from app.modules.gaps.models import GapType
from app.modules.items.models import ActorType, ItemAction


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def make_user(db: Session, **kw: Any) -> User:
    kw.setdefault("email", f"u{_uid()}@example.com")
    kw.setdefault("password_hash", "x")
    kw.setdefault("full_name", "Host")
    user = User(**kw)
    db.add(user)
    db.flush()
    return user


def make_organization(db: Session, owner: User, **kw: Any) -> Organization:
    kw.setdefault("name", f"Org {_uid()}")
    org = Organization(owner_user_id=owner.id, **kw)
    db.add(org)
    db.flush()
    return org


def make_person(db: Session, owner: User, org: Organization, **kw: Any) -> Person:
    kw.setdefault("full_name", "Person")
    kw.setdefault("email", f"p{_uid()}@example.com")
    person = Person(owner_user_id=owner.id, organization_id=org.id, **kw)
    db.add(person)
    db.flush()
    return person


def make_series(db: Session, owner: User, **kw: Any) -> Series:
    kw.setdefault("title", "Series")
    kw.setdefault("timezone", "Europe/Moscow")
    kw.setdefault("planned_duration_min", 30)
    series = Series(owner_user_id=owner.id, **kw)
    db.add(series)
    db.flush()
    return series


def make_meeting(db: Session, series: Series, **kw: Any) -> Meeting:
    start = kw.pop("planned_start", datetime.now(UTC))
    kw.setdefault("planned_end", start + timedelta(minutes=30))
    meeting = Meeting(series_id=series.id, planned_start=start, **kw)
    db.add(meeting)
    db.flush()
    return meeting


def add_member(db: Session, series: Series, person: Person) -> SeriesMember:
    member = SeriesMember(series_id=series.id, person_id=person.id)
    db.add(member)
    db.flush()
    return member


def make_participant(
    db: Session, meeting: Meeting, person: Person, present: bool = True
) -> MeetingParticipant:
    row = MeetingParticipant(meeting_id=meeting.id, person_id=person.id, present=present)
    db.add(row)
    db.flush()
    return row


def make_agenda_item(db: Session, meeting: Meeting, **kw: Any) -> AgendaItem:
    kw.setdefault("title", "Topic")
    if "position" not in kw:
        kw["position"] = (
            db.query(AgendaItem).filter(AgendaItem.meeting_id == meeting.id).count() + 1
        )
    agenda = AgendaItem(meeting_id=meeting.id, **kw)
    db.add(agenda)
    db.flush()
    return agenda


def make_notification(db: Session, **kw: Any) -> Notification:
    kw.setdefault("scheduled_for", datetime.now(UTC))
    kw.setdefault("dedupe_key", uuid.uuid4().hex)
    notification = Notification(**kw)
    db.add(notification)
    db.flush()
    return notification


def notifications_for(db: Session, item: Item, *types: Any) -> list[Notification]:
    query = db.query(Notification).filter(Notification.item_id == item.id)
    if types:
        query = query.filter(Notification.type.in_(types))
    return query.all()


def events_named(db: Session, name: str, **filters: Any) -> list[AnalyticsEvent]:
    query = db.query(AnalyticsEvent).filter(AnalyticsEvent.name == name)
    for column, value in filters.items():
        query = query.filter(getattr(AnalyticsEvent, column) == value)
    return query.all()


def make_item(db: Session, meeting: Meeting, **kw: Any) -> Item:
    kw.setdefault("raw_text", "do it")
    item = Item(meeting_id=meeting.id, series_id=meeting.series_id, **kw)
    db.add(item)
    db.flush()
    return item


def make_gap(db: Session, item: Item, **kw: Any) -> Gap:
    kw.setdefault("type", GapType.NO_DUE)
    gap = Gap(item_id=item.id, **kw)
    db.add(gap)
    db.flush()
    return gap


def make_event(db: Session, item: Item, **kw: Any) -> ItemEvent:
    kw.setdefault("actor_type", ActorType.SYSTEM)
    kw.setdefault("action", ItemAction.CREATED)
    event = ItemEvent(item_id=item.id, **kw)
    db.add(event)
    db.flush()
    return event


def make_access_link(db: Session, person: Person, meeting: Meeting, **kw: Any) -> AccessLink:
    kw.setdefault("token_hash", uuid.uuid4().bytes * 2)
    kw.setdefault("expires_at", datetime.now(UTC) + timedelta(days=14))
    link = AccessLink(person_id=person.id, meeting_id=meeting.id, **kw)
    db.add(link)
    db.flush()
    return link


class World:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.user = make_user(db)
        self.org = make_organization(db, self.user, is_own=True)
        self.person = make_person(db, self.user, self.org)
        self.series = make_series(db, self.user)
        self.meeting = make_meeting(db, self.series)

    def item(self, **kw: Any) -> Item:
        return make_item(self.db, self.meeting, **kw)
