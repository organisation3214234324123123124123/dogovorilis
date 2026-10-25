import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import Item
from app.modules.series.models import MeetingStatus
from tests.acceptance.helpers_items import (
    API,
    clock,  # noqa: F401
    events,
    make_ctx,
    problem_code,
    ws_events,
)
from tests.factories import World

pytestmark = [
    pytest.mark.fr("FR-10"),
    pytest.mark.usefixtures("clock"),
]

LINE = "Иван разберётся с выгрузкой к пятнице"


def post_item(client: TestClient, meeting_id, text: str = LINE):
    return client.post(f"{API}/meetings/{meeting_id}/items", json={"raw_text": text})


def test_create_returns_draft_manual_version_1(authed: TestClient, world: World) -> None:
    make_ctx(world)
    resp = post_item(authed, world.meeting.id)
    assert resp.status_code == 201
    body = resp.json()
    assert (body["status"], body["source"], body["version"]) == ("draft", "manual", 1)
    assert body["raw_text"] == LINE


def test_created_item_is_stored_in_meeting(authed: TestClient, world: World) -> None:
    make_ctx(world)
    item_id = post_item(authed, world.meeting.id).json()["id"]
    world.db.expire_all()
    stored = world.db.get(Item, item_id)
    assert stored.meeting_id == world.meeting.id
    assert stored.series_id == world.series.id


@pytest.mark.parametrize("length", [1, 999, 1000])
def test_raw_text_up_to_1000_chars_is_accepted(
    authed: TestClient, world: World, length: int
) -> None:
    make_ctx(world)
    assert post_item(authed, world.meeting.id, "а" * length).status_code == 201


def test_item_created_event_goes_to_panel(authed: TestClient, world: World) -> None:
    make_ctx(world)
    frames = ws_events(
        authed, world.meeting.id, lambda: post_item(authed, world.meeting.id), ("item.created",)
    )
    assert "item.created" in {f.get("type") for f in frames}


def test_item_updated_event_follows_parsing(authed: TestClient, world: World) -> None:
    make_ctx(world)
    frames = ws_events(
        authed,
        world.meeting.id,
        lambda: post_item(authed, world.meeting.id),
        ("item.created", "item.updated"),
    )
    assert "item.updated" in {f.get("type") for f in frames}


def test_parsed_fields_are_filled_after_creation(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item_id = post_item(authed, world.meeting.id).json()["id"]
    card = authed.get(f"{API}/items/{item_id}").json()
    assert card["assignee"]["id"] == str(ctx.ivan.id)
    assert card["due_date"] == "2026-10-16"


def test_rule_gaps_are_present_right_after_input(authed: TestClient, world: World) -> None:
    make_ctx(world)
    body = post_item(authed, world.meeting.id, "Подумать над выгрузкой").json()
    assert {g["type"] for g in body["gaps"]} >= {"no_assignee", "no_due"}
    assert {g["source"] for g in body["gaps"]} == {"rule"}


def test_61st_decision_within_a_minute_is_rate_limited(authed: TestClient, world: World) -> None:
    make_ctx(world)
    codes = [post_item(authed, world.meeting.id).status_code for _ in range(60)]
    assert set(codes) == {201}
    resp = post_item(authed, world.meeting.id)
    assert resp.status_code == 429
    assert problem_code(resp) == "RATE_LIMITED"
    assert int(resp.headers["Retry-After"]) > 0


def test_decision_captured_event_is_written(authed: TestClient, world: World) -> None:
    make_ctx(world)
    item_id = post_item(authed, world.meeting.id).json()["id"]
    [event] = events(world.db, "decision_captured", item_id=item_id)
    assert event.props["source"] == "manual"
    assert "fields_filled" in event.props


@pytest.mark.parametrize("status", [MeetingStatus.LIVE, MeetingStatus.CLOSING, MeetingStatus.SENT])
def test_items_can_be_added_in_live_closing_sent(
    authed: TestClient, world: World, status: MeetingStatus
) -> None:
    make_ctx(world, status)
    resp = post_item(authed, world.meeting.id)
    assert resp.status_code == 201
    assert resp.json()["status"] == "draft"


def test_planned_meeting_is_409_meeting_not_live(authed: TestClient, world: World) -> None:
    make_ctx(world, MeetingStatus.PLANNED)
    resp = post_item(authed, world.meeting.id)
    assert resp.status_code == 409
    assert problem_code(resp) == "MEETING_NOT_LIVE"


def test_decision_text_is_not_logged(
    authed: TestClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    make_ctx(world)
    secret = "Секретная выгрузка клиентов Альфа-Банка"
    with caplog.at_level(logging.DEBUG):
        assert post_item(authed, world.meeting.id, secret).status_code == 201
    assert secret not in caplog.text
    assert "выгрузка клиентов" not in caplog.text


def test_new_item_in_sent_meeting_is_draft_for_resend(authed: TestClient, world: World) -> None:
    make_ctx(world, MeetingStatus.SENT)
    world.meeting.summary_sent_at = world.meeting.planned_start + timedelta(hours=1)
    item_id = post_item(authed, world.meeting.id).json()["id"]
    world.db.expire_all()
    assert world.db.get(Item, item_id).sent_at is None
