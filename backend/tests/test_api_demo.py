"""Тесты демо-ручек: нейросеть, почта, планировщик, события живой панели.

Тесты подменяют `app.modules.demo.service.get_llm_client` и
`app.modules.demo.service.mailbox_transport` (`httpx.BaseTransport | None`, по умолчанию None).
Адрес ящика (env DEMO_MAILBOX_URL) читается через `get_settings()` в момент вызова.
Роутер подключается в `create_app()` только при `is_dev and demo_data == "1"`.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.llm.models import LlmCall, LlmOutcome, LlmTask
from app.modules.notifications.models import Notification, NotificationStatus, NotificationType
from app.modules.series.models import MeetingStatus
from tests.conftest import login_as
from tests.factories import (
    World,
    add_member,
    make_item,
    make_meeting,
    make_notification,
    make_organization,
    make_person,
    make_series,
    make_user,
)
from tests.llm_fakes import FakeLLM, reply

pytestmark = pytest.mark.fr("infra")

API = "/api/v1/demo"
MAILBOX = "http://mailpit.test:8025"
ENDPOINTS = [
    ("post", f"{API}/llm/check", {"text": "Маша пришлёт прайс"}),
    ("get", f"{API}/llm/calls", None),
    ("get", f"{API}/mailbox", None),
    ("post", f"{API}/scheduler/fast-forward", {}),
    ("get", f"{API}/meetings/00000000-0000-0000-0000-000000000001/live-events", None),
]


@pytest.fixture
def demo_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_DATA", "1")


@pytest.fixture
def demo_client(demo_on: None, client: TestClient) -> TestClient:
    return client


@pytest.fixture
def dc(demo_client: TestClient, world: World) -> TestClient:
    login_as(demo_client, world.user)
    return demo_client


@pytest.fixture
def mailbox_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MAILBOX_URL", MAILBOX)


def use_llm(monkeypatch: pytest.MonkeyPatch, fake: FakeLLM) -> None:
    monkeypatch.setattr("app.modules.demo.service.get_llm_client", lambda: fake)


def use_mailbox(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(
        "app.modules.demo.service.mailbox_transport", httpx.MockTransport(handler), raising=True
    )


def other_series(db: Session):
    stranger = make_user(db)
    return make_series(db, stranger)


# --- видимость ---------------------------------------------------------------------------


def test_routes_absent_without_demo_data(authed: TestClient) -> None:
    assert authed.get(f"{API}/llm/calls").status_code == 404
    paths = authed.get("/api/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith(API)]


def test_routes_present_with_demo_data(dc: TestClient) -> None:
    assert dc.get(f"{API}/llm/calls").status_code == 200


def test_openapi_has_demo_tag_after_auth(demo_client: TestClient) -> None:
    schema = demo_client.get("/api/openapi.json").json()
    names = [t["name"] for t in schema["tags"]]
    assert names.index("demo") == names.index("auth") + 1
    assert any(p.startswith(API) for p in schema["paths"])


def test_openapi_without_demo_data_has_no_demo_tag(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()
    assert "demo" not in [t["name"] for t in schema["tags"]]


@pytest.mark.parametrize(("method", "url", "body"), ENDPOINTS)
def test_demo_endpoints_require_login(demo_client: TestClient, method, url, body) -> None:
    resp = getattr(demo_client, method)(url, **({"json": body} if body is not None else {}))
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


# --- llm/check ---------------------------------------------------------------------------


def test_llm_check_stub(dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.person.full_name = "Анна Петрова"
    add_member(world.db, world.series, world.person)
    use_llm(monkeypatch, FakeLLM(reply()))
    from app.modules.llm.client import StubLLMClient

    monkeypatch.setattr("app.modules.demo.service.get_llm_client", lambda: StubLLMClient())
    resp = dc.post(
        f"{API}/llm/check",
        json={"text": "Анна разберётся с выгрузкой", "series_id": str(world.series.id)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["live"] is False
    assert "заглушка" in body["verdict"]
    assert body["rules"]["assignee"] == "Анна Петрова"
    assert {g["type"] for g in body["rules"]["gaps"]} >= {"no_due", "vague_action"}
    assert all(g["question"] for g in body["rules"]["gaps"])


def test_llm_check_live_fake(dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.person.full_name = "Анна Петрова"
    add_member(world.db, world.series, world.person)
    use_llm(
        monkeypatch,
        FakeLLM(reply(assignee_id="p1", done_criteria="файл выгрузки")),
    )
    resp = dc.post(
        f"{API}/llm/check",
        json={"text": "Анна разберётся с выгрузкой", "series_id": str(world.series.id)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["live"] is True
    assert (body["provider"], body["model"]) == ("fake", "fake-1")
    assert isinstance(body["elapsed_ms"], int)
    assert "fake-1" in body["verdict"]
    assert body["llm"]["outcome"] == "ok"
    assert body["llm"]["assignee"] == "Анна Петрова"
    assert body["llm"]["vague_action"] is True
    assert body["llm"]["done_criteria"] == "файл выгрузки"


def test_llm_check_writes_llm_call(
    dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    before = len(world.db.scalars(select(LlmCall)).all())
    assert dc.post(f"{API}/llm/check", json={"text": "Маша пришлёт прайс"}).status_code == 200
    calls = world.db.scalars(select(LlmCall)).all()
    assert len(calls) == before + 1
    assert any(c.meeting_id is None and c.provider == "fake" for c in calls)


def test_llm_check_foreign_series_404(
    dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    resp = dc.post(
        f"{API}/llm/check",
        json={"text": "Маша пришлёт прайс", "series_id": str(other_series(world.db).id)},
    )
    assert resp.status_code == 404


def test_llm_check_validates_text(dc: TestClient) -> None:
    assert dc.post(f"{API}/llm/check", json={"text": ""}).status_code == 422
    assert dc.post(f"{API}/llm/check", json={"text": "я" * 501}).status_code == 422


# --- llm/calls ---------------------------------------------------------------------------


def test_llm_calls_latest_first_with_limit(
    dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    for _ in range(3):
        dc.post(f"{API}/llm/check", json={"text": "Маша пришлёт прайс"})
    rows = dc.get(f"{API}/llm/calls", params={"limit": 2}).json()
    assert len(rows) == 2
    assert rows[0]["created_at"] >= rows[1]["created_at"]
    assert {"task", "provider", "model", "latency_ms", "outcome", "meeting_id"} <= set(rows[0])
    assert dc.get(f"{API}/llm/calls", params={"limit": 0}).status_code == 422
    assert dc.get(f"{API}/llm/calls", params={"limit": 101}).status_code == 422


def test_llm_calls_only_own_meetings_or_without_meeting(dc: TestClient, world: World) -> None:
    foreign_meeting = make_meeting(world.db, other_series(world.db))
    for meeting_id in (world.meeting.id, foreign_meeting.id, None):
        world.db.add(
            LlmCall(
                meeting_id=meeting_id,
                task=LlmTask.PARSE,
                provider="fake",
                model="m",
                outcome=LlmOutcome.OK,
            )
        )
    world.db.flush()
    ids = {r["meeting_id"] for r in dc.get(f"{API}/llm/calls").json()}
    assert str(world.meeting.id) in ids
    assert None in ids
    assert str(foreign_meeting.id) not in ids


def add_checks(world: World, count: int, age_s: int) -> None:
    created = datetime.now(UTC) - timedelta(seconds=age_s)
    for _ in range(count):
        world.db.add(
            LlmCall(
                meeting_id=None,
                task=LlmTask.PARSE,
                provider="fake",
                model="m",
                outcome=LlmOutcome.OK,
                created_at=created,
            )
        )
    world.db.flush()


def test_llm_check_rate_limited_after_20_per_minute(
    dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    add_checks(world, 20, 5)
    resp = dc.post(f"{API}/llm/check", json={"text": "Маша пришлёт прайс"})
    assert resp.status_code == 429
    assert resp.json()["code"] == "RATE_LIMITED"
    assert int(resp.headers["Retry-After"]) >= 1


def test_llm_check_busy_lock_returns_429(
    dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Замок проверки: `app.modules.llm.repository.CHECK_LOCK_KEY` (int, один bigint-ключ
    pg_try_advisory_xact_lock). Тест держит его на втором соединении."""
    from sqlalchemy import text

    from app.core.db import get_engine
    from app.modules.llm import repository

    use_llm(monkeypatch, FakeLLM(reply()))
    key = repository.CHECK_LOCK_KEY
    with get_engine().connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        try:
            resp = dc.post(f"{API}/llm/check", json={"text": "Маша пришлёт прайс"})
        finally:
            other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            other.commit()
    assert resp.status_code == 429
    assert resp.json()["code"] == "RATE_LIMITED"
    assert resp.headers["Retry-After"] == "1"
    assert "Проверка уже идёт" in resp.json()["detail"]


def test_llm_check_old_calls_do_not_count(
    dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    add_checks(world, 20, 120)
    assert dc.post(f"{API}/llm/check", json={"text": "Маша пришлёт прайс"}).status_code == 200


# --- mailbox -----------------------------------------------------------------------------


def test_mailbox_disabled_without_url(dc: TestClient) -> None:
    body = dc.get(f"{API}/mailbox").json()
    assert body["enabled"] is False
    assert body["letters"] == []
    assert "DEMO_MAILBOX_URL" in body["hint"]


def test_mailbox_parses_letters_and_links(
    mailbox_url: None, dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = "11111111-1111-1111-1111-111111111111"
    link = f"http://localhost:5173/p/TOKEN123?action=accept&item={item}&n=2"
    texts = {
        "m1": f"Привет!\n{link}\nещё раз {link}\nСтраница: http://localhost:5173/p/TOKEN123",
        "m2": "Без ссылок",
    }
    world.person.email = "anna@example.com"
    add_member(world.db, world.series, world.person)
    message_ids = {mid: own_meeting_mail(world) for mid in texts}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/messages":
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "ID": mid,
                            "MessageID": message_ids[mid],
                            "Created": "2026-11-04T10:00:00Z",
                            "To": [{"Address": "anna@example.com"}],
                            "Subject": f"Тема {mid}",
                        }
                        for mid in texts
                    ]
                },
            )
        return httpx.Response(200, json={"Text": texts[request.url.path.rsplit("/", 1)[1]]})

    use_mailbox(monkeypatch, handler)
    body = dc.get(f"{API}/mailbox", params={"limit": 5}).json()
    assert body["enabled"] is True
    first, second = body["letters"]
    assert first["to"] == ["anna@example.com"]
    assert first["subject"] == "Тема m1"
    assert first["text"] == texts["m1"]
    assert len(first["links"]) == 2
    assert first["links"][0] == {
        "url": link,
        "token": "TOKEN123",
        "action": "accept",
        "item_id": item,
        "kind": "2",
    }
    assert first["links"][1]["token"] == "TOKEN123"
    assert first["links"][1]["action"] is None
    assert second["links"] == []


def own_mail(w: World, type_=NotificationType.SUMMARY, **kw) -> str:
    """Уведомление ведущего w; возвращает MessageID, как его отдаёт Mailpit."""
    mail = make_notification(w.db, type=type_, **kw)
    return f"{mail.id}@dogovorilis"


def own_meeting_mail(w: World) -> str:
    return own_mail(w, recipient_person_id=w.person.id, meeting_id=w.meeting.id)


def foreign_host_mail(w: World, type_=NotificationType.SUMMARY) -> tuple[str, str]:
    """Письмо другого ведущего: (MessageID, e-mail получателя)."""
    other = make_user(w.db)
    if type_ == NotificationType.PASSWORD_RESET:
        return own_mail(w, type_, recipient_user_id=other.id), other.email
    org = make_organization(w.db, other)
    person = make_person(w.db, other, org)
    meeting = make_meeting(w.db, make_series(w.db, other))
    return own_mail(w, type_, recipient_person_id=person.id, meeting_id=meeting.id), person.email


def mailpit(letters: list[tuple[str, str, str | None]], seen: list[httpx.Request]):
    """letters: (тема, адрес получателя, MessageID) от новых к старым; ID в Mailpit = тема."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/messages":
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "ID": subject,
                            "MessageID": message_id,
                            "Created": "2026-11-04T10:00:00Z",
                            "To": [{"Address": to}],
                            "Subject": subject,
                        }
                        for subject, to, message_id in letters
                    ]
                },
            )
        return httpx.Response(200, json={"Text": "текст"})

    return handler


def subjects(client: TestClient) -> list[str]:
    return [x["subject"] for x in client.get(f"{API}/mailbox").json()["letters"]]


def test_mailbox_shows_own_letter_by_message_id(
    mailbox_url: None, dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    letters = [("own", "nobody@other.org", own_meeting_mail(world))]
    use_mailbox(monkeypatch, mailpit(letters, []))
    assert subjects(dc) == ["own"]


def test_mailbox_hides_foreign_host_even_if_address_is_in_own_series(
    mailbox_url: None, dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    message_id, email = foreign_host_mail(world)
    world.person.email = email
    add_member(world.db, world.series, world.person)
    seen: list[httpx.Request] = []
    use_mailbox(monkeypatch, mailpit([("foreign", email, message_id)], seen))
    assert subjects(dc) == []
    assert not [r for r in seen if r.url.path.endswith("/foreign")]


def test_mailbox_hides_letter_without_message_id(
    mailbox_url: None, dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.person.email = "anna@example.com"
    add_member(world.db, world.series, world.person)
    letters = [
        ("none", "anna@example.com", None),
        ("junk", "anna@example.com", "abc@elsewhere"),
    ]
    use_mailbox(monkeypatch, mailpit(letters, []))
    assert subjects(dc) == []


def test_mailbox_hides_password_reset_of_other_host(
    mailbox_url: None, dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    message_id, email = foreign_host_mail(world, NotificationType.PASSWORD_RESET)
    world.person.email = email
    add_member(world.db, world.series, world.person)
    use_mailbox(monkeypatch, mailpit([("reset", email, message_id)], []))
    assert subjects(dc) == []


def test_mailbox_shows_own_password_reset(
    mailbox_url: None, dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    message_id = own_mail(world, NotificationType.PASSWORD_RESET, recipient_user_id=world.user.id)
    use_mailbox(monkeypatch, mailpit([("reset", world.user.email, message_id)], []))
    assert subjects(dc) == ["reset"]


def test_mailbox_limit_applies_after_filter(
    mailbox_url: None, dc: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = foreign_host_mail(world)[0]
    seen: list[httpx.Request] = []
    letters = [("f1", "x@other.org", foreign), ("f2", "y@other.org", None)] + [
        (f"own{i}", "x@other.org", own_meeting_mail(world)) for i in range(3)
    ]
    use_mailbox(monkeypatch, mailpit(letters, seen))
    body = dc.get(f"{API}/mailbox", params={"limit": 2}).json()
    assert [x["subject"] for x in body["letters"]] == ["own0", "own1"]
    assert seen[0].url.params["limit"] == "50"
    detail = [r for r in seen if r.url.path.startswith("/api/v1/message/")]
    assert len(detail) == 2


def test_mailbox_down_returns_hint(
    mailbox_url: None, dc: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    use_mailbox(monkeypatch, handler)
    resp = dc.get(f"{API}/mailbox")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["letters"] == []
    assert body["hint"].startswith("Mailpit не отвечает")


# --- scheduler/fast-forward --------------------------------------------------------------


def queued(w: World, type_=NotificationType.DUE_TOMORROW, **kw) -> Notification:
    item = make_item(w.db, w.meeting, what="Отчёт")
    kw.setdefault("scheduled_for", datetime.now(UTC) + timedelta(days=1))
    return make_notification(
        w.db,
        type=type_,
        recipient_person_id=w.person.id,
        meeting_id=w.meeting.id,
        item_id=item.id,
        **kw,
    )


def test_fast_forward_moves_queued_own_mails(dc: TestClient, world: World) -> None:
    mail = queued(world)
    sent = queued(world, status=NotificationStatus.SENT)
    other_type = queued(world, NotificationType.SUMMARY)
    stranger_user = make_user(world.db)
    foreign_meeting = make_meeting(world.db, make_series(world.db, stranger_user))
    foreign_person = make_person(
        world.db, stranger_user, make_organization(world.db, stranger_user)
    )
    foreign = make_notification(
        world.db,
        type=NotificationType.DUE_TOMORROW,
        recipient_person_id=foreign_person.id,
        meeting_id=foreign_meeting.id,
        scheduled_for=datetime.now(UTC) + timedelta(days=1),
    )
    old_sent_at = sent.scheduled_for
    resp = dc.post(f"{API}/scheduler/fast-forward", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert [m["type"] for m in body["moved"]] == ["DUE_TOMORROW"]
    assert {"recipient_email", "item_what", "was_scheduled_for"} <= set(body["moved"][0])
    assert "Celery Beat" in body["hint"]
    for row in (mail, sent, other_type, foreign):
        world.db.refresh(row)
    assert abs(mail.scheduled_for - datetime.now(UTC)) < timedelta(minutes=1)
    assert sent.scheduled_for == old_sent_at
    assert other_type.scheduled_for > datetime.now(UTC) + timedelta(hours=1)
    assert foreign.scheduled_for > datetime.now(UTC) + timedelta(hours=1)


def test_fast_forward_empty(dc: TestClient) -> None:
    body = dc.post(f"{API}/scheduler/fast-forward", json={}).json()
    assert body["moved"] == []
    assert body["hint"] == "Запланированных писем этих типов нет"


def test_fast_forward_foreign_series_404(dc: TestClient, world: World) -> None:
    own = dc.post(f"{API}/scheduler/fast-forward", json={"series_id": str(world.series.id)})
    assert own.status_code == 200
    resp = dc.post(
        f"{API}/scheduler/fast-forward", json={"series_id": str(other_series(world.db).id)}
    )
    assert resp.status_code == 404


def test_fast_forward_rejects_empty_types(dc: TestClient) -> None:
    assert dc.post(f"{API}/scheduler/fast-forward", json={"types": []}).status_code == 422


# --- live-events -------------------------------------------------------------------------


def test_live_events_after_item_created(dc: TestClient, world: World) -> None:
    world.meeting.status = MeetingStatus.LIVE
    world.db.flush()
    resp = dc.post(
        f"/api/v1/meetings/{world.meeting.id}/items", json={"raw_text": "Иван подготовит отчёт"}
    )
    assert resp.status_code == 201
    body = dc.get(f"{API}/meetings/{world.meeting.id}/live-events").json()
    assert body["version"] >= 1
    created = [f for f in body["frames"] if f["event"] == "item.created"]
    assert created
    assert "version" in created[0]["data"]
    assert (
        dc.get(
            f"{API}/meetings/{world.meeting.id}/live-events", params={"since": body["version"]}
        ).json()["frames"]
        == []
    )


def test_live_events_foreign_meeting_404(dc: TestClient, world: World) -> None:
    assert dc.get(f"{API}/meetings/{world.meeting.id}/live-events").status_code == 200
    meeting = make_meeting(world.db, other_series(world.db))
    assert dc.get(f"{API}/meetings/{meeting.id}/live-events").status_code == 404
