import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, SessionTransaction

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.modules.analytics import service as analytics_service
from app.modules.auth.models import User
from app.modules.confirm import service as confirm_service
from app.modules.items import service as items_service
from app.modules.items.models import Item, ItemStatus
from app.modules.live import service as live_service
from app.modules.llm import service as llm_service
from app.modules.llm.client import get_llm_client
from app.modules.notifications import content
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.series import repository
from app.modules.series.models import (
    LOAD_WARNING_THRESHOLD_DEFAULT,
    AgendaItem,
    AgendaItemStatus,
    CaptureMode,
    ClosureTrigger,
    Meeting,
    MeetingParticipant,
    MeetingStatus,
    Organization,
    Person,
    Series,
    SeriesMember,
    SeriesStatus,
)
from app.modules.series.schemas import (
    AgendaItemCreate,
    AgendaItemRead,
    AgendaItemUpdate,
    AnonymizeResult,
    MeetingDetail,
    MeetingRead,
    MeetingStart,
    MeetingStarted,
    MeetingUpdate,
    MemberCreate,
    MemberRead,
    ParticipantRead,
    ReviewRead,
    SendRequest,
    SendResult,
    SeriesCreate,
    SeriesDetail,
    SeriesRead,
    SeriesUpdate,
    SummaryText,
)

logger = logging.getLogger(__name__)

CLOSURE_LEAD = timedelta(minutes=5)


def get_owned_series(db: Session, user: User, series_id: uuid.UUID) -> Series:
    series = repository.get_series(db, user.id, series_id)
    if series is None:
        raise NotFound("Серия не найдена")
    return series


def get_owned_meeting(db: Session, user: User, meeting_id: uuid.UUID) -> tuple[Meeting, Series]:
    found = repository.get_meeting(db, user.id, meeting_id)
    if found is None:
        raise NotFound("Встреча не найдена")
    return found


def member_persons(db: Session, series_id: uuid.UUID) -> list[Person]:
    return [p for p, _ in repository.members(db, series_id)]


def is_member(db: Session, series_id: uuid.UUID, person_id: uuid.UUID) -> bool:
    return repository.is_member(db, series_id, person_id)


def has_agenda_item(db: Session, meeting_id: uuid.UUID, agenda_id: uuid.UUID) -> bool:
    return any(a.id == agenda_id for a in repository.agenda(db, meeting_id))


def is_present(db: Session, meeting_id: uuid.UUID, person_id: uuid.UUID) -> bool:
    return repository.is_present(db, meeting_id, person_id)


def _reads(db: Session, series: list[Series]) -> list[SeriesRead]:
    ids = [s.id for s in series]
    counts = repository.members_count(db, ids)
    active = repository.active_meetings(db, ids)
    last = repository.last_meeting_starts(db, ids)
    open_items = items_service.open_counts_by_series(db, ids)
    columns = [
        f
        for f in SeriesRead.model_fields
        if f in Series.__table__.columns and f != "load_warning_threshold"
    ]
    return [
        SeriesRead(
            **{c: getattr(s, c) for c in columns},
            load_warning_threshold=s.load_warning_threshold or LOAD_WARNING_THRESHOLD_DEFAULT,
            open_items_count=open_items.get(s.id, 0),
            members_count=counts.get(s.id, 0),
            active_meeting_id=active.get(s.id),
            last_meeting_at=last.get(s.id),
        )
        for s in series
    ]


def list_series(db: Session, user: User) -> list[SeriesRead]:
    return _reads(db, repository.list_series(db, user.id))


def _members(db: Session, series: Series) -> list[MemberRead]:
    rows = repository.members(db, series.id)
    loads = items_service.open_counts(
        db, [p.id for p, _ in rows], items_service.today_in(series.timezone)
    )
    return [
        MemberRead(
            person_id=p.id,
            full_name=p.full_name,
            email=p.email,
            organization_id=o.id,
            organization_name=o.name,
            is_own_side=o.is_own,
            open_items=loads.get(p.id, 0),
        )
        for p, o in rows
    ]


def get_series(db: Session, user: User, series_id: uuid.UUID) -> SeriesDetail:
    series = get_owned_series(db, user, series_id)
    read = _reads(db, [series])[0]
    return SeriesDetail(**read.model_dump(), members=_members(db, series))


def list_members(db: Session, user: User, series_id: uuid.UUID) -> list[MemberRead]:
    return _members(db, get_owned_series(db, user, series_id))


def list_meetings(db: Session, user: User, series_id: uuid.UUID) -> list[MeetingRead]:
    series = get_owned_series(db, user, series_id)
    return [MeetingRead.model_validate(m) for m in repository.list_meetings(db, series.id)]


def get_meeting(db: Session, user: User, meeting_id: uuid.UUID) -> MeetingDetail:
    meeting, series = get_owned_meeting(db, user, meeting_id)
    return _detail(db, meeting, series)


def _detail(db: Session, meeting: Meeting, series: Series) -> MeetingDetail:
    items = items_service.list_meeting_items(db, meeting.id, series.timezone)
    participants = [
        ParticipantRead(
            person_id=p.id, full_name=p.full_name, organization_name=o.name, present=mp.present
        )
        for mp, p, o in repository.participants(db, meeting.id)
    ]
    closing = meeting.status == MeetingStatus.CLOSING
    uncovered = {a.id for a in repository.uncovered_agenda(db, meeting.id)} if closing else set()
    if closing:
        items.sort(key=lambda i: not i.gaps)
    return MeetingDetail(
        **MeetingRead.model_validate(meeting).model_dump(),
        series_title=series.title,
        timezone=series.timezone,
        participants=participants,
        agenda=[
            AgendaItemRead.model_validate(a).model_copy(update={"uncovered": a.id in uncovered})
            for a in repository.agenda(db, meeting.id)
        ],
        items=items,
        open_gaps_count=sum(len(i.gaps) for i in items),
    )


OWN_ORGANIZATION_NAME = "Моя команда"


def create_own_organization(db: Session, user: User) -> Organization:
    return repository.add_organization(
        db, Organization(owner_user_id=user.id, name=OWN_ORGANIZATION_NAME, is_own=True)
    )


def archived_guard(series: Series) -> None:
    if series.status == SeriesStatus.ARCHIVED:
        raise Conflict("Серия в архиве", code="SERIES_ARCHIVED")


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _upsert_member(
    db: Session, user: User, series: Series, body: MemberCreate
) -> tuple[Person, Organization]:
    person = repository.get_person_by_email(db, user.id, body.email)
    if person is not None:
        org = repository.get_organization_by_id(db, person.organization_id)
    else:
        org = repository.get_organization(db, user.id, body.organization_name)
        if org is None:
            org = repository.add(
                db, Organization(owner_user_id=user.id, name=body.organization_name)
            )
        person = repository.add(
            db,
            Person(
                owner_user_id=user.id,
                organization_id=org.id,
                full_name=body.full_name,
                email=body.email,
            ),
        )
    if not repository.is_member(db, series.id, person.id):
        repository.add(db, SeriesMember(series_id=series.id, person_id=person.id))
    return person, org


def _member_read(person: Person, org: Organization) -> MemberRead:
    return MemberRead(
        person_id=person.id,
        full_name=person.full_name,
        email=person.email,
        organization_id=org.id,
        organization_name=org.name,
        is_own_side=org.is_own,
    )


def create_series(db: Session, user: User, body: SeriesCreate) -> SeriesDetail:
    if body.capture_mode == CaptureMode.AUDIO:
        raise ValidationFailed("Режим audio появится на этапе 3")  # OQ-15
    series = repository.add(
        db,
        Series(
            owner_user_id=user.id,
            title=body.title,
            timezone=body.timezone,
            planned_duration_min=body.planned_duration_min,
            capture_mode=body.capture_mode,
        ),
    )
    added = [_upsert_member(db, user, series, m) for m in body.members]
    analytics_service.track(
        db,
        "series_created",
        user_id=user.id,
        series_id=series.id,
        members_count=len({p.id for p, _ in added}),
        has_external=any(not org.is_own for _, org in added),
    )
    return get_series(db, user, series.id)


def update_series(
    db: Session, user: User, series_id: uuid.UUID, body: SeriesUpdate
) -> SeriesDetail:
    series = get_owned_series(db, user, series_id)
    archived_guard(series)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(series, field, value)
    db.flush()
    return get_series(db, user, series.id)


def archive_series(db: Session, user: User, series_id: uuid.UUID) -> SeriesDetail:
    series = get_owned_series(db, user, series_id)
    if series.status == SeriesStatus.ARCHIVED:
        return get_series(db, user, series.id)  # повтор: дата архивации не сдвигается
    repository.lock_series(db, series)
    if repository.active_meetings(db, [series.id]):
        raise Conflict("В серии идёт встреча", code="MEETING_ALREADY_LIVE")
    series.status = SeriesStatus.ARCHIVED
    series.archived_at = datetime.now(UTC)
    db.flush()
    return get_series(db, user, series.id)


def add_member(db: Session, user: User, series_id: uuid.UUID, body: MemberCreate) -> MemberRead:
    series = get_owned_series(db, user, series_id)
    archived_guard(series)
    person, org = _upsert_member(db, user, series, body)
    active = repository.active_meetings(db, [series.id]).get(series.id)
    # добавленный во время встречи сразу становится её участником и получит SUMMARY (OQ-12)
    if active and repository.get_participant(db, active, person.id) is None:
        repository.add(db, MeetingParticipant(meeting_id=active, person_id=person.id))
    return _member_read(person, org)


def remove_member(db: Session, user: User, series_id: uuid.UUID, person_id: uuid.UUID) -> None:
    series = get_owned_series(db, user, series_id)
    archived_guard(series)
    if not repository.remove_member(db, series.id, person_id):
        raise NotFound("Участник не найден в серии")


def _review(db: Session, series: Series, meeting: Meeting) -> ReviewRead:
    previous = repository.previous_meeting(db, series.id, meeting.planned_start, meeting.id)
    groups = items_service.review_groups(db, series, previous.planned_start if previous else None)
    return ReviewRead(**groups)


def _copy_moved_agenda(db: Session, series: Series, meeting: Meeting) -> None:
    previous = repository.previous_meeting(db, series.id, meeting.planned_start, meeting.id)
    if previous is None:
        return
    for position, moved in enumerate(repository.moved_agenda(db, previous.id), start=1):
        repository.add(db, AgendaItem(meeting_id=meeting.id, position=position, title=moved.title))


def start_meeting(
    db: Session, user: User, series_id: uuid.UUID, body: MeetingStart
) -> MeetingStarted:
    series = get_owned_series(db, user, series_id)
    repository.lock_series(db, series)
    archived_guard(series)
    if repository.active_meetings(db, [series.id]):
        raise Conflict("В серии уже идёт встреча", code="MEETING_ALREADY_LIVE")
    now = datetime.now(UTC)
    end = _utc(body.planned_end) if body.planned_end else None
    end = end or now + timedelta(minutes=series.planned_duration_min)
    if end <= now:
        raise ValidationFailed("Плановый конец должен быть позже начала")
    try:
        with db.begin_nested():
            meeting = repository.add(
                db,
                Meeting(
                    series_id=series.id,
                    planned_start=now,
                    planned_end=end,
                    status=MeetingStatus.LIVE,
                    started_at=now,
                ),
            )
    except IntegrityError as exc:
        raise Conflict("В серии уже идёт встреча", code="MEETING_ALREADY_LIVE") from exc
    for person_id in repository.member_ids(db, series.id):
        repository.add(db, MeetingParticipant(meeting_id=meeting.id, person_id=person_id))
    _copy_moved_agenda(db, series, meeting)
    review = _review(db, series, meeting)
    analytics_service.track(
        db,
        "meeting_started",
        user_id=user.id,
        series_id=series.id,
        meeting_id=meeting.id,
        capture_mode=series.capture_mode.value,
        review_items_count=_action_count(review),
    )
    return MeetingStarted(meeting=_detail(db, meeting, series), review=review)


def _action_count(review: ReviewRead) -> int:
    return (
        len(review.unassigned)
        + len(review.overdue)
        + len(review.disputed)
        + len(review.stale_pending)
    )


def update_meeting(
    db: Session, user: User, meeting_id: uuid.UUID, body: MeetingUpdate
) -> MeetingDetail:
    meeting, series = get_owned_meeting(db, user, meeting_id)
    archived_guard(series)
    if body.planned_end is not None:
        end = _utc(body.planned_end)
        if end <= meeting.planned_start:
            raise ValidationFailed("Плановый конец должен быть позже начала")
        meeting.planned_end = end
    if body.participants is not None:
        rows = []
        for entry in body.participants:
            row = repository.get_participant(db, meeting.id, entry.person_id)
            if row is None:
                raise ValidationFailed(
                    "Человека нет среди участников встречи", code="NOT_A_PARTICIPANT"
                )
            rows.append((row, entry.present))
        for row, present in rows:
            row.present = present
    db.flush()
    return _detail(db, meeting, series)


def uncovered_agenda(db: Session, meeting_id: uuid.UUID) -> list[AgendaItem]:
    return repository.uncovered_agenda(db, meeting_id)


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError:
        return None


def apply_agenda_matches(db: Session, meeting_id: uuid.UUID, pairs: list[dict[str, Any]]) -> int:
    """Пары {agenda_item_id, item_id} от LLM. Id из чужой встречи отбрасываем, связь, уже
    поставленную ведущим, не перезаписываем.
    """
    topics = {a.id for a in repository.agenda(db, meeting_id)}
    item_ids = {i.id for i in items_service.meeting_items(db, meeting_id)}
    applied = 0
    for pair in pairs:
        topic, item = _as_uuid(pair.get("agenda_item_id")), _as_uuid(pair.get("item_id"))
        if (
            topic in topics
            and item in item_ids
            and (repository.link_agenda_if_free(db, meeting_id, item, topic))
        ):
            applied += 1
    return applied


AGENDA_OUTBOX = "agenda_matching"


def _schedule_agenda_matching(db: Session, meeting: Meeting) -> None:
    """Запрос к LLM уходит в задачу после коммита, чтобы не держать блокировку встречи."""
    db.info.setdefault(AGENDA_OUTBOX, set()).add(meeting.id)


@event.listens_for(Session, "after_commit")
def _start_agenda_matching(session: Session) -> None:
    for meeting_id in session.info.pop(AGENDA_OUTBOX, set()):
        try:
            from app.modules.series import tasks

            tasks.match_agenda.delay(str(meeting_id))
        except Exception:
            logger.warning("agenda matching was not started", exc_info=True)


@event.listens_for(Session, "after_soft_rollback")
def _drop_agenda_matching(session: Session, previous_transaction: SessionTransaction) -> None:
    if not previous_transaction.nested:
        session.info.pop(AGENDA_OUTBOX, None)


def suggest_agenda_links(db: Session, meeting_id: uuid.UUID) -> int:
    """Сопоставление темы повестки с решениями одним запросом к LLM (FR-27). Связи ведущего не
    трогаем.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None or meeting.status != MeetingStatus.CLOSING:
        return 0
    topics = [a for a in repository.agenda(db, meeting.id) if a.status == AgendaItemStatus.OPEN]
    items = items_service.meeting_items(db, meeting.id)
    if not topics or not items:
        return 0
    pairs = llm_service.match_agenda(
        db,
        get_llm_client(),
        meeting_id=meeting.id,
        agenda=[{"id": str(a.id), "title": a.title} for a in topics],
        decisions=[{"id": str(i.id), "text": i.what or i.raw_text} for i in items],
    )
    applied = apply_agenda_matches(db, meeting.id, pairs)
    if applied:
        live_service.publish_after_commit(
            db,
            meeting.id,
            {
                "type": "agenda.updated",
                "meeting_id": str(meeting.id),
                "item_id": None,
                "version": None,
                "data": {"matched": applied},
            },
        )
    return applied


def _begin_closure(
    db: Session, meeting: Meeting, series: Series, trigger: ClosureTrigger, now: datetime
) -> None:
    meeting.status = MeetingStatus.CLOSING
    meeting.closure_opened_at = now
    meeting.closure_trigger = trigger
    db.flush()
    analytics_service.track(
        db,
        "closure_opened",
        user_id=series.owner_user_id,
        series_id=series.id,
        meeting_id=meeting.id,
        trigger=trigger.value,
        minutes_to_planned_end=int((meeting.planned_end - now).total_seconds() // 60),
    )
    _schedule_agenda_matching(db, meeting)
    live_service.publish_after_commit(
        db,
        meeting.id,
        {
            "type": "closure.opened",
            "meeting_id": str(meeting.id),
            "item_id": None,
            "version": None,
            "data": {"trigger": trigger.value},
        },
    )


def open_closure(db: Session, user: User, meeting_id: uuid.UUID) -> MeetingDetail:
    meeting, series = get_owned_meeting(db, user, meeting_id)
    repository.lock_meeting(db, meeting)
    if meeting.status == MeetingStatus.SENT:
        raise Conflict("Встреча уже разослана", code="ALREADY_SENT")
    if meeting.status != MeetingStatus.LIVE:
        raise Conflict("Встреча не идёт", code="MEETING_NOT_LIVE")
    _begin_closure(db, meeting, series, ClosureTrigger.BUTTON, datetime.now(UTC))
    return _detail(db, meeting, series)


def open_due_closures(db: Session, now: datetime) -> int:
    due = repository.due_for_closure(db, now + CLOSURE_LEAD)
    for meeting, series in due:
        _begin_closure(db, meeting, series, ClosureTrigger.TIMER, now)
    return len(due)


def _queue_letters(
    db: Session,
    series: Series,
    meeting: Meeting,
    sent: list[Item],
    all_sent: list[Item],
    *,
    first: bool,
    now: datetime,
) -> int:
    """Письма одной рассылки; повтор ничего не дублирует благодаря dedupe_key. SUMMARY получают все
    участники, но только при первой рассылке; ссылку получает каждый исполнитель, в том числе по
    согласованным пунктам (FR-35).
    """
    people = {i.assignee_person_id for i in (all_sent if first else sent) if i.assignee_person_id}
    links = {p: confirm_service.ensure_link(db, p, meeting.id, now).id for p in people}
    queued = 0
    if first:
        for mp, _, _ in repository.participants(db, meeting.id):
            queued += notifications_service.queue(
                db,
                NotificationType.SUMMARY,
                item_id=None,
                meeting_id=meeting.id,
                person_id=mp.person_id,
                dedupe_key=f"{meeting.id}:SUMMARY:{mp.person_id}",
                scheduled_for=now,
                payload=content.summary(db, meeting.id, mp.person_id, links.get(mp.person_id)),
            )
    repeat_for: set[uuid.UUID] = set()
    for item in sent:
        person = item.assignee_person_id
        if person is None:
            continue
        link_id = links[person]
        if item.status == ItemStatus.PENDING:
            repeat_for.add(person)
            queued += notifications_service.queue(
                db,
                NotificationType.CONFIRM_REQUEST,
                item_id=item.id,
                meeting_id=meeting.id,
                person_id=person,
                dedupe_key=f"{item.id}:CONFIRM_REQUEST:{person}:{item.version}",
                scheduled_for=now,
                payload=content.confirm_request(db, item, link_id),
            )
        queued += notifications_service.plan_due_reminder(
            db,
            item_id=item.id,
            meeting_id=meeting.id,
            person_id=person,
            due_date=item.due_date,
            version=item.version,
            tz=series.timezone,
            now=now,
            payload=content.due_tomorrow(db, item, link_id),
        )
    for person in repeat_for:
        queued += notifications_service.queue(
            db,
            NotificationType.CONFIRM_REPEAT,
            item_id=None,
            meeting_id=meeting.id,
            person_id=person,
            dedupe_key=f"{meeting.id}:CONFIRM_REPEAT:{person}",
            scheduled_for=notifications_service.repeat_time(now, series.timezone),
        )
    return queued


def _send_result(
    meeting: Meeting, counters: dict[str, int], *, items_sent: int, queued: int, already_sent: bool
) -> SendResult:
    return SendResult(
        meeting_id=meeting.id,
        status=meeting.status,
        summary_sent_at=meeting.summary_sent_at,
        meeting=MeetingRead.model_validate(meeting),
        items_sent=items_sent,
        notifications_queued=queued,
        already_sent=already_sent,
        **counters,
    )


def send_summary(db: Session, user: User, meeting_id: uuid.UUID, body: SendRequest) -> SendResult:
    """draft -> pending/accepted, ссылки и письма в одной транзакции (FR-28)."""
    meeting, series = get_owned_meeting(db, user, meeting_id)
    repository.lock_meeting(db, meeting)
    if meeting.status == MeetingStatus.PLANNED:
        raise Conflict("Встреча ещё не началась", code="MEETING_NOT_LIVE")
    items = items_service.meeting_items(db, meeting.id, lock=True)
    drafts = [i for i in items if i.status == ItemStatus.DRAFT]
    first = meeting.status != MeetingStatus.SENT
    gaps = items_service.open_gaps(db, [i.id for i in items])
    counters = {
        "items_total": len(items),
        "items_with_open_gaps": sum(1 for i in items if gaps.get(i.id)),
        "items_unassigned": sum(1 for i in items if i.assignee_person_id is None),
        "recipients": len(repository.participants(db, meeting.id)),
    }
    if not first and not drafts:
        return _send_result(meeting, counters, items_sent=0, queued=0, already_sent=True)
    open_gaps = sum(len(gaps.get(i.id, [])) for i in drafts)
    if open_gaps and not body.confirm_open_gaps:
        raise ValidationFailed(
            f"Осталось пробелов: {open_gaps}", code="GAPS_OPEN", open_gaps_count=open_gaps
        )
    now = datetime.now(UTC)
    if first:
        meeting.status = MeetingStatus.SENT
        meeting.summary_sent_at = now
    for item in drafts:
        items_service.send_draft(db, user, item, now)
    all_sent = [i for i in items if i.sent_at is not None and i.status != ItemStatus.CANCELLED]
    queued = _queue_letters(db, series, meeting, drafts, all_sent, first=first, now=now)
    if first:
        analytics_service.track(
            db,
            "summary_sent",
            user_id=user.id,
            series_id=series.id,
            meeting_id=meeting.id,
            items_total=counters["items_total"],
            items_with_open_gaps=counters["items_with_open_gaps"],
            recipients=counters["recipients"],
        )
    for item in drafts:
        items_service.emit_updated(db, item, series.timezone)
    live_service.publish_after_commit(
        db,
        meeting.id,
        {
            "type": "meeting.sent",
            "meeting_id": str(meeting.id),
            "item_id": None,
            "version": None,
            "data": {"items_sent": len(drafts), **counters},
        },
    )
    return _send_result(
        meeting, counters, items_sent=len(drafts), queued=queued, already_sent=False
    )


def summary_text(db: Session, user: User, meeting_id: uuid.UUID) -> SummaryText:
    """Текст для мессенджера без персональных ссылок и токенов."""
    meeting, series = get_owned_meeting(db, user, meeting_id)
    if meeting.status not in (MeetingStatus.CLOSING, MeetingStatus.SENT):
        raise Conflict("Встреча ещё идёт, итога пока нет", code="SUMMARY_NOT_READY")
    items = [
        i for i in items_service.meeting_items(db, meeting.id) if i.status != ItemStatus.CANCELLED
    ]
    names = repository.persons(db, [i.assignee_person_id for i in items if i.assignee_person_id])
    day = meeting.planned_start.astimezone(ZoneInfo(series.timezone))
    lines = [f"Итоги встречи «{series.title}», {day:%d.%m}"]
    for number, item in enumerate(items, start=1):
        person = names.get(item.assignee_person_id) if item.assignee_person_id else None
        due = f", до {item.due_date:%d.%m}" if item.due_date else ""
        who = person.full_name if person else "не назначен"
        lines.append(f"{number}. {item.what or item.raw_text} — {who}{due}")
    analytics_service.track(
        db,
        "summary_copied",
        user_id=user.id,
        series_id=series.id,
        meeting_id=meeting.id,
        items_total=len(items),
        after_summary_sent=meeting.summary_sent_at is not None,
    )
    return SummaryText(text="\n".join(lines), items_total=len(items))


def get_review(db: Session, user: User, meeting_id: uuid.UUID) -> ReviewRead:
    meeting, series = get_owned_meeting(db, user, meeting_id)
    return _review(db, series, meeting)


def skip_review(db: Session, user: User, meeting_id: uuid.UUID) -> None:
    meeting, series = get_owned_meeting(db, user, meeting_id)
    analytics_service.track(
        db,
        "review_skipped",
        user_id=user.id,
        series_id=series.id,
        meeting_id=meeting.id,
        items_count=_action_count(_review(db, series, meeting)),
    )


MAX_AGENDA_ITEMS = 20


def add_agenda_item(
    db: Session, user: User, meeting_id: uuid.UUID, body: AgendaItemCreate
) -> AgendaItem:
    meeting, series = get_owned_meeting(db, user, meeting_id)
    archived_guard(series)
    repository.lock_meeting(db, meeting)
    count, top = repository.agenda_stats(db, meeting.id)
    if count >= MAX_AGENDA_ITEMS:
        raise ValidationFailed("В повестке не больше 20 пунктов")
    return repository.add(db, AgendaItem(meeting_id=meeting.id, position=top + 1, title=body.title))


def _move_agenda_item(db: Session, item: AgendaItem, target: int) -> None:
    """Повестку после перестановки перенумеровываем 1..n без пропусков; целевая позиция прижимается
    к этому диапазону.
    """
    order = [a for a in repository.agenda(db, item.meeting_id) if a.id != item.id]
    order.insert(min(max(target, 1), len(order) + 1) - 1, item)
    changed = [(a, n) for n, a in enumerate(order, start=1) if a.position != n]
    if not changed:
        return
    for a, _ in changed:
        a.position = -a.position
    db.flush()
    for a, n in changed:
        a.position = n
    db.flush()


def update_agenda_item(
    db: Session, user: User, agenda_id: uuid.UUID, body: AgendaItemUpdate
) -> AgendaItem:
    item = repository.get_agenda_item(db, user.id, agenda_id)
    if item is None:
        raise NotFound("Пункт повестки не найден")
    archived_guard(repository.get_series_of_meeting(db, item.meeting_id))
    if body.title is not None:
        item.title = body.title
    if body.position is not None:
        repository.lock_meeting(db, repository.get_meeting_by_id(db, item.meeting_id))
        _move_agenda_item(db, item, body.position)
    if body.status is not None and body.status != item.status:
        first_resolution = item.status == AgendaItemStatus.OPEN
        item.status = body.status
        if (
            first_resolution
            and body.status != AgendaItemStatus.OPEN
            and not repository.agenda_has_items(db, item.id)
        ):
            meeting = repository.get_meeting_by_id(db, item.meeting_id)
            analytics_service.track(
                db,
                "agenda_item_uncovered",
                user_id=user.id,
                series_id=meeting.series_id,
                meeting_id=meeting.id,
                position=item.position,
                resolution=body.status.value,
            )
    db.flush()
    return item


def anonymize_person(db: Session, person_id: uuid.UUID) -> AnonymizeResult:
    person = repository.get_person_for_update(db, person_id)
    if person is None:
        raise NotFound("Участник не найден")
    if person.anonymized_at is not None:
        return AnonymizeResult(
            person_id=person.id,
            full_name=person.full_name,
            anonymized_at=person.anonymized_at,
            links_revoked=0,
            comments_cleared=0,
        )
    now = datetime.now(UTC)
    repository.lock_owner_numbering(db, person.owner_user_id)
    number = repository.count_anonymized_persons(db, person.owner_user_id) + 1
    person.full_name = f"Участник {number}"
    person.email = None
    person.anonymized_at = now
    links = confirm_service.revoke_person_links(db, person.id, now)
    comments = items_service.clear_person_comments(db, person.id, now)
    notifications_service.cancel_queued_for_person(db, person.id)
    db.flush()
    return AnonymizeResult(
        person_id=person.id,
        full_name=person.full_name,
        anonymized_at=now,
        links_revoked=links,
        comments_cleared=comments,
    )


def retire_archived(db: Session, cutoff: datetime) -> int:
    """Обезличивает участников серий, архивированных до cutoff, если у них нет других серий и
    пунктов. Вызывается задачей хранения с cutoff на год назад (BR-20).
    """
    orphans = repository.orphan_person_ids(db, repository.drop_members_of_archived(db, cutoff))
    for person_id in orphans:
        anonymize_person(db, person_id)
    return len(orphans)
