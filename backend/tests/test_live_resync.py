import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.series.models import MeetingStatus
from tests.factories import World, make_meeting, make_series

pytestmark = pytest.mark.fr("FR-14")

API = "/api/v1"


@pytest.fixture(autouse=True)
def live_meeting(world: World) -> None:
    world.meeting.status = MeetingStatus.LIVE
    world.db.flush()


def ws_url(meeting_id) -> str:
    return f"/api/v1/ws/meetings/{meeting_id}"


def add_item(client: TestClient, meeting_id, raw: str = "Иван подготовит отчёт к пятнице") -> dict:
    resp = client.post(f"{API}/meetings/{meeting_id}/items", json={"raw_text": raw})
    assert resp.status_code == 201
    return resp.json()


def events_version(client: TestClient, meeting_id) -> int:
    resp = client.get(f"{API}/meetings/{meeting_id}")
    assert resp.status_code == 200
    return resp.json()["events_version"]


def collect_until(ws, target: int, limit: int = 200) -> list[dict]:
    frames: list[dict] = []
    while len(frames) < limit:
        frame = ws.receive_json()
        frames.append(frame)
        version = frame.get("version")
        if not isinstance(version, int) or version >= target:
            break
    return frames


def versions(frames: list[dict]) -> list[int]:
    return [f["version"] for f in frames]


def test_missed_events_arrive_in_order_and_complete(authed: TestClient, world: World) -> None:
    add_item(authed, world.meeting.id)
    seen = events_version(authed, world.meeting.id)
    add_item(authed, world.meeting.id, "Анна согласует бюджет до среды")
    add_item(authed, world.meeting.id, "Пётр обновит план до пятницы")
    target = events_version(authed, world.meeting.id)
    assert target > seen + 1
    with authed.websocket_connect(ws_url(world.meeting.id)) as ws:
        ws.send_json({"type": "resync", "since_version": seen})
        frames = collect_until(ws, target)
    assert versions(frames) == list(range(seen + 1, target + 1))
    assert [f["event"] for f in frames].count("item.created") == 2


def test_resync_from_zero_replays_whole_journal(authed: TestClient, world: World) -> None:
    add_item(authed, world.meeting.id)
    target = events_version(authed, world.meeting.id)
    with authed.websocket_connect(ws_url(world.meeting.id)) as ws:
        ws.send_json({"type": "resync", "since_version": 0})
        frames = collect_until(ws, target)
    assert versions(frames) == list(range(1, target + 1))


def test_no_duplicates_when_live_events_follow_replay(authed: TestClient, world: World) -> None:
    add_item(authed, world.meeting.id)
    seen = events_version(authed, world.meeting.id)
    add_item(authed, world.meeting.id, "Анна согласует бюджет до среды")
    with authed.websocket_connect(ws_url(world.meeting.id)) as ws:
        ws.send_json({"type": "resync", "since_version": seen})
        add_item(authed, world.meeting.id, "Пётр обновит план до пятницы")
        target = events_version(authed, world.meeting.id)
        frames = collect_until(ws, target)
    assert versions(frames) == list(range(seen + 1, target + 1))


def test_other_meeting_events_never_arrive(authed: TestClient, world: World) -> None:
    other_series = make_series(world.db, world.user)
    other = make_meeting(world.db, other_series, status=MeetingStatus.LIVE)
    add_item(authed, world.meeting.id)
    with authed.websocket_connect(ws_url(world.meeting.id)) as ws:
        ws.send_json({"type": "resync", "since_version": 0})
        add_item(authed, other.id, "Анна согласует бюджет до среды")
        add_item(authed, world.meeting.id, "Пётр обновит план до пятницы")
        target = events_version(authed, world.meeting.id)
        frames = collect_until(ws, target)
    assert {f["meeting_id"] for f in frames} == {str(world.meeting.id)}
    assert versions(frames) == list(range(1, target + 1))


def test_detail_events_version_equals_last_frame_version(authed: TestClient, world: World) -> None:
    add_item(authed, world.meeting.id)
    add_item(authed, world.meeting.id, "Анна согласует бюджет до среды")
    target = events_version(authed, world.meeting.id)
    with authed.websocket_connect(ws_url(world.meeting.id)) as ws:
        ws.send_json({"type": "resync", "since_version": 0})
        frames = collect_until(ws, target)
    assert frames[-1]["version"] == target


def test_fresh_meeting_has_zero_events_version(authed: TestClient, world: World) -> None:
    assert events_version(authed, world.meeting.id) == 0


def test_frame_carries_type_and_event_and_item_version(authed: TestClient, world: World) -> None:
    add_item(authed, world.meeting.id)
    second = add_item(authed, world.meeting.id, "Анна согласует бюджет до среды")
    target = events_version(authed, world.meeting.id)
    with authed.websocket_connect(ws_url(world.meeting.id)) as ws:
        ws.send_json({"type": "resync", "since_version": 0})
        frames = collect_until(ws, target)
    created = [f for f in frames if f["event"] == "item.created" and f["item_id"] == second["id"]]
    assert len(created) == 1
    frame = created[0]
    assert frame["type"] == frame["event"] == "item.created"
    assert frame["data"]["version"] == second["version"]
    assert frame["version"] != second["version"]


def test_live_frame_after_resync_has_event_number(authed: TestClient, world: World) -> None:
    with authed.websocket_connect(ws_url(world.meeting.id)) as ws:
        ws.send_json({"type": "resync", "since_version": 0})
        item = add_item(authed, world.meeting.id)
        target = events_version(authed, world.meeting.id)
        frames = collect_until(ws, target)
    assert versions(frames) == list(range(1, target + 1))
    created = next(f for f in frames if f["event"] == "item.created")
    assert created["data"]["version"] == item["version"]


def test_resync_at_current_version_replays_nothing(authed: TestClient, world: World) -> None:
    add_item(authed, world.meeting.id)
    current = events_version(authed, world.meeting.id)
    with authed.websocket_connect(ws_url(world.meeting.id)) as ws:
        ws.send_json({"type": "resync", "since_version": current})
        add_item(authed, world.meeting.id, "Анна согласует бюджет до среды")
        target = events_version(authed, world.meeting.id)
        frames = collect_until(ws, target)
    assert versions(frames) == list(range(current + 1, target + 1))


def assert_single_reset(authed: TestClient, world: World, since: object) -> None:
    add_item(authed, world.meeting.id)
    add_item(authed, world.meeting.id, "Анна согласует бюджет до среды")
    current = events_version(authed, world.meeting.id)
    with authed.websocket_connect(ws_url(world.meeting.id)) as ws:
        ws.send_json({"type": "resync", "since_version": since})
        first = ws.receive_json()
        add_item(authed, world.meeting.id, "Пётр обновит план до пятницы")
        target = events_version(authed, world.meeting.id)
        rest = collect_until(ws, target)
    assert first["event"] == first["type"] == "resync.reset"
    assert first["version"] == current
    assert all(f["event"] != "resync.reset" for f in rest)
    assert versions(rest) == list(range(current + 1, target + 1))


def test_pruned_since_version_gives_single_reset(authed: TestClient, world: World) -> None:
    add_item(authed, world.meeting.id)
    add_item(authed, world.meeting.id, "Анна согласует бюджет до среды")
    world.db.execute(
        text("DELETE FROM meeting_events WHERE meeting_id = :m AND version <= 3"),
        {"m": world.meeting.id},
    )
    world.db.commit()
    assert_single_reset(authed, world, 0)


def test_too_large_since_version_gives_single_reset(authed: TestClient, world: World) -> None:
    assert_single_reset(authed, world, 10_000)


@pytest.mark.parametrize("since", ["abc", 1.5, None, True, -1])
def test_non_integer_since_version_gives_single_reset(
    authed: TestClient, world: World, since: object
) -> None:
    assert_single_reset(authed, world, since)


def test_concurrent_changes_get_unique_gapless_numbers(db: Session) -> None:
    from app.core.db import SessionLocal, get_engine
    from app.modules.live.service import publish_after_commit
    from tests.factories import make_user

    with SessionLocal(bind=get_engine()) as setup:
        user = make_user(setup)
        meeting_id = make_meeting(setup, make_series(setup, user), status=MeetingStatus.LIVE).id
        user_id = user.id
        setup.commit()
    workers, per_worker = 6, 5
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(per_worker):
                with SessionLocal(bind=get_engine()) as session:
                    publish_after_commit(
                        session,
                        meeting_id,
                        {"type": "gaps.updated", "meeting_id": str(meeting_id), "data": {}},
                    )
                    session.commit()
        except BaseException as exc:
            errors.append(exc)

    try:
        threads = [threading.Thread(target=work) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errors
        with SessionLocal(bind=get_engine()) as check:
            rows = (
                check.execute(
                    text("SELECT version FROM meeting_events WHERE meeting_id = :m ORDER BY 1"),
                    {"m": meeting_id},
                )
                .scalars()
                .all()
            )
            counter = check.execute(
                text("SELECT events_version FROM meetings WHERE id = :m"), {"m": meeting_id}
            ).scalar_one()
        assert list(rows) == list(range(1, workers * per_worker + 1))
        assert counter == workers * per_worker
    finally:
        with SessionLocal(bind=get_engine()) as cleanup:
            cleanup.execute(text("DELETE FROM series WHERE owner_user_id = :u"), {"u": user_id})
            cleanup.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
            cleanup.commit()
