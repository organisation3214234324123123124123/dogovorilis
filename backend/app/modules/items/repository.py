import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime

from sqlalchemy import Select, func, select, text, update
from sqlalchemy import delete as sql_delete
from sqlalchemy.orm import Session

from app.modules.auth.models import User
from app.modules.gaps.models import Gap
from app.modules.items.models import OPEN_STATUSES, Item, ItemComment, ItemEvent, ItemStatus
from app.modules.series.models import (
    Meeting,
    MeetingParticipant,
    Person,
    Series,
    SeriesStatus,
)


def get_owned(
    db: Session, owner_id: uuid.UUID, item_id: uuid.UUID, *, lock: bool = False
) -> tuple[Item, Series, Meeting] | None:
    stmt = (
        select(Item, Series, Meeting)
        .join(Series, Series.id == Item.series_id)
        .join(Meeting, Meeting.id == Item.meeting_id)
        .where(Item.id == item_id, Series.owner_user_id == owner_id)
    )
    if lock:
        stmt = stmt.with_for_update(of=Item)
    row = db.execute(stmt).one_or_none()
    return (row[0], row[1], row[2]) if row else None


def get_locked(db: Session, item_id: uuid.UUID) -> tuple[Item, Series] | None:
    stmt = (
        select(Item, Series)
        .join(Series, Series.id == Item.series_id)
        .where(Item.id == item_id)
        .with_for_update(of=Item)
    )
    row = db.execute(stmt).one_or_none()
    return (row[0], row[1]) if row else None


def pending_count(db: Session, meeting_id: uuid.UUID, person_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(
        Item.meeting_id == meeting_id,
        Item.assignee_person_id == person_id,
        Item.status == ItemStatus.PENDING,
    )
    return db.scalar(stmt) or 0


def add_comment(db: Session, comment: ItemComment) -> ItemComment:
    db.add(comment)
    db.flush()
    return comment


def open_gaps(db: Session, item_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[Gap]]:
    result: dict[uuid.UUID, list[Gap]] = defaultdict(list)
    ids = list(item_ids)
    if not ids:
        return result
    stmt = (
        select(Gap)
        .where(Gap.item_id.in_(ids), Gap.resolved_at.is_(None), Gap.dismissed_at.is_(None))
        .order_by(Gap.detected_at)
    )
    for gap in db.scalars(stmt):
        result[gap.item_id].append(gap)
    return result


def persons(db: Session, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, Person]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return {p.id: p for p in db.scalars(select(Person).where(Person.id.in_(wanted)))}


def present_pairs(db: Session, items: Iterable[Item]) -> set[tuple[uuid.UUID, uuid.UUID]]:
    meeting_ids = {i.meeting_id for i in items if i.assignee_person_id}
    if not meeting_ids:
        return set()
    stmt = select(MeetingParticipant.meeting_id, MeetingParticipant.person_id).where(
        MeetingParticipant.meeting_id.in_(meeting_ids), MeetingParticipant.present.is_(True)
    )
    return {(m, p) for m, p in db.execute(stmt)}


def users(db: Session, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, User]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return {u.id: u for u in db.scalars(select(User).where(User.id.in_(wanted)))}


def list_for_meeting(db: Session, meeting_id: uuid.UUID, *, lock: bool = False) -> list[Item]:
    """С lock=True строки берутся FOR UPDATE, чтобы параллельный commit не затёр правку."""
    stmt = select(Item).where(Item.meeting_id == meeting_id).order_by(Item.created_at, Item.id)
    if lock:
        stmt = stmt.with_for_update(of=Item).execution_options(populate_existing=True)
    return list(db.scalars(stmt))


def _series_filter(
    series_id: uuid.UUID,
    status: list[ItemStatus] | None,
    assignee_person_id: uuid.UUID | None,
    meeting_id: uuid.UUID | None,
    overdue_before: date | None,
) -> Select:
    stmt = select(Item).where(Item.series_id == series_id)
    if status:
        stmt = stmt.where(Item.status.in_(status))
    if assignee_person_id:
        stmt = stmt.where(Item.assignee_person_id == assignee_person_id)
    if meeting_id:
        stmt = stmt.where(Item.meeting_id == meeting_id)
    if overdue_before:
        stmt = stmt.where(Item.status.in_(OPEN_STATUSES), Item.due_date < overdue_before)
    return stmt


def list_for_series(
    db: Session,
    series_id: uuid.UUID,
    *,
    status: list[ItemStatus] | None,
    assignee_person_id: uuid.UUID | None,
    meeting_id: uuid.UUID | None,
    overdue_before: date | None,
    limit: int,
    offset: int,
) -> tuple[list[Item], int]:
    stmt = _series_filter(series_id, status, assignee_person_id, meeting_id, overdue_before)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page = stmt.order_by(Item.created_at.desc(), Item.id).limit(limit).offset(offset)
    return list(db.scalars(page)), total


def on_time_counts(
    db: Session,
    series_id: uuid.UUID,
    tz: str,
    *,
    due_from: date,
    due_to: date,
    assignee_person_id: uuid.UUID | None,
) -> tuple[int, int]:
    """(сделано в срок, всего) по отправленным пунктам со сроком в [due_from, due_to]; снятые не
    считаются (FR-44).
    """
    done_day = func.date(func.timezone(tz, Item.done_at))
    on_time = (Item.status == ItemStatus.DONE) & (done_day <= Item.due_date)
    stmt = select(func.count().filter(on_time), func.count()).where(
        Item.series_id == series_id,
        Item.due_date.between(due_from, due_to),
        Item.status.not_in((ItemStatus.DRAFT, ItemStatus.CANCELLED)),
    )
    if assignee_person_id:
        stmt = stmt.where(Item.assignee_person_id == assignee_person_id)
    done, total = db.execute(stmt).one()
    return done, total


def events(db: Session, item_id: uuid.UUID) -> list[ItemEvent]:
    stmt = (
        select(ItemEvent)
        .where(ItemEvent.item_id == item_id)
        .order_by(ItemEvent.created_at, ItemEvent.id)
    )
    return list(db.scalars(stmt))


def last_comment(db: Session, item_id: uuid.UUID) -> ItemComment | None:
    stmt = (
        select(ItemComment)
        .where(ItemComment.item_id == item_id)
        .order_by(ItemComment.created_at.desc())
        .limit(1)
    )
    return db.scalar(stmt)


def add(db: Session, item: Item) -> Item:
    db.add(item)
    db.flush()
    return item


def delete(db: Session, item: Item) -> None:
    db.delete(item)
    db.flush()


def open_load(
    db: Session,
    owner_id: uuid.UUID,
    person_id: uuid.UUID,
    *,
    due_from: date,
    due_to: date,
    exclude_item_id: uuid.UUID,
) -> int:
    stmt = (
        select(func.count())
        .select_from(Item)
        .join(Series, Series.id == Item.series_id)
        .where(
            Series.owner_user_id == owner_id,
            Item.assignee_person_id == person_id,
            Item.status.in_(OPEN_STATUSES),
            Item.due_date.between(due_from, due_to),
            Item.id != exclude_item_id,
        )
    )
    return db.scalar(stmt) or 0


def open_counts(
    db: Session, person_ids: Iterable[uuid.UUID], *, due_from: date, due_to: date
) -> dict[uuid.UUID, int]:
    """То же для списка людей, но по сериям ведущего, которому они принадлежат (BR-9)."""
    ids = list(person_ids)
    if not ids:
        return {}
    stmt = (
        select(Item.assignee_person_id, func.count())
        .join(Series, Series.id == Item.series_id)
        .join(Person, Person.id == Item.assignee_person_id)
        .where(
            Item.assignee_person_id.in_(ids),
            Series.owner_user_id == Person.owner_user_id,
            Item.status.in_(OPEN_STATUSES),
            Item.due_date.between(due_from, due_to),
        )
        .group_by(Item.assignee_person_id)
    )
    return {person_id: count for person_id, count in db.execute(stmt)}


def open_counts_by_series(db: Session, series_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    stmt = (
        select(Item.series_id, func.count())
        .where(Item.series_id.in_(series_ids), Item.status.in_(OPEN_STATUSES))
        .group_by(Item.series_id)
    )
    return {sid: n for sid, n in db.execute(stmt)}


def add_event(db: Session, event: ItemEvent) -> ItemEvent:
    db.add(event)
    db.flush()
    return event


def lock(db: Session, item: Item) -> None:
    db.refresh(item, with_for_update=True)


def review_open(db: Session, series_id: uuid.UUID) -> list[Item]:
    stmt = (
        select(Item)
        .where(Item.series_id == series_id, Item.status.in_((*OPEN_STATUSES, ItemStatus.DISPUTED)))
        .order_by(Item.due_date.asc().nulls_last(), Item.created_at, Item.id)
    )
    return list(db.scalars(stmt))


def review_done(db: Session, series_id: uuid.UUID, since: datetime | None) -> list[Item]:
    stmt = select(Item).where(Item.series_id == series_id, Item.status == ItemStatus.DONE)
    if since is not None:
        stmt = stmt.where(Item.done_at >= since)
    return list(db.scalars(stmt.order_by(Item.done_at, Item.id)))


def overdue_for_series(db: Session, series_id: uuid.UUID, before: date) -> list[Item]:
    stmt = select(Item).where(
        Item.series_id == series_id, Item.status.in_(OPEN_STATUSES), Item.due_date < before
    )
    return list(db.scalars(stmt))


def active_series_timezones(db: Session) -> dict[str, list[uuid.UUID]]:
    result: dict[str, list[uuid.UUID]] = defaultdict(list)
    stmt = select(Series.timezone, Series.id).where(Series.status == SeriesStatus.ACTIVE)
    for tz, series_id in db.execute(stmt):
        result[tz].append(series_id)
    return result


def capture_window(
    db: Session, meeting_id: uuid.UUID, window_sec: int
) -> tuple[int, datetime | None, datetime]:
    """Лочит строку встречи, чтобы счётчик для лимита (NFR-10) не обгоняли параллельные запросы.
    Отдаёт число решений за окно, время самого старого и now по часам БД.
    """
    db.execute(select(Meeting.id).where(Meeting.id == meeting_id).with_for_update(key_share=True))
    now = func.clock_timestamp()
    row = db.execute(
        select(func.count(), func.min(Item.created_at), now).where(
            Item.meeting_id == meeting_id,
            Item.created_at > now - func.make_interval(0, 0, 0, 0, 0, 0, window_sec),
        )
    ).one()
    return row[0], row[1], row[2]


def clear_person_comments(db: Session, person_id: uuid.UUID, now: datetime) -> int:
    result = db.execute(
        update(ItemComment)
        .where(ItemComment.author_person_id == person_id, ItemComment.anonymized_at.is_(None))
        .values(text=None, anonymized_at=now)
    )
    db.flush()
    return result.rowcount


def delete_events_before(db: Session, cutoff: datetime, limit: int) -> int:
    db.execute(text("SELECT set_config('app.retention_cleanup', 'on', true)"))
    result = db.execute(
        sql_delete(ItemEvent).where(
            ItemEvent.id.in_(select(ItemEvent.id).where(ItemEvent.created_at < cutoff).limit(limit))
        )
    )
    db.flush()
    return result.rowcount


def delete_items_of_archived_series(db: Session, cutoff: datetime, limit: int) -> int:
    ids = (
        select(Item.id)
        .join(Series, Series.id == Item.series_id)
        .where(Series.status == SeriesStatus.ARCHIVED, Series.archived_at < cutoff)
        .limit(limit)
    )
    result = db.execute(sql_delete(Item).where(Item.id.in_(ids)))
    db.flush()
    return result.rowcount
