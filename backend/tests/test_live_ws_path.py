import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.modules.series.models import MeetingStatus
from tests.factories import World

pytestmark = pytest.mark.fr("FR-14")

API = "/api/v1"
NEW_PATH = f"{API}/ws/meetings/{{}}"
OLD_PATH = "/ws/meetings/{}"

GAP_KEYS = {"id", "type", "source", "question", "detected_at"}


@pytest.fixture(autouse=True)
def live_meeting(world: World) -> None:
    world.meeting.status = MeetingStatus.LIVE
    world.db.flush()


def collect_gaps_frames(client: TestClient, meeting_id) -> list[dict]:
    created = client.post(
        f"{API}/meetings/{meeting_id}/items", json={"raw_text": "Иван подготовит отчёт"}
    )
    assert created.status_code == 201
    item = created.json()
    patched = client.patch(
        f"{API}/items/{item['id']}",
        json={"what": "Подготовить отчёт"},
        headers={"If-Match": str(item["version"])},
    )
    assert patched.status_code == 200
    version = client.get(f"{API}/meetings/{meeting_id}").json()["events_version"]
    frames: list[dict] = []
    with client.websocket_connect(f"{API}/ws/meetings/{meeting_id}") as ws:
        ws.send_json({"type": "resync", "since_version": 0})
        while len(frames) < version:
            frames.append(ws.receive_json())
    return [f for f in frames if f["event"] == "gaps.updated"]


def test_ws_served_under_api_v1_prefix(authed: TestClient, world: World) -> None:
    with authed.websocket_connect(NEW_PATH.format(world.meeting.id)) as ws:
        ws.send_json({"type": "resync", "since_version": 0})


def test_ws_under_api_v1_prefix_checks_auth(client: TestClient, world: World) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(NEW_PATH.format(world.meeting.id)) as ws,
    ):
        ws.receive_text()
    assert exc.value.code == 4401


def test_ws_old_path_is_not_served(authed: TestClient, world: World) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        authed.websocket_connect(OLD_PATH.format(world.meeting.id)),
    ):
        pass


def test_gaps_updated_data_is_object_with_item_id_and_gaps(
    authed: TestClient, world: World
) -> None:
    frames = collect_gaps_frames(authed, world.meeting.id)
    assert frames
    for frame in frames:
        assert isinstance(frame["data"], dict)
        assert frame["data"]["item_id"] == frame["item_id"]
        assert isinstance(frame["data"]["gaps"], list)


def test_gaps_updated_gap_has_contract_fields(authed: TestClient, world: World) -> None:
    frames = collect_gaps_frames(authed, world.meeting.id)
    gaps = [g for f in frames for g in f["data"]["gaps"]]
    assert gaps
    for gap in gaps:
        assert set(gap) >= GAP_KEYS
