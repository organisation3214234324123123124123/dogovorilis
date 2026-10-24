import math
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.business_days import add_business_days
from app.core.errors import (
    Conflict,
    Forbidden,
    NotFound,
    PreconditionRequired,
    RateLimited,
    ValidationFailed,
)
from app.modules.analytics import service as analytics_service
from app.modules.auth.models import User
from app.modules.confirm import service as confirm_service
from app.modules.gaps import service as gaps_service
from app.modules.gaps.models import BLOCKING_GAPS, Gap
from app.modules.items import repository
from app.modules.items.models import (
    OPEN_STATUSES,
    ActorType,
    CommentKind,
    DoneBy,
    Item,
    ItemAction,
    ItemComment,
    ItemEvent,
    ItemStatus,
)
from app.modules.items.parsing import parse_line
from app.modules.items.schemas import (
    AcceptInMeetingRequest,
    AssigneeResponse,
    GapRead,
    ItemActionRequest,
    ItemCard,
    ItemCreate,
    ItemEventRead,
    ItemPage,
    ItemRead,
    ItemUpdate,
    OverloadWarning,
    PersonBrief,
    ReassignAction,
    RescheduleAction,
)
from app.modules.live import service as live_service
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.notifications.service import CHANGED_FIELDS
from app.modules.series import service as series_service
from app.modules.series.models import (
    LOAD_WARNING_THRESHOLD_DEFAULT,
    Meeting,
    MeetingStatus,
    Person,
    Series,
)

ON_TIME_WINDOW_DAYS = 30
OVERLOAD_WINDOW_DAYS = 7
CAPTURE_LIMIT = 60
CAPTURE_WINDOW_SEC = 60
EDITABLE_FIELDS = ("what", "assignee_person_id", "due_date", "due_text", "done_criteria")
PARSED_FIELDS = ("what", "assignee_person_id", "due_date", "due_text")

# Точка расширения для LLM (FR-12): вызывается после разбора правилами, до пересчёта пробелов.
# Обогатитель заполняет только пустые поля и не трогает item.manual_fields (BR-15).
ENRICHERS: list[Callable[[Session, Item], None]] = []
STALE_PENDING_BUSINESS_DAYS = 2
OVERDUE_JOB_FROM_MINUTE = 5
OVERDUE_JOB_TO_MINUTE = 60


def today_in(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def is_overdue(item: Item, today: date) -> bool:
    return item.status in OPEN_STATUSES and item.due_date is not None and item.due_date < today


def _gap_read(gap: Gap) -> GapRead:
    return GapRead.model_validate(gap).model_copy(update={"blocking": gap.type in BLOCKING_GAPS})


def accept_blocker(item: Item, gaps: list[Gap], present: bool) -> str | None:
    """Почему нельзя отметить "Согласовано" (BR-3), либо None."""
    if item.status != ItemStatus.DRAFT:
        return "Пункт уже разослан"
    if item.assignee_person_id is None:
        return "Не назначен исполнитель"
    if not present:
        return "Исполнитель отсутствует на встрече"
    if item.due_date is None:
        return "Нужен срок-дата"
    if any(g.type in BLOCKING_GAPS for g in gaps):
        return "Есть открытые пробелы"
    return None


def _read(
    item: Item,
    today: date,
    persons: dict[uuid.UUID, Person],
    gaps: list[Gap],
    loads: dict[uuid.UUID, int],
    present: bool,
) -> dict:
    person = persons.get(item.assignee_person_id) if item.assignee_person_id else None
    data = {c: getattr(item, c) for c in ItemRead.model_fields if hasattr(item, c)}
    data.update(
        assignee=(
            PersonBrief(
                id=person.id, full_name=person.full_name, open_items=loads.get(person.id, 0)
            )
            if person
            else None
        ),
        is_overdue=is_overdue(item, today),
        gaps=[_gap_read(g) for g in gaps],
        accept_blocked_reason=(reason := accept_blocker(item, gaps, present)),
        can_accept_in_meeting=reason is None,
    )
    return data


def to_reads(db: Session, items: list[Item], tz: str) -> list[ItemRead]:
    today = today_in(tz)
    persons = repository.persons(db, (i.assignee_person_id for i in items))
    gaps = repository.open_gaps(db, (i.id for i in items))
    loads = open_counts(db, persons, today)
    here = repository.present_pairs(db, items)
    return [
        ItemRead(
            **_read(
                i,
                today,
                persons,
                gaps.get(i.id, []),
                loads,
                (i.meeting_id, i.assignee_person_id) in here,
            )
        )
        for i in items
    ]


def open_counts_by_series(db: Session, series_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    return repository.open_counts_by_series(db, series_ids)


def open_counts(db: Session, person_ids: Iterable[uuid.UUID], today: date) -> dict[uuid.UUID, int]:
    return repository.open_counts(
        db, person_ids, due_from=today, due_to=today + timedelta(days=OVERLOAD_WINDOW_DAYS)
    )


def _owned_writable(
    db: Session, user: User, item_id: uuid.UUID, *, lock: bool = False
) -> tuple[Item, Series, Meeting]:
    """Как _owned, но для правок: архивная серия только на чтение."""
    found = _owned(db, user, item_id, lock=lock)
    series_service.archived_guard(found[1])
    return found


def _owned(
    db: Session, user: User, item_id: uuid.UUID, *, lock: bool = False
) -> tuple[Item, Series, Meeting]:
    found = repository.get_owned(db, user.id, item_id, lock=lock)
    if found is None:
        raise NotFound("Пункт не найден")
    return found


def _last_response(db: Session, item: Item, events: list) -> AssigneeResponse | None:
    answers = [
        e
        for e in events
        if e.actor_type == ActorType.ASSIGNEE
        and e.action in (ItemAction.RESCHEDULED, ItemAction.DISPUTED)
    ]
    comment = repository.last_comment(db, item.id)
    if not answers and comment is None:
        return None
    if answers:
        event = answers[-1]
        kind = (
            CommentKind.RESCHEDULE
            if event.action == ItemAction.RESCHEDULED
            else CommentKind.NOT_MINE
        )
        text = comment.text if comment and comment.kind == kind else None
        due = event.data.get("due_date")
        return AssigneeResponse(kind=kind, text=text, due_date=due, at=event.created_at)
    assert comment is not None
    return AssigneeResponse(kind=comment.kind, text=comment.text, at=comment.created_at)


def get_card(db: Session, user: User, item_id: uuid.UUID) -> ItemCard:
    item, series, meeting = _owned(db, user, item_id)
    read = to_reads(db, [item], series.timezone)[0]
    return ItemCard(
        **read.model_dump(),
        series_title=series.title,
        meeting_planned_start=meeting.planned_start,
        last_response=_last_response(db, item, repository.events(db, item.id)),
    )


def history(db: Session, user: User, item_id: uuid.UUID) -> list[ItemEventRead]:
    _owned(db, user, item_id)
    events = repository.events(db, item_id)
    users = repository.users(db, (e.actor_user_id for e in events))
    persons = repository.persons(db, (e.actor_person_id for e in events))

    def actor(e) -> str | None:
        if e.actor_user_id in users:
            return users[e.actor_user_id].full_name
        if e.actor_person_id in persons:
            return persons[e.actor_person_id].full_name
        return user.full_name if e.actor_type == ActorType.HOST else None

    return [
        ItemEventRead.model_validate(e).model_copy(update={"actor_name": actor(e)}) for e in events
    ]


def list_meeting_items(db: Session, meeting_id: uuid.UUID, tz: str) -> list[ItemRead]:
    return to_reads(db, repository.list_for_meeting(db, meeting_id), tz)


def list_series_items(
    db: Session,
    user: User,
    series_id: uuid.UUID,
    *,
    status: list[ItemStatus] | None,
    assignee_person_id: uuid.UUID | None,
    meeting_id: uuid.UUID | None,
    overdue: bool,
    limit: int,
    offset: int,
) -> ItemPage:
    series = series_service.get_owned_series(db, user, series_id)
    today = today_in(series.timezone)
    items, total = repository.list_for_series(
        db,
        series.id,
        status=status,
        assignee_person_id=assignee_person_id,
        meeting_id=meeting_id,
        overdue_before=today if overdue else None,
        limit=limit,
        offset=offset,
    )
    on_time, due_total = repository.on_time_counts(
        db,
        series.id,
        series.timezone,
        due_from=today - timedelta(days=ON_TIME_WINDOW_DAYS - 1),
        due_to=today,
        assignee_person_id=assignee_person_id,
    )
    return ItemPage(
        items=to_reads(db, items, series.timezone),
        total=total,
        limit=limit,
        offset=offset,
        on_time_share_30d=round(on_time / due_total, 4) if due_total else None,
    )


def parse_if_match(value: str | None) -> int:
    if value is None:
        raise PreconditionRequired("Нужен заголовок If-Match с версией пункта")
    try:
        return int(value.removeprefix("W/").strip('"'))
    except ValueError as exc:
        raise ValidationFailed("If-Match должен содержать номер версии") from exc


def _emit(
    db: Session,
    meeting_id: uuid.UUID,
    kind: str,
    item: Item | None = None,
    data: object = None,
) -> None:
    live_service.publish_after_commit(
        db,
        meeting_id,
        {
            "type": kind,
            "meeting_id": str(meeting_id),
            "item_id": str(item.id) if item else None,
            "version": item.version if item else None,
            "data": data if data is not None else {},
        },
    )


def _one(db: Session, item: Item, tz: str) -> ItemRead:
    return to_reads(db, [item], tz)[0]


def _journal(db: Session, user: User, item: Item, action: ItemAction, **data: object) -> None:
    repository.add_event(
        db,
        ItemEvent(
            item_id=item.id,
            actor_type=ActorType.HOST,
            actor_user_id=user.id,
            action=action,
            data={"version": item.version, "status": item.status.value, **data},
        ),
    )


def _ids(user: User, item: Item) -> dict:
    return {
        "user_id": user.id,
        "series_id": item.series_id,
        "meeting_id": item.meeting_id,
        "item_id": item.id,
    }


def _assignee_name(db: Session, item: Item) -> str | None:
    person = repository.persons(db, [item.assignee_person_id]).get(item.assignee_person_id)
    return person.full_name.split()[0] if person else None


def _check_rate(db: Session, meeting_id: uuid.UUID) -> None:
    """Не больше 60 решений в минуту на встречу (NFR-10); считаем по строкам в БД."""
    count, oldest, now = repository.capture_window(db, meeting_id, CAPTURE_WINDOW_SEC)
    if count >= CAPTURE_LIMIT and oldest is not None:
        wait = CAPTURE_WINDOW_SEC - (now - oldest).total_seconds()
        raise RateLimited("Слишком много решений за минуту", retry_after=math.ceil(wait))


def _warn_overload(
    db: Session, user: User, series: Series, item: Item, today: date
) -> OverloadWarning | None:
    """Только предупреждение, назначение не блокируем (BR-9)."""
    if item.assignee_person_id is None:
        return None
    load = repository.open_load(
        db,
        user.id,
        item.assignee_person_id,
        due_from=today,
        due_to=today + timedelta(days=OVERLOAD_WINDOW_DAYS),
        exclude_item_id=item.id,
    )
    threshold = series.load_warning_threshold or LOAD_WARNING_THRESHOLD_DEFAULT
    if load < threshold:
        return None
    analytics_service.track(db, "overload_warning_shown", open_items=load, **_ids(user, item))
    return OverloadWarning(open_items=load, threshold=threshold)


def _accept_allowed(db: Session, item: Item) -> bool:
    """ "Согласовано на встрече" возможно, если исполнитель присутствует, есть срок-дата и нет
    открытых блокирующих пробелов.
    """
    return (
        item.assignee_person_id is not None
        and item.due_date is not None
        and series_service.is_present(db, item.meeting_id, item.assignee_person_id)
        and not gaps_service.has_open(db, item, BLOCKING_GAPS)
    )


def create_item(db: Session, user: User, meeting_id: uuid.UUID, body: ItemCreate) -> ItemRead:
    meeting, series = series_service.get_owned_meeting(db, user, meeting_id)
    series_service.archived_guard(series)
    if meeting.status == MeetingStatus.PLANNED:
        raise Conflict("Встреча ещё не началась", code="MEETING_NOT_LIVE")
    if body.agenda_item_id and not series_service.has_agenda_item(
        db, meeting.id, body.agenda_item_id
    ):
        raise ValidationFailed("Пункт повестки не из этой встречи")
    _check_rate(db, meeting.id)
    today = today_in(series.timezone)

    item = repository.add(
        db,
        Item(
            meeting_id=meeting.id,
            series_id=series.id,
            agenda_item_id=body.agenda_item_id,
            raw_text=body.raw_text,
            manual_fields=[],
        ),
    )
    _journal(db, user, item, ItemAction.CREATED)
    _emit(
        db,
        meeting.id,
        "item.created",
        item,
        _one(db, item, series.timezone).model_dump(mode="json"),
    )

    parsed = parse_line(
        body.raw_text, today, series_service.member_persons(db, series.id), item.manual_fields
    )
    filled = [f for f in PARSED_FIELDS if getattr(parsed, f) is not None]
    for field in filled:
        setattr(item, field, getattr(parsed, field))
    for enrich in ENRICHERS:
        enrich(db, item)
    db.flush()
    gaps_service.recalc(db, item, today, _assignee_name(db, item))
    analytics_service.track(
        db, "decision_captured", source="manual", fields_filled=filled, **_ids(user, item)
    )
    warning = _warn_overload(db, user, series, item, today)

    read = _one(db, item, series.timezone)
    _emit(db, meeting.id, "item.updated", item, read.model_dump(mode="json"))
    _emit(db, meeting.id, "gaps.updated", item, [g.model_dump(mode="json") for g in read.gaps])
    read.overload_warning = warning
    return read


def _check_version(item: Item, if_match: str | None) -> None:
    if parse_if_match(if_match) != item.version:
        raise Conflict("Пункт уже изменён, обновите карточку", code="VERSION_CONFLICT")


def update_item(
    db: Session, user: User, item_id: uuid.UUID, if_match: str | None, body: ItemUpdate
) -> ItemRead:
    item, series, meeting = _owned_writable(db, user, item_id, lock=True)
    _check_version(item, if_match)
    if item.status in (ItemStatus.DONE, ItemStatus.CANCELLED):
        raise Conflict("Пункт закрыт, править нельзя", code="INVALID_TRANSITION")
    sent = item.status != ItemStatus.DRAFT
    today = today_in(series.timezone)

    provided = [f for f in EDITABLE_FIELDS if f in body.model_fields_set]
    assignee_changes = (
        "assignee_person_id" in provided and body.assignee_person_id != item.assignee_person_id
    )
    if "assignee_person_id" in provided and not assignee_changes:
        provided.remove("assignee_person_id")
    if assignee_changes:
        if sent:
            raise ValidationFailed(
                "После рассылки исполнителя меняют через «Переназначить»", code="USE_REASSIGN"
            )
        new = body.assignee_person_id
        if new is not None and not series_service.is_member(db, series.id, new):
            raise ValidationFailed("Исполнитель не участник серии", code="NOT_A_PARTICIPANT")
    if body.due_date is not None and body.due_date != item.due_date and body.due_date < today:
        raise ValidationFailed("Срок раньше сегодняшнего дня", code="DUE_IN_PAST")

    old = {f: getattr(item, f) for f in provided}
    changed = [f for f in provided if getattr(body, f) != old[f]]
    if not changed:
        return _one(db, item, series.timezone)
    for field in provided:
        setattr(item, field, getattr(body, field))
    item.manual_fields = list(dict.fromkeys([*item.manual_fields, *provided]))
    if assignee_changes:
        item.accepted_in_meeting = False
    due_changed = "due_date" in changed
    if sent and due_changed:
        item.reschedule_count += 1
    item.version += 1
    db.flush()

    gaps_service.recalc(db, item, today, _assignee_name(db, item))
    if item.accepted_in_meeting and not _accept_allowed(db, item):
        item.accepted_in_meeting = False
    _journal(db, user, item, ItemAction.UPDATED, fields=changed)
    analytics_service.track(db, "item_edited", fields=changed, after_send=sent, **_ids(user, item))
    if sent:
        notifications_service.queue_item_changed(
            db, item, {f: old[f] for f in changed if f in CHANGED_FIELDS}
        )
        if due_changed:
            notifications_service.replan_due_tomorrow(db, item, series.timezone)
            if item.assignee_person_id:
                confirm_service.recalc_link_expiry(db, meeting.id, item.assignee_person_id)
    warning = _warn_overload(db, user, series, item, today) if assignee_changes else None

    read = _one(db, item, series.timezone)
    _emit(db, meeting.id, "item.updated", item, read.model_dump(mode="json"))
    _emit(db, meeting.id, "gaps.updated", item, [g.model_dump(mode="json") for g in read.gaps])
    read.overload_warning = warning
    return read


def delete_item(db: Session, user: User, item_id: uuid.UUID, if_match: str | None) -> None:
    item, _, meeting = _owned_writable(db, user, item_id, lock=True)
    _check_version(item, if_match)
    if item.status != ItemStatus.DRAFT:
        raise Conflict(
            "Удалить можно только пункт в draft, дальше — «Снять»", code="INVALID_TRANSITION"
        )
    repository.delete(db, item)
    _emit(db, meeting.id, "item.deleted", item)


def dismiss_gap(db: Session, user: User, item_id: uuid.UUID, gap_id: uuid.UUID) -> ItemRead:
    item, series, meeting = _owned_writable(db, user, item_id)
    gap = gaps_service.get_for_item(db, item.id, gap_id)
    if gap is None:
        raise NotFound("Пробел не найден")
    if gaps_service.dismiss(db, item, gap):
        read = _one(db, item, series.timezone)
        _emit(db, meeting.id, "gaps.updated", item, [g.model_dump(mode="json") for g in read.gaps])
        return read
    return _one(db, item, series.timezone)


def accept_in_meeting(
    db: Session,
    user: User,
    item_id: uuid.UUID,
    if_match: str | None,
    body: AcceptInMeetingRequest,
) -> ItemRead:
    item, series, meeting = _owned_writable(db, user, item_id, lock=True)
    if meeting.status == MeetingStatus.SENT:
        raise Conflict("Встреча уже разослана", code="ALREADY_SENT")
    _check_version(item, if_match)
    if item.status != ItemStatus.DRAFT:
        raise Conflict("Пункт уже разослан", code="INVALID_TRANSITION")
    if body.value and not _accept_allowed(db, item):
        raise ValidationFailed(
            "Нужны присутствующий исполнитель, срок-дата и закрытые пробелы", code="GAPS_OPEN"
        )
    if item.accepted_in_meeting != body.value:
        item.accepted_in_meeting = body.value
        item.version += 1
        db.flush()
        _journal(db, user, item, ItemAction.ACCEPTED_IN_MEETING, value=body.value)
        if body.value:
            analytics_service.track(
                db,
                "accepted_in_meeting",
                due_changed="due_date" in item.manual_fields,
                **_ids(user, item),
            )
    read = _one(db, item, series.timezone)
    _emit(db, meeting.id, "item.updated", item, read.model_dump(mode="json"))
    return read


def _review_group(item: Item, today: date, tz: str) -> str | None:
    if item.status == ItemStatus.PENDING and item.assignee_person_id is None:
        return "unassigned"
    if is_overdue(item, today):
        return "overdue"
    if item.status == ItemStatus.DISPUTED:
        return "disputed"
    if item.status == ItemStatus.PENDING and item.sent_at is not None:
        sent_on = item.sent_at.astimezone(ZoneInfo(tz)).date()
        if add_business_days(sent_on, STALE_PENDING_BUSINESS_DAYS) <= today:
            return "stale_pending"
    return None


def review_groups(db: Session, series: Series, since: datetime | None) -> dict[str, list[ItemRead]]:
    """Группы сверки (FR-42). Каждый пункт попадает в первую подходящую группу; since - начало
    предыдущей встречи.
    """
    tz = series.timezone
    today = today_in(tz)
    groups: dict[str, list[Item]] = {
        "unassigned": [],
        "overdue": [],
        "disputed": [],
        "stale_pending": [],
    }
    for item in repository.review_open(db, series.id):
        group = _review_group(item, today, tz)
        if group:
            groups[group].append(item)
    groups["done_since_last"] = repository.review_done(db, series.id, since)
    return {name: to_reads(db, items, tz) for name, items in groups.items()}


def emit_overdue_events(db: Session, now: datetime) -> int:
    """С 00:05 по поясу серии пишем item_overdue по просроченному пункту (FR-41).

    В день D просрочены пункты с due < D, как is_overdue в API; days_overdue = D - due. Задача
    идёт каждые 5 минут до 00:59, повтор безопасен: дубли отсекаются по дню.
    """
    emitted = 0
    for tz, series_ids in repository.active_series_timezones(db).items():
        zone = ZoneInfo(tz)
        local = now.astimezone(zone)
        if not OVERDUE_JOB_FROM_MINUTE <= local.minute < OVERDUE_JOB_TO_MINUTE or local.hour:
            continue
        reported = local.date()
        day_start = datetime.combine(local.date(), time.min, zone)
        for series_id in series_ids:
            for item in repository.overdue_for_series(db, series_id, reported):
                if analytics_service.was_tracked_since(db, "item_overdue", item.id, day_start):
                    continue
                analytics_service.track(
                    db,
                    "item_overdue",
                    series_id=series_id,
                    meeting_id=item.meeting_id,
                    item_id=item.id,
                    days_overdue=(reported - item.due_date).days,
                )
                emitted += 1
    return emitted


def _require_status(item: Item, allowed: tuple[ItemStatus, ...]) -> None:
    if item.status not in allowed:
        raise Conflict(f"Из статуса {item.status.value} так нельзя", code="INVALID_TRANSITION")


def _journal_review(
    db: Session, user: User, item: Item, action: ItemAction, status_from: ItemStatus, **data
) -> None:
    repository.add_event(
        db,
        ItemEvent(
            item_id=item.id,
            actor_type=ActorType.HOST,
            actor_user_id=user.id,
            action=action,
            data={
                "status_from": status_from.value,
                "status_to": item.status.value,
                **{k: v.isoformat() if isinstance(v, date) else str(v) for k, v in data.items()},
            },
        ),
    )


def _notify_changed(db: Session, item: Item, now: datetime) -> None:
    if item.assignee_person_id is None:
        return
    notifications_service.queue(
        db,
        NotificationType.ITEM_CHANGED,
        item_id=item.id,
        meeting_id=item.meeting_id,
        person_id=item.assignee_person_id,
        dedupe_key=f"{item.id}:ITEM_CHANGED:{item.version}",
        scheduled_for=now,
    )


def _plan_reminder(db: Session, item: Item, tz: str, now: datetime) -> None:
    if item.assignee_person_id is None:
        return
    notifications_service.plan_due_reminder(
        db,
        item_id=item.id,
        meeting_id=item.meeting_id,
        person_id=item.assignee_person_id,
        due_date=item.due_date,
        version=item.version,
        tz=tz,
        now=now,
    )


def _do_done(db: Session, user: User, item: Item, series: Series, now: datetime) -> ItemAction:
    _require_status(item, tuple(OPEN_STATUSES))
    item.status = ItemStatus.DONE
    item.done_at = now
    item.done_by = DoneBy.HOST
    notifications_service.cancel_due_reminder(db, item.id)
    analytics_service.track(
        db,
        "item_done",
        user_id=user.id,
        series_id=series.id,
        meeting_id=item.meeting_id,
        item_id=item.id,
        on_time=item.due_date is None or today_in(series.timezone) <= item.due_date,
        by=DoneBy.HOST.value,
    )
    return ItemAction.DONE


def _do_reschedule(
    db: Session, item: Item, series: Series, now: datetime, body: RescheduleAction
) -> ItemAction:
    _require_status(item, tuple(OPEN_STATUSES))
    if body.due_date < today_in(series.timezone):
        raise ValidationFailed("Срок не может быть в прошлом", code="DUE_IN_PAST")
    item.due_date = body.due_date
    item.reschedule_count += 1
    return ItemAction.RESCHEDULED


def _do_cancel(db: Session, item: Item, now: datetime) -> ItemAction:
    _require_status(item, (*OPEN_STATUSES, ItemStatus.DISPUTED))
    item.status = ItemStatus.CANCELLED
    item.cancelled_at = now
    notifications_service.cancel_due_reminder(db, item.id)
    return ItemAction.CANCELLED


def _do_reassign(
    db: Session, item: Item, series: Series, now: datetime, body: ReassignAction
) -> ItemAction:
    _require_status(item, (*OPEN_STATUSES, ItemStatus.DISPUTED))
    if not series_service.is_member(db, series.id, body.assignee_person_id):
        raise ValidationFailed("Исполнитель должен быть участником серии", code="NOT_A_PARTICIPANT")
    item.assignee_person_id = body.assignee_person_id
    item.status = ItemStatus.PENDING
    item.accepted_in_meeting = False
    item.accepted_at = None
    item.sent_at = now
    return ItemAction.REASSIGNED


def _do_reopen(item: Item) -> ItemAction:
    _require_status(item, (ItemStatus.DONE,))
    item.status = ItemStatus.ACCEPTED
    item.done_at = None
    item.done_by = None
    return ItemAction.REOPENED


def apply_action(
    db: Session, user: User, item_id: uuid.UUID, if_match: str | None, body: ItemActionRequest
) -> ItemRead:
    item, series, _ = _owned_writable(db, user, item_id)
    version = parse_if_match(if_match)
    repository.lock(db, item)
    if item.version != version:
        raise Conflict("Пункт уже изменён, обновите страницу", code="VERSION_CONFLICT")
    now = datetime.now(UTC)
    status_from, assignee_from, due_from = item.status, item.assignee_person_id, item.due_date
    if body.action == "done":
        action = _do_done(db, user, item, series, now)
    elif body.action == "reschedule":
        action = _do_reschedule(db, item, series, now, body)
    elif body.action == "cancel":
        action = _do_cancel(db, item, now)
    elif body.action == "reassign":
        action = _do_reassign(db, item, series, now, body)
    else:
        action = _do_reopen(item)
    item.version += 1
    db.flush()
    extra = {}
    if action == ItemAction.RESCHEDULED:
        notifications_service.cancel_due_reminder(db, item.id)
        _plan_reminder(db, item, series.timezone, now)
        _notify_changed(db, item, now)
        extra = {"due_from": due_from, "due_to": item.due_date}
    elif action == ItemAction.REOPENED:
        _notify_changed(db, item, now)
    elif action == ItemAction.REASSIGNED:
        _hand_over(db, item, series, assignee_from, now)
        extra = {"assignee_from": assignee_from, "assignee_to": item.assignee_person_id}
    _journal_review(db, user, item, action, status_from, **extra)
    if action != ItemAction.REOPENED:
        analytics_service.track(
            db,
            "review_action",
            user_id=user.id,
            series_id=series.id,
            meeting_id=item.meeting_id,
            item_id=item.id,
            action=body.action,
        )
    return to_reads(db, [item], series.timezone)[0]


def _hand_over(
    db: Session, item: Item, series: Series, previous: uuid.UUID | None, now: datetime
) -> None:
    """Новому исполнителю уходит CONFIRM_REQUEST с повтором (BR-10), прежнему - уведомление. Токен
    тут не сохраняем: ссылку выдаёт отправитель в момент отправки.
    """
    assert item.assignee_person_id is not None
    notifications_service.cancel_due_reminder(db, item.id)
    _plan_reminder(db, item, series.timezone, now)
    notifications_service.queue(
        db,
        NotificationType.CONFIRM_REQUEST,
        item_id=item.id,
        meeting_id=item.meeting_id,
        person_id=item.assignee_person_id,
        dedupe_key=f"{item.id}:CONFIRM_REQUEST:{item.assignee_person_id}:{item.version}",
        scheduled_for=now,
    )
    notifications_service.plan_reassign_repeat(
        db, item, item.assignee_person_id, series.timezone, now
    )
    if previous is not None and previous != item.assignee_person_id:
        if repository.pending_count(db, item.meeting_id, previous) == 0:
            notifications_service.cancel_repeat(db, item.meeting_id, previous)
        notifications_service.queue(
            db,
            NotificationType.ITEM_REASSIGNED,
            item_id=item.id,
            meeting_id=item.meeting_id,
            person_id=previous,
            dedupe_key=f"{item.id}:ITEM_REASSIGNED:{previous}:{item.version}",
            scheduled_for=now,
            payload={"what": item.what or item.raw_text},
        )


def meeting_items(db: Session, meeting_id: uuid.UUID, *, lock: bool = False) -> list[Item]:
    return repository.list_for_meeting(db, meeting_id, lock=lock)


def open_gaps(db: Session, item_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[Gap]]:
    return repository.open_gaps(db, item_ids)


def emit_updated(db: Session, item: Item, tz: str) -> None:
    _emit(db, item.meeting_id, "item.updated", item, _one(db, item, tz).model_dump(mode="json"))


def send_draft(db: Session, user: User, item: Item, now: datetime) -> None:
    """Draft становится accepted, если пункт согласован на встрече; иначе pending, даже без
    исполнителя.
    """
    agreed = item.accepted_in_meeting and item.assignee_person_id is not None
    item.status = ItemStatus.ACCEPTED if agreed else ItemStatus.PENDING
    item.sent_at = now
    if agreed:
        item.accepted_at = now
    db.flush()
    _journal(db, user, item, ItemAction.SENT)


def _check_new_due(item: Item, series: Series, due: date) -> None:
    if due < today_in(series.timezone):
        raise ValidationFailed("Срок не может быть в прошлом", code="DUE_IN_PAST")
    if item.due_date and due > item.due_date + confirm_service.RESCHEDULE_LIMIT:
        raise ValidationFailed("Новый срок не позже исходного плюс 60 дней", code="DUE_TOO_LATE")


def _answer(
    db: Session,
    item: Item,
    series: Series,
    action: str,
    now: datetime,
    due_date: date | None,
) -> ItemAction:
    if action == "accept":
        _require_status(item, (ItemStatus.PENDING,))
        item.status = ItemStatus.ACCEPTED
        item.accepted_at = now
        return ItemAction.ACCEPTED
    if action == "done":
        _require_status(item, (ItemStatus.ACCEPTED,))
        item.status = ItemStatus.DONE
        item.done_at = now
        item.done_by = DoneBy.ASSIGNEE
        notifications_service.cancel_due_reminder(db, item.id)
        analytics_service.track(
            db,
            "item_done",
            series_id=series.id,
            meeting_id=item.meeting_id,
            item_id=item.id,
            on_time=item.due_date is None or today_in(series.timezone) <= item.due_date,
            by=DoneBy.ASSIGNEE.value,
        )
        return ItemAction.DONE
    _require_status(item, tuple(OPEN_STATUSES))
    if action == "reschedule":
        assert due_date is not None
        _check_new_due(item, series, due_date)
        item.due_date = due_date
        item.reschedule_count += 1
        item.status = ItemStatus.ACCEPTED
        item.accepted_at = item.accepted_at or now
        return ItemAction.RESCHEDULED
    item.status = ItemStatus.DISPUTED
    notifications_service.cancel_due_reminder(db, item.id)
    return ItemAction.DISPUTED


def answer_by_link(
    db: Session,
    person: Person,
    meeting_id: uuid.UUID,
    item_id: uuid.UUID,
    action: str,
    *,
    due_date: date | None = None,
    comment: str | None = None,
) -> Item:
    """Перенос срока считается принятием пункта (BR-6). О переносе и отказе ведущему уходит
    HOST_RESPONSE (FR-31).
    """
    locked = repository.get_locked(db, item_id)
    if locked is None:
        raise NotFound("Пункт не найден")
    item, series = locked
    series_service.archived_guard(series)
    if (
        item.assignee_person_id != person.id
        or item.meeting_id != meeting_id
        or item.sent_at is None
    ):
        raise Forbidden("Это не ваш пункт", code="NOT_YOUR_ITEM")
    now = datetime.now(UTC)
    hours_since_sent = int((now - item.sent_at).total_seconds() // 3600) if item.sent_at else None
    status_from, due_from = item.status, item.due_date
    journal = _answer(db, item, series, action, now, due_date)
    item.version += 1
    db.flush()
    data: dict[str, object] = {
        "version": item.version,
        "status_from": status_from.value,
        "status": item.status.value,
    }
    if journal == ItemAction.RESCHEDULED and item.due_date:
        data |= {"due_from": due_from.isoformat() if due_from else None}
        data["due_date"] = item.due_date.isoformat()
    event = repository.add_event(
        db,
        ItemEvent(
            item_id=item.id,
            actor_type=ActorType.ASSIGNEE,
            actor_person_id=person.id,
            action=journal,
            data=data,
        ),
    )
    if action in ("reschedule", "decline"):
        if comment:
            kind = CommentKind.RESCHEDULE if action == "reschedule" else CommentKind.NOT_MINE
            repository.add_comment(
                db,
                ItemComment(item_id=item.id, author_person_id=person.id, kind=kind, text=comment),
            )
        notifications_service.queue_host_response(
            db,
            item=item,
            series=series,
            person=person,
            kind=action,
            event_id=event.id,
            now=now,
            comment=comment,
        )
    if action == "reschedule":
        notifications_service.replan_due_tomorrow(db, item, series.timezone)
        confirm_service.recalc_link_expiry(db, item.meeting_id, person.id)
    if repository.pending_count(db, item.meeting_id, person.id) == 0:
        notifications_service.cancel_repeat(db, item.meeting_id, person.id)
    analytics_service.track(
        db,
        "link_answered",
        series_id=series.id,
        meeting_id=item.meeting_id,
        item_id=item.id,
        answer=action,
        hours_since_sent=hours_since_sent,
    )
    emit_updated(db, item, series.timezone)
    return item


def clear_person_comments(db: Session, person_id: uuid.UUID, now: datetime) -> int:
    return repository.clear_person_comments(db, person_id, now)


def purge_old_events(db: Session, cutoff: datetime, limit: int) -> int:
    return repository.delete_events_before(db, cutoff, limit)


def purge_items_of_archived_series(db: Session, cutoff: datetime, limit: int) -> int:
    return repository.delete_items_of_archived_series(db, cutoff, limit)
