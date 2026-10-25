from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time

from app.models import SeriesMember
from app.modules.items.models import ActorType, CommentKind, ItemAction, ItemComment, ItemStatus
from tests.conftest import login_as
from tests.factories import World, make_event

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 20)
SENT = NOW - timedelta(days=60)


@pytest.fixture
def w(world: World, client: TestClient):
    world.db.add(SeriesMember(series_id=world.series.id, person_id=world.person.id))
    world.db.flush()
    with freeze_time(NOW):
        login_as(client, world.user)
        yield world


def share(client: TestClient, w: World, **params) -> float | None:
    resp = client.get(f"/api/v1/series/{w.series.id}/items", params=params)
    assert resp.status_code == 200
    return resp.json()["on_time_share_30d"]


def done_item(w: World, due: date, done_at: datetime):
    return w.item(status=ItemStatus.DONE, sent_at=SENT, due_date=due, done_at=done_at)


@pytest.mark.fr("FR-44")
def test_share_counts_item_due_today_done_today(client: TestClient, w: World) -> None:
    done_item(w, TODAY, NOW)
    assert share(client, w) == 1.0


@pytest.mark.fr("FR-44")
def test_share_window_includes_day_29_ago_and_excludes_day_30_ago(
    client: TestClient, w: World
) -> None:
    done_item(w, TODAY - timedelta(days=29), NOW - timedelta(days=29))
    done_item(w, TODAY - timedelta(days=30), NOW - timedelta(days=29))
    assert share(client, w) == 1.0


@pytest.mark.fr("FR-44")
def test_share_compares_done_date_in_series_timezone(client: TestClient, w: World) -> None:
    done_item(w, TODAY - timedelta(days=1), datetime(2026, 10, 19, 22, 30, tzinfo=UTC))
    assert share(client, w) == 0.0


@pytest.mark.fr("FR-44")
def test_share_done_late_in_utc_but_on_time_in_series_timezone(
    client: TestClient, w: World
) -> None:
    w.series.timezone = "Pacific/Auckland"
    w.db.flush()
    done_item(w, TODAY - timedelta(days=2), datetime(2026, 10, 18, 20, 0, tzinfo=UTC))
    assert share(client, w) == 0.0


@pytest.mark.fr("FR-44")
def test_share_excludes_cancelled_and_draft_from_denominator(client: TestClient, w: World) -> None:
    done_item(w, TODAY - timedelta(days=3), NOW - timedelta(days=4))
    w.item(
        status=ItemStatus.CANCELLED,
        sent_at=SENT,
        due_date=TODAY - timedelta(days=3),
        cancelled_at=NOW - timedelta(days=2),
    )
    w.item(due_date=TODAY - timedelta(days=3))
    assert share(client, w) == 1.0


@pytest.mark.fr("FR-44")
def test_share_counts_open_overdue_item_as_not_on_time(client: TestClient, w: World) -> None:
    done_item(w, TODAY - timedelta(days=3), NOW - timedelta(days=4))
    w.item(status=ItemStatus.PENDING, sent_at=SENT, due_date=TODAY - timedelta(days=2))
    assert share(client, w) == 0.5


@pytest.mark.fr("FR-44")
def test_share_ignores_future_due_items(client: TestClient, w: World) -> None:
    w.item(status=ItemStatus.PENDING, sent_at=SENT, due_date=TODAY + timedelta(days=2))
    assert share(client, w) is None


@pytest.mark.fr("FR-44")
def test_share_not_affected_by_status_filter_hidden_rows_in_page(
    client: TestClient, w: World
) -> None:
    done_item(w, TODAY - timedelta(days=3), NOW - timedelta(days=4))
    assert share(client, w, assignee_person_id=str(w.person.id)) is None


@pytest.mark.fr("FR-44")
def test_foreign_series_items_is_404_not_found(client: TestClient, w: World) -> None:
    from tests.factories import make_series, make_user

    other = make_series(w.db, make_user(w.db))
    resp = client.get(f"/api/v1/series/{other.id}/items")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


@pytest.mark.fr("FR-44")
def test_series_items_without_session_is_401(client: TestClient, w: World) -> None:
    client.cookies.clear()
    assert client.get(f"/api/v1/series/{w.series.id}/items").status_code == 401


def comment(w: World, item, kind: CommentKind, text: str, at: datetime) -> None:
    w.db.add(
        ItemComment(
            item_id=item.id, author_person_id=w.person.id, kind=kind, text=text, created_at=at
        )
    )
    w.db.flush()


@pytest.mark.fr("FR-45")
def test_card_last_response_is_not_mine_with_comment(client: TestClient, w: World) -> None:
    item = w.item(assignee_person_id=w.person.id, status=ItemStatus.PENDING, sent_at=SENT)
    comment(w, item, CommentKind.NOT_MINE, "Это к Анне", NOW - timedelta(hours=1))
    last = client.get(f"/api/v1/items/{item.id}").json()["last_response"]
    assert last["kind"] == "not_mine"
    assert last["text"] == "Это к Анне"


@pytest.mark.fr("FR-45")
def test_card_last_response_is_the_latest_comment(client: TestClient, w: World) -> None:
    item = w.item(assignee_person_id=w.person.id, status=ItemStatus.PENDING, sent_at=SENT)
    comment(w, item, CommentKind.NOT_MINE, "Старый", NOW - timedelta(days=2))
    comment(w, item, CommentKind.RESCHEDULE, "Новый", NOW - timedelta(hours=1))
    last = client.get(f"/api/v1/items/{item.id}").json()["last_response"]
    assert last["kind"] == "reschedule"
    assert last["text"] == "Новый"


@pytest.mark.fr("FR-45")
def test_card_last_response_is_null_without_comments(client: TestClient, w: World) -> None:
    item = w.item()
    assert client.get(f"/api/v1/items/{item.id}").json()["last_response"] is None


@pytest.mark.fr("FR-45")
def test_card_is_overdue_flag(client: TestClient, w: World) -> None:
    item = w.item(status=ItemStatus.PENDING, sent_at=SENT, due_date=TODAY - timedelta(days=1))
    assert client.get(f"/api/v1/items/{item.id}").json()["is_overdue"] is True


@pytest.mark.fr("FR-45")
def test_history_is_ordered_by_time(client: TestClient, w: World) -> None:
    item = w.item()
    make_event(w.db, item, action=ItemAction.UPDATED, created_at=NOW - timedelta(hours=1))
    make_event(w.db, item, action=ItemAction.CREATED, created_at=NOW - timedelta(hours=3))
    make_event(w.db, item, action=ItemAction.SENT, created_at=NOW - timedelta(hours=2))
    actions = [e["action"] for e in client.get(f"/api/v1/items/{item.id}/history").json()]
    assert actions == ["created", "sent", "updated"]


@pytest.mark.fr("FR-45")
def test_history_shows_actor_name_data_and_time(client: TestClient, w: World) -> None:
    item = w.item()
    make_event(
        w.db,
        item,
        action=ItemAction.DONE,
        actor_type=ActorType.ASSIGNEE,
        actor_person_id=w.person.id,
        data={"status_from": "pending", "status_to": "done"},
    )
    event = client.get(f"/api/v1/items/{item.id}/history").json()[0]
    assert event["actor_type"] == "assignee"
    assert event["actor_name"] == w.person.full_name
    assert event["data"] == {"status_from": "pending", "status_to": "done"}
    assert event["created_at"]


@pytest.mark.fr("FR-45")
def test_history_of_host_actor_has_host_name(client: TestClient, w: World) -> None:
    item = w.item()
    make_event(w.db, item, actor_type=ActorType.HOST, actor_user_id=w.user.id)
    event = client.get(f"/api/v1/items/{item.id}/history").json()[0]
    assert event["actor_name"] == w.user.full_name


@pytest.mark.fr("FR-45")
@pytest.mark.parametrize("suffix", ["", "/history"])
def test_card_and_history_without_session_are_401(
    client: TestClient, w: World, suffix: str
) -> None:
    item = w.item()
    client.cookies.clear()
    assert client.get(f"/api/v1/items/{item.id}{suffix}").status_code == 401


@pytest.mark.fr("FR-45")
def test_history_is_not_available_by_assignee_link(client: TestClient, w: World) -> None:
    import hashlib

    from tests.factories import make_access_link

    token = "tok-" + "h" * 28
    make_access_link(w.db, w.person, w.meeting, token_hash=hashlib.sha256(token.encode()).digest())
    item = w.item(assignee_person_id=w.person.id, status=ItemStatus.PENDING, sent_at=SENT)
    client.cookies.clear()
    resp = client.get(f"/api/v1/p/{token}/items/{item.id}/history")
    assert resp.status_code in (404, 405)
    assert "history" not in client.get(f"/api/v1/p/{token}").text
