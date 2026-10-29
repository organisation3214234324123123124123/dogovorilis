"""Обезличивание человека чистит журнал кадров живой встречи (FR-52, FR-14).

В `meeting_events.data` лежат полные пункты (исполнитель с именем, исходная строка ведущего).
После обезличивания в журнале встречи не должно остаться ни имени, ни email человека —
ни в data, ни в других столбцах строк журнала. Сам журнал и номера кадров остаются.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import get_settings
from app.modules.items import service as items_service
from app.modules.items.models import DoneBy, ItemStatus
from app.modules.live.models import MeetingEvent
from app.modules.series.models import MeetingStatus
from tests.factories import World, add_member, make_person

pytestmark = pytest.mark.fr("FR-52")

API = "/api/v1"
TOKEN = "a" * 32
FULL_NAME = "Зинаида Кузнецова"
SURNAME = "Кузнецова"
EMAIL = "zinaida.kuznetsova@example.org"


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    get_settings.cache_clear()
    return {"X-Admin-Token": TOKEN}


def journal_text(w: World) -> str:
    rows = w.db.execute(
        text("SELECT m::text FROM meeting_events m WHERE m.meeting_id = :id ORDER BY m.version"),
        {"id": w.meeting.id},
    ).scalars()
    return "\n".join(rows)


def test_anonymize_removes_name_and_email_from_meeting_journal(
    authed: TestClient, world: World, admin: dict[str, str]
) -> None:
    person = make_person(world.db, world.user, world.org, full_name=FULL_NAME, email=EMAIL)
    add_member(world.db, world.series, person)
    world.meeting.status = MeetingStatus.LIVE
    world.db.flush()

    created = authed.post(
        f"{API}/meetings/{world.meeting.id}/items",
        json={"raw_text": f"{FULL_NAME} подготовит отчёт к пятнице"},
    )
    assert created.status_code == 201
    item = created.json()
    patched = authed.patch(
        f"{API}/items/{item['id']}",
        json={"assignee_person_id": str(person.id)},
        headers={"If-Match": str(item["version"])},
    )
    assert patched.status_code == 200

    before = journal_text(world)
    assert FULL_NAME in before, "в журнале должны быть кадры с именем исполнителя"
    kinds = {e.event for e in world.db.query(MeetingEvent).filter_by(meeting_id=world.meeting.id)}
    assert {"item.created", "item.updated"} <= kinds
    count = world.db.query(MeetingEvent).filter_by(meeting_id=world.meeting.id).count()

    resp = authed.post(f"{API}/admin/persons/{person.id}/anonymize", headers=admin)
    assert resp.status_code == 200
    world.db.expire_all()

    after = journal_text(world)
    for secret in (FULL_NAME, SURNAME, EMAIL):
        assert secret not in after
    assert world.db.query(MeetingEvent).filter_by(meeting_id=world.meeting.id).count() == count


ALIAS = re.compile(r"Участник \d+")


def live(w: World) -> None:
    w.meeting.status = MeetingStatus.LIVE
    w.db.flush()


def create_item(
    client: TestClient, w: World, raw_text: str, assignee: Any = None
) -> dict[str, Any]:
    created = client.post(f"{API}/meetings/{w.meeting.id}/items", json={"raw_text": raw_text})
    assert created.status_code == 201
    item = created.json()
    if assignee is not None:
        patched = client.patch(
            f"{API}/items/{item['id']}",
            json={"assignee_person_id": str(assignee.id)},
            headers={"If-Match": str(item["version"])},
        )
        assert patched.status_code == 200
    return item


def last_frame(w: World, item_id: str) -> dict[str, Any]:
    w.db.expire_all()
    row = (
        w.db.query(MeetingEvent)
        .filter(
            MeetingEvent.meeting_id == w.meeting.id,
            MeetingEvent.item_id == uuid.UUID(item_id),
            MeetingEvent.event.like("item.%"),
        )
        .order_by(MeetingEvent.version.desc())
        .first()
    )
    assert row is not None
    return row.data


def anonymize(client: TestClient, admin: dict[str, str], person: Any) -> None:
    assert (
        client.post(f"{API}/admin/persons/{person.id}/anonymize", headers=admin).status_code == 200
    )


def words(value: str) -> list[str]:
    return [w.lower() for w in re.findall(r"\w+", value)]


STRUCTURAL = ("id", "meeting_id", "series_id", "version", "status", "source", "created_at")


@pytest.mark.parametrize(
    ("name", "status"),
    [("Don Lee", ItemStatus.DONE), ("Manual Draft", ItemStatus.DRAFT)],
)
def test_anonymize_keeps_status_and_enums_of_frames(
    authed: TestClient, world: World, admin: dict[str, str], name: str, status: ItemStatus
) -> None:
    person = make_person(world.db, world.user, world.org, full_name=name, email="x1@example.org")
    live(world)
    done = (
        {"done_at": datetime.now(UTC), "done_by": DoneBy.HOST} if status == ItemStatus.DONE else {}
    )
    item = world.item(status=status, raw_text="Подготовить отчёт", **done)
    items_service.emit_updated(world.db, item, world.series.timezone)
    world.db.flush()
    before = last_frame(world, str(item.id))
    assert before["status"] == status.value

    anonymize(authed, admin, person)

    after = last_frame(world, str(item.id))
    for key in STRUCTURAL:
        assert after[key] == before[key], key


def test_anonymize_keeps_ids_when_name_equals_id_prefix(
    authed: TestClient, world: World, admin: dict[str, str]
) -> None:
    live(world)
    item = create_item(authed, world, "Подготовить отчёт")
    name = f"{item['id'][:8]} Тестов"
    person = make_person(world.db, world.user, world.org, full_name=name, email="x2@example.org")
    before = last_frame(world, item["id"])
    event_item_ids = {
        e.item_id for e in world.db.query(MeetingEvent).filter_by(meeting_id=world.meeting.id)
    }

    anonymize(authed, admin, person)

    after = last_frame(world, item["id"])
    assert after["id"] == item["id"]
    assert after["meeting_id"] == str(world.meeting.id)
    assert after["series_id"] == before["series_id"]
    assert after["created_at"] == before["created_at"]
    world.db.expire_all()
    assert {
        e.item_id for e in world.db.query(MeetingEvent).filter_by(meeting_id=world.meeting.id)
    } == event_item_ids


def test_anonymize_keeps_other_person_with_similar_name(
    authed: TestClient, world: World, admin: dict[str, str]
) -> None:
    target = make_person(world.db, world.user, world.org, full_name="Иван Петров")
    other = make_person(world.db, world.user, world.org, full_name="Иванов Сергей")
    for p in (target, other):
        add_member(world.db, world.series, p)
    live(world)
    mine = create_item(authed, world, "Иван Петров подготовит отчёт", target)
    theirs = create_item(authed, world, "Иванов Сергей подготовит отчёт на 30 минут", other)
    theirs_before = last_frame(world, theirs["id"])
    assert theirs_before["assignee"]["full_name"] == "Иванов Сергей"

    anonymize(authed, admin, target)

    assert last_frame(world, theirs["id"]) == theirs_before
    own = last_frame(world, mine["id"])
    assert ALIAS.fullmatch(own["assignee"]["full_name"])
    assert "петров" not in words(own["raw_text"])


def test_anonymize_replaces_assignee_name_by_id(
    authed: TestClient, world: World, admin: dict[str, str]
) -> None:
    person = make_person(world.db, world.user, world.org, full_name=FULL_NAME, email=EMAIL)
    add_member(world.db, world.series, person)
    live(world)
    item = create_item(authed, world, "Подготовить отчёт к пятнице", person)
    assert last_frame(world, item["id"])["assignee"]["id"] == str(person.id)

    anonymize(authed, admin, person)

    assignee = last_frame(world, item["id"])["assignee"]
    assert assignee["id"] == str(person.id)
    assert ALIAS.fullmatch(assignee["full_name"])


def test_anonymize_replaces_assignee_name_by_id_even_if_person_was_renamed(
    authed: TestClient, world: World, admin: dict[str, str]
) -> None:
    person = make_person(world.db, world.user, world.org, full_name="Зина Кузнецова")
    add_member(world.db, world.series, person)
    live(world)
    item = create_item(authed, world, "Подготовить отчёт", person)
    person.full_name = "Зинаида Кузнецова"
    world.db.flush()

    anonymize(authed, admin, person)

    assignee = last_frame(world, item["id"])["assignee"]
    assert assignee["id"] == str(person.id)
    assert ALIAS.fullmatch(assignee["full_name"])


def test_anonymize_free_text_replaces_whole_words_only(
    authed: TestClient, world: World, admin: dict[str, str]
) -> None:
    person = make_person(world.db, world.user, world.org, full_name="Ли Мин")
    add_member(world.db, world.series, person)
    live(world)
    item = create_item(authed, world, "МИН обсудит левый отчёт 30 минут", person)

    anonymize(authed, admin, person)

    text_after = last_frame(world, item["id"])["raw_text"]
    assert "минут" in text_after
    assert "левый" in text_after
    assert "мин" not in words(text_after)


def test_anonymize_free_text_replaces_name_word_ignoring_case(
    authed: TestClient, world: World, admin: dict[str, str]
) -> None:
    person = make_person(world.db, world.user, world.org, full_name="Иван Петров")
    add_member(world.db, world.series, person)
    live(world)
    item = create_item(authed, world, "иван и ПЕТРОВ обсудят с Ивановым отчёт", person)

    anonymize(authed, admin, person)

    text_after = last_frame(world, item["id"])["raw_text"]
    assert not {"иван", "петров"} & set(words(text_after))
    assert ALIAS.search(text_after)
    assert "Ивановым" in text_after


def test_anonymize_short_name_not_searched_in_free_text_but_replaced_by_id(
    authed: TestClient, world: World, admin: dict[str, str]
) -> None:
    person = make_person(world.db, world.user, world.org, full_name="Ян")
    add_member(world.db, world.series, person)
    live(world)
    item = create_item(authed, world, "Январский релиз: левый отчёт", person)

    anonymize(authed, admin, person)

    frame = last_frame(world, item["id"])
    assert frame["raw_text"] == "Январский релиз: левый отчёт"
    assert frame["assignee"]["id"] == str(person.id)
    assert ALIAS.fullmatch(frame["assignee"]["full_name"])


def test_anonymize_replaces_email_whole_in_free_text(
    authed: TestClient, world: World, admin: dict[str, str]
) -> None:
    person = make_person(world.db, world.user, world.org, full_name=FULL_NAME, email=EMAIL)
    add_member(world.db, world.series, person)
    live(world)
    item = create_item(authed, world, f"Писать на {EMAIL.upper()} до пятницы")

    anonymize(authed, admin, person)

    text_after = last_frame(world, item["id"])["raw_text"]
    assert "example.org" not in text_after.lower()
    assert "kuznetsova" not in text_after.lower()
    assert text_after.endswith(" до пятницы")
