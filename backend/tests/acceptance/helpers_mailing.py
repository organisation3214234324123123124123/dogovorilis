"""Общие помощники приёмочных тестов закрытия встречи, рассылки, писем и ссылок.

Подменяют отправку писем (sender.deliver) и собирают данные для рассылки.
Ссылка в письме: ``{PUBLIC_BASE_URL}/p/<token>?action=<accept|reschedule|decline|done>`` (FR-32.4).
"""

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import AccessLink, AnalyticsEvent, Meeting, MeetingParticipant, Notification
from app.modules.items.models import Item, ItemStatus
from app.modules.series.models import MeetingStatus
from tests.factories import World, make_person

TUESDAY = datetime(2030, 1, 15, 12, 0, tzinfo=UTC)
TUESDAY_ISO = "2030-01-15T12:00:00+00:00"
FRIDAY_ISO = "2030-01-18T12:00:00+00:00"
LINK_RE = re.compile(r"/p/([A-Za-z0-9_-]{20,})(\?[^\s\"'<>]*)?")
API = "/api/v1"


_ACTIVE: list["Mailbox"] = []


class Mailbox:
    """Перехватчик SMTP: письма так, как их получит адресат; токены берутся только отсюда."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.letters: list[dict[str, Any]] = []
        self.heads: list[Notification] = []

    def run(self, now: datetime | None = None) -> int:
        from app.modules.notifications.sender import send_due

        return send_due(self.db, now or datetime.now(UTC))

    def to(self, person) -> list[dict[str, Any]]:
        return [x for x in self.letters if x["to"] == person.email]

    def text_of(self, letter: dict[str, Any]) -> str:
        return "\n".join([letter["subject"], letter["text"], letter["html"]])

    def tokens(self, letter: dict[str, Any]) -> list[str]:
        return tokens_in(self.text_of(letter))

    def first_token(self, person) -> str:
        if not self.to(person):
            self.run()
        (token,) = set(self.tokens(self.to(person)[0]))
        return token

    def letter_for(self, mail: Notification) -> dict[str, Any]:
        """Письмо, в котором ушла строка mail (доставляет всё, что к её сроку готово)."""
        if mail.sent_at is None:
            self.run()
            self.db.refresh(mail)
            stamps = sorted(
                {m.scheduled_for for m in mails(self.db) if m.sent_at is None}
                - {m.scheduled_for for m in mails(self.db) if m.scheduled_for > mail.scheduled_for}
            )
            for at in stamps:
                self.run(at)
        found = [
            letter
            for letter, head in zip(self.letters, self.heads, strict=True)
            if head.type == mail.type and head.recipient_person_id == mail.recipient_person_id
        ]
        assert found, f"письмо {mail.type} не отправлено"
        return found[-1]


@pytest.fixture(autouse=True)
def mailbox(db: Session, monkeypatch):
    """Настоящий путь отправщика до smtplib: SMTP_HOST задан, сокет подменён перехватчиком."""
    import smtplib

    from app.core.config import get_settings
    from app.modules.notifications import sender

    box = Mailbox(db)
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://app.test")
    get_settings.cache_clear()
    current: list[Notification] = []

    class FakeSMTP:
        def __init__(self, *_a: Any, **_kw: Any) -> None: ...

        def __enter__(self):
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        def has_extn(self, _name: str) -> bool:
            return False

        def login(self, *_a: Any) -> None: ...

        def send_message(self, message) -> None:
            body = message.get_body(("plain",))
            html = message.get_body(("html",))
            box.letters.append(
                {
                    "to": message["To"],
                    "subject": message["Subject"],
                    "text": body.get_content() if body else "",
                    "html": html.get_content() if html else "",
                }
            )
            box.heads.append(current[-1])

    real_deliver = sender.deliver

    def deliver(mail, *args: Any, **kwargs: Any) -> None:
        current.append(mail)
        return real_deliver(mail, *args, **kwargs)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(sender, "deliver", deliver)
    _ACTIVE.append(box)
    yield box
    _ACTIVE.pop()
    get_settings.cache_clear()


def payload_text(mail: Notification) -> str:
    """Письмо, каким оно ушло получателю, плюс то, что лежит в payload."""
    box = _ACTIVE[-1]
    return "\n".join(
        [json.dumps(mail.payload, ensure_ascii=False), box.text_of(box.letter_for(mail))]
    )


def mails(
    db: Session, kind: str | None = None, *, meeting: Meeting | None = None, **filters: Any
) -> list[Notification]:
    query = select(Notification).order_by(Notification.created_at, Notification.dedupe_key)
    if kind:
        query = query.where(Notification.type == kind)
    if meeting is not None:
        query = query.where(Notification.meeting_id == meeting.id)
    for column, value in filters.items():
        query = query.where(getattr(Notification, column) == value)
    return list(db.scalars(query))


def events(db: Session, name: str, **filters: Any) -> list[AnalyticsEvent]:
    query = select(AnalyticsEvent).where(AnalyticsEvent.name == name)
    for column, value in filters.items():
        query = query.where(getattr(AnalyticsEvent, column) == value)
    return list(db.scalars(query))


def tokens_in(text: str) -> list[str]:
    return [m.group(1) for m in LINK_RE.finditer(text)]


def actions_in(text: str) -> set[str]:
    return set(re.findall(r"[?&]action=(accept|reschedule|decline|done)", text))


def digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def add_participant(world: World, name: str | None = None):
    person = make_person(world.db, world.user, world.org, full_name=name or "Участник")
    world.db.add(MeetingParticipant(meeting_id=world.meeting.id, person_id=person.id))
    world.db.flush()
    return person


def make_closing(world: World, *, start: datetime = TUESDAY) -> None:
    world.meeting.status = MeetingStatus.CLOSING
    world.meeting.planned_start = start - timedelta(minutes=30)
    world.meeting.planned_end = start + timedelta(minutes=25)
    world.meeting.started_at = start - timedelta(minutes=30)
    world.meeting.closure_opened_at = start
    world.db.flush()


def draft(world: World, assignee, due: date | None, *, agreed: bool = False, what: str) -> Item:
    return world.item(
        what=what,
        assignee_person_id=assignee.id if assignee else None,
        due_date=due,
        accepted_in_meeting=agreed,
        status=ItemStatus.DRAFT,
    )


def send(client: TestClient, world: World, *, confirm: bool = True, expect: int | None = 200):
    resp = client.post(
        f"{API}/meetings/{world.meeting.id}/send", json={"confirm_open_gaps": confirm}
    )
    if expect is not None:
        assert resp.status_code == expect, resp.text
    return resp


def link_for(db: Session, person, meeting: Meeting, *, active: bool = True) -> list[AccessLink]:
    query = select(AccessLink).where(
        AccessLink.person_id == person.id, AccessLink.meeting_id == meeting.id
    )
    if active:
        query = query.where(AccessLink.revoked_at.is_(None))
    return list(db.scalars(query))


def token_of(db: Session, person, meeting: Meeting) -> str:
    """Токен, пришедший исполнителю в письме (письма доставляются по требованию)."""
    return _ACTIVE[-1].first_token(person)


def hash_is_stored(db: Session, token: str) -> bool:
    """SHA-256 токена лежит в какой-либо bytea-колонке (схема хранения не важна тесту)."""
    columns = db.execute(
        text(
            "select table_name, column_name from information_schema.columns"
            " where table_schema = 'public' and data_type = 'bytea'"
        )
    ).all()
    return any(
        db.execute(
            text(f'select count(*) from "{table}" where "{column}" = :h'), {"h": digest(token)}
        ).scalar_one()
        for table, column in columns
    )


def raw_token_stored_anywhere(db: Session, token: str) -> bool:
    """Токен как текст в любой строке любой таблицы (колонки text/json/varchar целиком)."""
    tables = db.execute(
        text("select tablename from pg_tables where schemaname = 'public'")
    ).scalars()
    return any(
        db.execute(
            text(f'select count(*) from "{table}" t where t::text like :p'), {"p": f"%{token}%"}
        ).scalar_one()
        for table in tables
    )


def stored_payload(mail: Notification) -> str:
    return json.dumps(mail.payload, ensure_ascii=False)


from freezegun import freeze_time  # noqa: E402

from tests.conftest import login_as  # noqa: E402

DUE_FRIDAY = date(2030, 1, 25)
DUE_MONDAY = date(2030, 1, 21)


class Scenario:
    """Закрытие во вторник 15:00 по Москве: три участника, три пункта в draft."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.ivan = world.person
        self.ivan.full_name = "Иван Петров"
        self.anna = add_participant(world, "Анна Смирнова")
        self.oleg = add_participant(world, "Олег Орлов")
        world.db.add(MeetingParticipant(meeting_id=world.meeting.id, person_id=self.ivan.id))
        make_closing(world)
        self.pending = draft(world, self.ivan, DUE_FRIDAY, what="Выгрузка отчёта по оттоку")
        self.agreed = draft(world, self.anna, DUE_MONDAY, agreed=True, what="Согласовать договор")
        self.unassigned = draft(world, None, None, what="Найти подрядчика")
        world.db.flush()


@pytest.fixture
def frozen():
    with freeze_time(TUESDAY_ISO) as clock:
        yield clock


@pytest.fixture
def host(frozen, client: TestClient, world: World) -> TestClient:
    login_as(client, world.user)
    return client


@pytest.fixture
def sc(frozen, world: World) -> Scenario:
    return Scenario(world)
