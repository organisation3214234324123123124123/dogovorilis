import hashlib
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.models import AnalyticsEvent
from app.modules.items.models import ItemStatus
from tests.factories import World, make_access_link, make_person

pytestmark = pytest.mark.fr("FR-31")

TOKEN = "tok-" + "b" * 28
URL = f"/api/v1/p/{TOKEN}"
LATE_UTC = datetime(2030, 1, 15, 22, 0, tzinfo=UTC)


@pytest.fixture
def link(world: World):
    token_hash = hashlib.sha256(TOKEN.encode()).digest()
    return make_access_link(world.db, world.person, world.meeting, token_hash=token_hash)


@pytest.fixture
def own_item(world: World, link):
    return world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.PENDING,
        sent_at=datetime.now(UTC),
        what="Mine",
    )


def csrf_headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.get(URL).json()["csrf_token"]}


def test_page_names_the_recipient(client: TestClient, world: World, own_item) -> None:
    body = client.get(URL).json()
    assert body["recipient_name"] == world.person.full_name
    assert "person_name" not in body


def test_page_has_no_timezone_and_no_meeting_start(client: TestClient, own_item) -> None:
    body = client.get(URL).json()
    assert "timezone" not in body
    assert "meeting_start" not in body


def test_meeting_date_is_the_date_in_series_timezone(
    client: TestClient, world: World, own_item
) -> None:
    world.meeting.planned_start = LATE_UTC
    world.meeting.planned_end = LATE_UTC + timedelta(minutes=30)
    world.db.flush()
    assert world.series.timezone == "Europe/Moscow"
    assert client.get(URL).json()["meeting_date"] == "2030-01-16"


def test_meeting_date_is_a_plain_date(client: TestClient, own_item) -> None:
    value = client.get(URL).json()["meeting_date"]
    assert date.fromisoformat(value).isoformat() == value


def test_page_lists_own_items_only(client: TestClient, world: World, own_item) -> None:
    other = make_person(world.db, world.user, world.org)
    world.item(assignee_person_id=other.id, status=ItemStatus.PENDING, sent_at=datetime.now(UTC))
    body = client.get(URL).json()
    assert [i["id"] for i in body["items"]] == [str(own_item.id)]
    assert body["recipient_name"] == world.person.full_name


def test_preselected_action_is_null_without_query(client: TestClient, own_item) -> None:
    assert client.get(URL).json()["preselected_action"] is None


@pytest.mark.parametrize("action", ["accept", "reschedule", "decline", "done"])
def test_preselected_action_comes_from_query(client: TestClient, own_item, action: str) -> None:
    assert client.get(f"{URL}?action={action}").json()["preselected_action"] == action


def test_unknown_action_in_query_is_not_preselected(client: TestClient, own_item) -> None:
    resp = client.get(f"{URL}?action=explode")
    assert resp.status_code == 200
    assert resp.json()["preselected_action"] is None


def test_allowed_actions_by_status(client: TestClient, world: World, own_item) -> None:
    accepted = world.item(
        assignee_person_id=world.person.id, status=ItemStatus.ACCEPTED, sent_at=datetime.now(UTC)
    )
    done = world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.DONE,
        sent_at=datetime.now(UTC),
        done_at=datetime.now(UTC),
    )
    by_id = {i["id"]: i for i in client.get(URL).json()["items"]}
    assert by_id[str(own_item.id)]["allowed_actions"] == ["accept", "reschedule", "decline"]
    assert by_id[str(accepted.id)]["allowed_actions"] == ["done", "reschedule", "decline"]
    assert by_id[str(done.id)]["allowed_actions"] == []


def test_item_has_no_actions_or_is_overdue(client: TestClient, own_item) -> None:
    (item,) = client.get(URL).json()["items"]
    assert "is_overdue" not in item
    assert "actions" not in item


def test_overdue_item_still_has_no_is_overdue(client: TestClient, world: World, link) -> None:
    world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.ACCEPTED,
        sent_at=datetime.now(UTC),
        due_date=date(2020, 1, 1),
        what="Old",
    )
    (item,) = client.get(URL).json()["items"]
    assert "is_overdue" not in item


@pytest.mark.parametrize("flag", [True, False])
def test_accepted_in_meeting_flag_is_exposed(
    client: TestClient, world: World, link, flag: bool
) -> None:
    world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.ACCEPTED,
        sent_at=datetime.now(UTC),
        accepted_in_meeting=flag,
        what="Agreed",
    )
    (item,) = client.get(URL).json()["items"]
    assert item["accepted_in_meeting"] is flag


def test_what_falls_back_to_raw_text(client: TestClient, world: World, link) -> None:
    world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.PENDING,
        sent_at=datetime.now(UTC),
        what=None,
        raw_text="Сырой текст пункта",
    )
    (item,) = client.get(URL).json()["items"]
    assert item["what"] == "Сырой текст пункта"


def test_what_is_never_null(client: TestClient, world: World, link) -> None:
    world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.PENDING,
        sent_at=datetime.now(UTC),
        what=None,
    )
    (item,) = client.get(URL).json()["items"]
    assert isinstance(item["what"], str) and item["what"]


@pytest.mark.fr("FR-32")
def test_csrf_cookie_is_named_csrf(client: TestClient, own_item) -> None:
    resp = client.get(URL)
    assert client.cookies.get("csrf") == resp.json()["csrf_token"]
    assert "csrf_token=" not in resp.headers["set-cookie"]


@pytest.mark.fr("FR-32")
def test_csrf_cookie_attributes(client: TestClient, own_item) -> None:
    cookie = client.get(URL).headers["set-cookie"].lower()
    assert cookie.startswith("csrf=")
    assert "samesite=strict" in cookie
    assert "httponly" in cookie
    assert "path=/api/v1/p" in cookie


@pytest.mark.fr("FR-32")
def test_post_fails_when_cookie_has_the_old_name(client: TestClient, own_item) -> None:
    token = client.get(URL).json()["csrf_token"]
    client.cookies.clear()
    client.cookies.set("csrf_token", token, path="/api/v1/p")
    resp = client.post(f"{URL}/items/{own_item.id}/accept", headers={"X-CSRF-Token": token})
    assert resp.status_code == 403
    assert resp.json()["code"] == "CSRF_INVALID"


@pytest.mark.parametrize("method", ["get", "post"])
def test_unknown_token_is_410_link_expired(client: TestClient, link, method: str) -> None:
    url = "/api/v1/p/unknown-token"
    if method == "get":
        resp = client.get(url)
    else:
        resp = client.post(f"{url}/items/{link.id}/accept", headers={"X-CSRF-Token": "x"})
    assert resp.status_code == 410
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == "LINK_EXPIRED"


def test_unknown_token_discloses_no_names(client: TestClient, world: World, link) -> None:
    resp = client.get("/api/v1/p/unknown-token")
    assert resp.status_code == 410
    body = resp.json()
    assert not body.get("host_name")
    assert "items" not in body
    assert world.user.full_name not in str(body)


@pytest.mark.parametrize("action", ["accept", "reschedule", "decline"])
def test_post_returns_the_whole_page(
    client: TestClient, world: World, own_item, action: str
) -> None:
    second = world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.PENDING,
        sent_at=datetime.now(UTC),
        due_date=date(2030, 1, 25),
        what="Second",
    )
    headers = csrf_headers(client)
    body = {"due_date": "2030-01-24"} if action == "reschedule" else None
    resp = client.post(f"{URL}/items/{own_item.id}/{action}", json=body, headers=headers)
    assert resp.status_code == 200
    page = resp.json()
    assert page["recipient_name"] == world.person.full_name
    assert page["csrf_token"] == headers["X-CSRF-Token"]
    assert {i["id"] for i in page["items"]} == {str(own_item.id), str(second.id)}
    assert "id" not in page


def test_post_page_shows_the_new_status_and_actions(
    client: TestClient, world: World, own_item
) -> None:
    resp = client.post(f"{URL}/items/{own_item.id}/accept", headers=csrf_headers(client))
    (item,) = resp.json()["items"]
    assert item["status"] == "accepted"
    assert item["allowed_actions"] == ["done", "reschedule", "decline"]


def test_post_page_equals_a_fresh_get(client: TestClient, own_item) -> None:
    resp = client.post(f"{URL}/items/{own_item.id}/accept", headers=csrf_headers(client))
    fresh = client.get(URL).json()
    assert resp.json() == fresh


def test_post_page_has_no_preselected_action(client: TestClient, own_item) -> None:
    resp = client.post(f"{URL}/items/{own_item.id}/accept", headers=csrf_headers(client))
    assert resp.json()["preselected_action"] is None


@pytest.mark.fr("FR-30")
@pytest.mark.fr("FR-16")
def test_item_changed_mail_link_opens_the_page(client: TestClient, own_item) -> None:
    resp = client.get(f"{URL}?item={own_item.id}&n=ITEM_CHANGED")
    assert resp.status_code == 200
    assert resp.json()["items"][0]["id"] == str(own_item.id)


@pytest.mark.fr("FR-16")
def test_item_changed_link_is_recorded_as_link_opened(
    client: TestClient, world: World, own_item
) -> None:
    client.get(f"{URL}?item={own_item.id}&n=ITEM_CHANGED")
    events = (
        world.db.query(AnalyticsEvent)
        .filter(AnalyticsEvent.name == "link_opened", AnalyticsEvent.meeting_id == world.meeting.id)
        .all()
    )
    assert [e.props["notification_type"] for e in events] == ["ITEM_CHANGED"]


@pytest.mark.fr("FR-32")
def test_csrf_cookie_is_secure_outside_dev(
    client: TestClient, own_item, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEV_MODE", "0")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    get_settings.cache_clear()
    try:
        cookie = client.get(URL).headers["set-cookie"].lower()
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert "secure" in cookie


@pytest.mark.fr("FR-32")
def test_csrf_cookie_is_not_secure_in_dev_mode(client: TestClient, own_item) -> None:
    assert get_settings().is_dev
    assert "secure" not in client.get(URL).headers["set-cookie"].lower()
