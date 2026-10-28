import argparse
import hashlib
import logging
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal, get_engine
from app.core.security import hash_password
from app.models import (
    AccessLink,
    Base,
    Gap,
    Item,
    ItemComment,
    ItemEvent,
    LlmPrompt,
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
from app.modules.items.models import ActorType, CommentKind, DoneBy, ItemAction, ItemStatus
from app.modules.notifications.models import NotificationStatus, NotificationType
from app.modules.series.models import ClosureTrigger, MeetingStatus

DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"
DEFAULT_SET = "main"
LINK_TTL = timedelta(days=14)
# Справочные таблицы, которые `clear` не трогает (промпты создаются миграцией).
KEEP_ON_CLEAR = {LlmPrompt.__tablename__}

logger = logging.getLogger(__name__)

Refs = dict[str, Any]

# Порядок фиксирован, чтобы наборы с разным составом давали одному ключу один адрес команды.
TEAM_SLOTS = ("ivan", "anna", "oleg", "kate")


def read_set(name: str) -> dict | None:
    path = DEMO_DIR / f"{name}.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: top level must be a mapping")
    return data


def _now() -> datetime:
    return datetime.now(UTC)


def _today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def _load_users(db: Session, rows: list[dict], refs: Refs) -> int:
    for row in rows:
        user = db.scalar(select(User).where(User.email == row["email"]))
        if user is None:
            user = User(
                email=row["email"],
                password_hash=hash_password(str(row["password"])),
                full_name=row["full_name"],
            )
            if "timezone" in row:
                user.timezone = row["timezone"]
            db.add(user)
        refs[row["ref"]] = user
    db.flush()
    return len(rows)


def _load_organizations(db: Session, rows: list[dict], refs: Refs) -> int:
    for row in rows:
        owner = refs[row["owner"]]
        org = db.scalar(
            select(Organization).where(
                Organization.owner_user_id == owner.id,
                func.lower(Organization.name) == row["name"].lower(),
            )
        )
        if org is None:
            org = Organization(
                owner_user_id=owner.id, name=row["name"], is_own=row.get("is_own", False)
            )
            db.add(org)
            db.flush()
        refs[row["ref"]] = org
    return len(rows)


def _person_email(row: dict) -> str:
    team = get_settings().demo_team_email_list
    slot = TEAM_SLOTS.index(row["ref"]) if row["ref"] in TEAM_SLOTS else len(team)
    return team[slot] if slot < len(team) else row["email"]


def _load_persons(db: Session, rows: list[dict], refs: Refs) -> int:
    for row in rows:
        email = _person_email(row)
        owner = refs[row["owner"]]
        person = db.scalar(
            select(Person).where(Person.owner_user_id == owner.id, Person.email == email)
        )
        if person is None:
            person = Person(
                owner_user_id=owner.id,
                organization_id=refs[row["organization"]].id,
                full_name=row["full_name"],
                email=email,
            )
            db.add(person)
            db.flush()
        refs[row["ref"]] = person
    return len(rows)


def _load_series(db: Session, rows: list[dict], refs: Refs) -> int:
    for row in rows:
        series = Series(
            owner_user_id=refs[row["owner"]].id,
            title=row["title"],
            timezone=row.get("timezone", "Europe/Moscow"),
            planned_duration_min=row["planned_duration_min"],
        )
        db.add(series)
        db.flush()
        db.add_all(SeriesMember(series_id=series.id, person_id=refs[p].id) for p in row["members"])
        refs[row["ref"]] = series
    db.flush()
    return len(rows)


def _meeting_start(spec: dict, tz: str) -> datetime:
    if "minutes" in spec:
        return (_now() + timedelta(minutes=spec["minutes"])).replace(second=0, microsecond=0)
    day = _today(tz) + timedelta(days=spec.get("days", 0))
    local = datetime.combine(day, time.fromisoformat(spec.get("time", "10:00")), ZoneInfo(tz))
    return local.astimezone(UTC)


def _load_meetings(db: Session, rows: list[dict], refs: Refs) -> int:
    for row in rows:
        series: Series = refs[row["series"]]
        start = _meeting_start(row["start"], series.timezone)
        end = start + timedelta(minutes=series.planned_duration_min)
        status = MeetingStatus(row.get("status", MeetingStatus.PLANNED))
        meeting = Meeting(series_id=series.id, planned_start=start, planned_end=end, status=status)
        if status != MeetingStatus.PLANNED:
            meeting.started_at = start
        if status in (MeetingStatus.CLOSING, MeetingStatus.SENT):
            meeting.closure_opened_at = end - timedelta(minutes=5)
            meeting.closure_trigger = ClosureTrigger.BUTTON
        if status == MeetingStatus.SENT:
            meeting.summary_sent_at = end
        db.add(meeting)
        db.flush()
        participants = row.get("participants", {})
        db.add_all(
            MeetingParticipant(meeting_id=meeting.id, person_id=refs[p].id, present=present)
            for p, present in participants.items()
        )
        if status == MeetingStatus.SENT:
            db.add_all(
                Notification(
                    type=NotificationType.SUMMARY,
                    recipient_person_id=refs[p].id,
                    meeting_id=meeting.id,
                    dedupe_key=f"{meeting.id}:SUMMARY:{refs[p].id}",
                    scheduled_for=end,
                    status=NotificationStatus.SENT,
                    attempts=1,
                    sent_at=end + timedelta(minutes=1),
                )
                for p in participants
            )
        refs[row["ref"]] = meeting
    db.flush()
    return len(rows)


def _event(item: Item, action: ItemAction, at: datetime, **data: Any) -> ItemEvent:
    actor = ActorType.ASSIGNEE if action in _ASSIGNEE_ACTIONS else ActorType.HOST
    return ItemEvent(
        item_id=item.id,
        actor_type=actor,
        actor_person_id=item.assignee_person_id if actor == ActorType.ASSIGNEE else None,
        action=action,
        data=data,
        created_at=at,
    )


_ASSIGNEE_ACTIONS = {ItemAction.ACCEPTED, ItemAction.RESCHEDULED, ItemAction.DISPUTED}


def _load_items(db: Session, rows: list[dict], refs: Refs) -> int:
    for row in rows:
        meeting: Meeting = refs[row["meeting"]]
        series = db.get(Series, meeting.series_id)
        assert series is not None
        status = ItemStatus(row.get("status", ItemStatus.DRAFT))
        assignee = refs[row["assignee"]] if row.get("assignee") else None
        due = row.get("due")
        item = Item(
            id=uuid.uuid4(),
            meeting_id=meeting.id,
            series_id=series.id,
            raw_text=row["raw_text"],
            what=row.get("what"),
            assignee_person_id=assignee.id if assignee else None,
            due_date=_today(series.timezone) + timedelta(days=due["days"]) if due else None,
            due_text=row.get("due_text"),
            done_criteria=row.get("done_criteria"),
            status=status,
            accepted_in_meeting=row.get("accepted_in_meeting", False),
            reschedule_count=row.get("reschedule_count", 0),
            created_at=meeting.started_at or _now(),
        )
        events = [_event(item, ItemAction.CREATED, item.created_at, status="draft")]
        sent = meeting.summary_sent_at
        if status != ItemStatus.DRAFT and sent is not None:
            item.sent_at = sent
            first = ItemStatus.ACCEPTED if item.accepted_in_meeting else ItemStatus.PENDING
            events.append(_event(item, ItemAction.SENT, sent, status_from="draft", status_to=first))
            if item.accepted_in_meeting:
                item.accepted_at = sent
            elif status in (ItemStatus.ACCEPTED, ItemStatus.DONE):
                item.accepted_at = sent + timedelta(hours=3)
                events.append(_event(item, ItemAction.ACCEPTED, item.accepted_at))
            if item.reschedule_count:
                at = sent + timedelta(days=1)
                events.append(_event(item, ItemAction.RESCHEDULED, at, due_date=str(item.due_date)))
            if status == ItemStatus.DISPUTED:
                at = sent + timedelta(hours=5)
                events.append(_event(item, ItemAction.DISPUTED, at, status_to="disputed"))
            if status == ItemStatus.DONE:
                item.done_at = _now() - timedelta(days=1)
                item.done_by = DoneBy.ASSIGNEE
                events.append(_event(item, ItemAction.DONE, item.done_at, status_to="done"))
            if status == ItemStatus.CANCELLED:
                item.cancelled_at = _now() - timedelta(days=2)
                events.append(
                    _event(item, ItemAction.CANCELLED, item.cancelled_at, status_to="cancelled")
                )
            if status == ItemStatus.PENDING and assignee is not None:
                db.add(
                    Notification(
                        type=NotificationType.CONFIRM_REQUEST,
                        recipient_person_id=assignee.id,
                        meeting_id=meeting.id,
                        item_id=item.id,
                        dedupe_key=f"{item.id}:CONFIRM_REQUEST:{assignee.id}:{item.version or 1}",
                        scheduled_for=sent,
                        status=NotificationStatus.SENT,
                        attempts=1,
                        sent_at=sent + timedelta(minutes=1),
                    )
                )
        db.add(item)
        db.flush()
        db.add_all(events)
        db.add_all(
            Gap(item_id=item.id, type=GapType(g["type"]), question=g.get("question"))
            for g in row.get("gaps", [])
        )
        if comment := row.get("comment"):
            if assignee is None:
                raise ValueError(f"item '{item.raw_text}': comment needs an assignee")
            db.add(
                ItemComment(
                    item_id=item.id,
                    author_person_id=assignee.id,
                    kind=CommentKind(comment["kind"]),
                    text=comment["text"],
                )
            )
        if "ref" in row:
            refs[row["ref"]] = item
    db.flush()
    return len(rows)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _load_access_links(db: Session, rows: list[dict], refs: Refs) -> int:
    for row in rows:
        digest = token_hash(row["token"])
        if db.scalar(select(AccessLink.id).where(AccessLink.token_hash == digest)):
            continue
        db.add(
            AccessLink(
                token_hash=digest,
                person_id=refs[row["person"]].id,
                meeting_id=refs[row["meeting"]].id,
                expires_at=_now() + LINK_TTL,
            )
        )
    db.flush()
    return len(rows)


LOADERS: dict[str, Callable[[Session, list[dict], Refs], int]] = {
    "users": _load_users,
    "organizations": _load_organizations,
    "persons": _load_persons,
    "series": _load_series,
    "meetings": _load_meetings,
    "items": _load_items,
    "access_links": _load_access_links,
}


def load(db: Session, data: dict) -> dict[str, int]:
    unknown = [key for key in data if key not in LOADERS]
    if unknown:
        logger.warning("demo: no loaders for %s, skipped", ", ".join(unknown))
    refs: Refs = {}
    return {key: fn(db, data[key] or [], refs) for key, fn in LOADERS.items() if key in data}


def clear(db: Session) -> None:
    for table in reversed(Base.metadata.sorted_tables):
        # Прямой DELETE в item_events запрещён (BR-19), строки удаляются каскадом с items.
        if table.name not in KEEP_ON_CLEAR and table.name != "item_events":
            db.execute(delete(table))


def is_empty(db: Session) -> bool:
    """Все демо-записи привязаны к пользователю: нет пользователей - нет демо-данных."""
    return db.scalar(select(User.id).limit(1)) is None


def apply(command: str, name: str, data: dict) -> int:
    if command == "load" and not data:
        print(f"demo {command} '{name}': nothing to load")
        return 0
    with SessionLocal(bind=get_engine()) as db:
        if command in ("reset", "clear"):
            clear(db)
        counts = load(db, data) if command in ("load", "reset") else {}
        db.commit()
    summary = ", ".join(f"{k}={v}" for k, v in counts.items()) or "nothing loaded"
    print(f"demo {command} '{name}': {summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.demo")
    parser.add_argument("command", choices=["load", "reset", "clear"])
    parser.add_argument("--set", dest="name", default=DEFAULT_SET)
    parser.add_argument("--if-empty", action="store_true", help="load only into an empty database")
    args = parser.parse_args(argv)

    if not get_settings().is_dev:
        print("демо-команды доступны только при DEV_MODE=1", file=sys.stderr)
        return 1

    try:
        data = read_set(args.name)
    except (ValueError, yaml.YAMLError) as exc:
        print(f"demo set '{args.name}' is invalid: {exc}", file=sys.stderr)
        return 2
    if data is None:
        print(f"unknown demo set '{args.name}'", file=sys.stderr)
        return 2
    if args.if_empty and args.command == "load":
        settings = get_settings()
        if settings.demo_data != "1":
            print("demo: DEMO_DATA != 1, skipped")
            return 0
        with SessionLocal(bind=get_engine()) as db:
            if not is_empty(db):
                print("demo: database has data, skipped")
                return 0
    return apply(args.command, args.name, data)
