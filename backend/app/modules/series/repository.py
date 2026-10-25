import uuid
from datetime import datetime

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.orm import Session

from app.modules.items.models import Item
from app.modules.series.models import (
    AgendaItem,
    AgendaItemStatus,
    Meeting,
    MeetingParticipant,
    MeetingStatus,
    Organization,
    Person,
    Series,
    SeriesMember,
    SeriesStatus,
)

ACTIVE_MEETING = (MeetingStatus.LIVE, MeetingStatus.CLOSING)


def list_series(db: Session, owner_id: uuid.UUID) -> list[Series]:
    stmt = select(Series).where(Series.owner_user_id == owner_id).order_by(Series.created_at.desc())
    return list(db.scalars(stmt))


def get_series(db: Session, owner_id: uuid.UUID, series_id: uuid.UUID) -> Series | None:
    return db.scalar(select(Series).where(Series.id == series_id, Series.owner_user_id == owner_id))


def members_count(db: Session, series_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    stmt = (
        select(SeriesMember.series_id, func.count())
        .where(SeriesMember.series_id.in_(series_ids))
        .group_by(SeriesMember.series_id)
    )
    return {sid: n for sid, n in db.execute(stmt)}


def active_meetings(db: Session, series_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    stmt = select(Meeting.series_id, Meeting.id).where(
        Meeting.series_id.in_(series_ids), Meeting.status.in_(ACTIVE_MEETING)
    )
    return {sid: mid for sid, mid in db.execute(stmt)}


def last_meeting_starts(db: Session, series_ids: list[uuid.UUID]) -> dict:
    stmt = (
        select(Meeting.series_id, func.max(Meeting.planned_start))
        .where(Meeting.series_id.in_(series_ids))
        .group_by(Meeting.series_id)
    )
    return {sid: at for sid, at in db.execute(stmt)}


def members(db: Session, series_id: uuid.UUID) -> list[tuple[Person, Organization]]:
    stmt = (
        select(Person, Organization)
        .join(SeriesMember, SeriesMember.person_id == Person.id)
        .join(Organization, Organization.id == Person.organization_id)
        .where(SeriesMember.series_id == series_id)
        .order_by(Organization.is_own.desc(), Person.full_name)
    )
    return [(p, o) for p, o in db.execute(stmt)]


def list_meetings(db: Session, series_id: uuid.UUID) -> list[Meeting]:
    stmt = (
        select(Meeting).where(Meeting.series_id == series_id).order_by(Meeting.planned_start.desc())
    )
    return list(db.scalars(stmt))


def get_meeting(
    db: Session, owner_id: uuid.UUID, meeting_id: uuid.UUID
) -> tuple[Meeting, Series] | None:
    row = db.execute(
        select(Meeting, Series)
        .join(Series, Series.id == Meeting.series_id)
        .where(Meeting.id == meeting_id, Series.owner_user_id == owner_id)
    ).one_or_none()
    return (row[0], row[1]) if row else None


def participants(
    db: Session, meeting_id: uuid.UUID
) -> list[tuple[MeetingParticipant, Person, Organization]]:
    stmt = (
        select(MeetingParticipant, Person, Organization)
        .join(Person, Person.id == MeetingParticipant.person_id)
        .join(Organization, Organization.id == Person.organization_id)
        .where(MeetingParticipant.meeting_id == meeting_id)
        .order_by(Organization.is_own.desc(), Person.full_name)
    )
    return [(mp, p, o) for mp, p, o in db.execute(stmt)]


def agenda(db: Session, meeting_id: uuid.UUID) -> list[AgendaItem]:
    stmt = (
        select(AgendaItem).where(AgendaItem.meeting_id == meeting_id).order_by(AgendaItem.position)
    )
    return list(db.scalars(stmt))


def get_agenda_item(db: Session, owner_id: uuid.UUID, agenda_id: uuid.UUID) -> AgendaItem | None:
    stmt = (
        select(AgendaItem)
        .join(Meeting, Meeting.id == AgendaItem.meeting_id)
        .join(Series, Series.id == Meeting.series_id)
        .where(AgendaItem.id == agenda_id, Series.owner_user_id == owner_id)
    )
    return db.scalar(stmt)


def get_person(db: Session, person_id: uuid.UUID) -> Person | None:
    return db.get(Person, person_id)


def add_organization(db: Session, organization: Organization) -> Organization:
    db.add(organization)
    db.flush()
    return organization


def add[T](db: Session, row: T) -> T:
    db.add(row)
    db.flush()
    return row


def get_organization(db: Session, owner_id: uuid.UUID, name: str) -> Organization | None:
    stmt = select(Organization).where(
        Organization.owner_user_id == owner_id, func.lower(Organization.name) == name.lower()
    )
    return db.scalar(stmt)


def get_organization_by_id(db: Session, organization_id: uuid.UUID) -> Organization:
    return db.get_one(Organization, organization_id)


def get_person_by_email(db: Session, owner_id: uuid.UUID, email: str) -> Person | None:
    return db.scalar(select(Person).where(Person.owner_user_id == owner_id, Person.email == email))


def is_member(db: Session, series_id: uuid.UUID, person_id: uuid.UUID) -> bool:
    return db.get(SeriesMember, (series_id, person_id)) is not None


def is_present(db: Session, meeting_id: uuid.UUID, person_id: uuid.UUID) -> bool:
    row = db.get(MeetingParticipant, (meeting_id, person_id))
    return row is not None and row.present


def remove_member(db: Session, series_id: uuid.UUID, person_id: uuid.UUID) -> int:
    result = db.execute(
        delete(SeriesMember).where(
            SeriesMember.series_id == series_id, SeriesMember.person_id == person_id
        )
    )
    db.flush()
    return result.rowcount


def member_ids(db: Session, series_id: uuid.UUID) -> list[uuid.UUID]:
    stmt = select(SeriesMember.person_id).where(SeriesMember.series_id == series_id)
    return list(db.scalars(stmt))


def get_participant(
    db: Session, meeting_id: uuid.UUID, person_id: uuid.UUID
) -> MeetingParticipant | None:
    return db.get(MeetingParticipant, (meeting_id, person_id))


def previous_meeting(
    db: Session, series_id: uuid.UUID, before: datetime, exclude_id: uuid.UUID
) -> Meeting | None:
    stmt = (
        select(Meeting)
        .where(
            Meeting.series_id == series_id,
            Meeting.id != exclude_id,
            Meeting.planned_start <= before,
        )
        .order_by(Meeting.planned_start.desc())
        .limit(1)
    )
    return db.scalar(stmt)


def moved_agenda(db: Session, meeting_id: uuid.UUID) -> list[AgendaItem]:
    return [a for a in agenda(db, meeting_id) if a.status == AgendaItemStatus.MOVED]


def agenda_stats(db: Session, meeting_id: uuid.UUID) -> tuple[int, int]:
    count, top = db.execute(
        select(func.count(), func.coalesce(func.max(AgendaItem.position), 0)).where(
            AgendaItem.meeting_id == meeting_id
        )
    ).one()
    return count, top


def agenda_others(db: Session, meeting_id: uuid.UUID, agenda_id: uuid.UUID) -> list[AgendaItem]:
    return [a for a in agenda(db, meeting_id) if a.id != agenda_id]


def lock_series(db: Session, series: Series) -> None:
    db.refresh(series, with_for_update=True)


def lock_meeting(db: Session, meeting: Meeting) -> None:
    db.refresh(meeting, with_for_update=True)


def get_meeting_by_id(db: Session, meeting_id: uuid.UUID) -> Meeting:
    return db.get_one(Meeting, meeting_id)


def get_series_of_meeting(db: Session, meeting_id: uuid.UUID) -> Series:
    return db.get_one(Series, db.get_one(Meeting, meeting_id).series_id)


def agenda_has_items(db: Session, agenda_id: uuid.UUID) -> bool:
    stmt = select(func.count()).select_from(Item).where(Item.agenda_item_id == agenda_id)
    return bool(db.scalar(stmt))


def uncovered_agenda(db: Session, meeting_id: uuid.UUID) -> list[AgendaItem]:
    linked = exists().where(Item.agenda_item_id == AgendaItem.id)
    stmt = (
        select(AgendaItem)
        .where(
            AgendaItem.meeting_id == meeting_id,
            AgendaItem.status == AgendaItemStatus.OPEN,
            ~linked,
        )
        .order_by(AgendaItem.position)
    )
    return list(db.scalars(stmt))


def persons(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, Person]:
    return {p.id: p for p in db.scalars(select(Person).where(Person.id.in_(ids)))}


def due_for_closure(db: Session, before: datetime) -> list[tuple[Meeting, Series]]:
    stmt = (
        select(Meeting, Series)
        .join(Series, Series.id == Meeting.series_id)
        .where(Meeting.status == MeetingStatus.LIVE, Meeting.planned_end <= before)
        .with_for_update(of=Meeting, skip_locked=True)
    )
    return [(m, s) for m, s in db.execute(stmt)]


def get_person_for_update(db: Session, person_id: uuid.UUID) -> Person | None:
    return db.scalar(select(Person).where(Person.id == person_id).with_for_update())


def lock_owner_numbering(db: Session, owner_user_id: uuid.UUID) -> None:
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(str(owner_user_id), 0))))


def count_anonymized_persons(db: Session, owner_user_id: uuid.UUID) -> int:
    stmt = (
        select(func.count())
        .select_from(Person)
        .where(Person.owner_user_id == owner_user_id, Person.anonymized_at.is_not(None))
    )
    return db.scalar(stmt) or 0


def drop_members_of_archived(db: Session, cutoff: datetime) -> list[uuid.UUID]:
    old = select(Series.id).where(
        Series.status == SeriesStatus.ARCHIVED, Series.archived_at < cutoff
    )
    rows = db.execute(
        delete(SeriesMember)
        .where(SeriesMember.series_id.in_(old))
        .returning(SeriesMember.person_id)
    )
    person_ids = list({row[0] for row in rows})
    db.flush()
    return person_ids


def orphan_person_ids(db: Session, person_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    stmt = select(Person.id).where(
        Person.id.in_(person_ids),
        Person.anonymized_at.is_(None),
        ~select(SeriesMember.person_id).where(SeriesMember.person_id == Person.id).exists(),
        ~select(Item.id).where(Item.assignee_person_id == Person.id).exists(),
    )
    return list(db.scalars(stmt))


def link_agenda_if_free(
    db: Session, meeting_id: uuid.UUID, item_id: uuid.UUID, topic_id: uuid.UUID
) -> bool:
    """Один UPDATE с условием "связи ещё нет": связь, поставленную ведущим, не затираем."""
    result = db.execute(
        update(Item)
        .where(Item.id == item_id, Item.meeting_id == meeting_id, Item.agenda_item_id.is_(None))
        .values(agenda_item_id=topic_id)
    )
    db.flush()
    return result.rowcount == 1
