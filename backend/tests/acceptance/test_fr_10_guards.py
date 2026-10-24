import pytest
from fastapi.testclient import TestClient

from app.modules.series.models import MeetingStatus
from tests.acceptance.helpers_items import (
    API,
    clock,  # noqa: F401
    foreign_host,
    make_ctx,
    problem_code,
)
from tests.conftest import login_as
from tests.factories import World, make_meeting, make_user

pytestmark = [pytest.mark.fr("FR-10"), pytest.mark.usefixtures("clock")]


LINE = "Иван разберётся с выгрузкой к пятнице"


def post_item(client: TestClient, meeting_id, text: str = LINE):
    return client.post(f"{API}/meetings/{meeting_id}/items", json={"raw_text": text})


@pytest.mark.parametrize("text", ["а" * 1001, "", None])
def test_raw_text_empty_or_too_long_is_422(authed: TestClient, world: World, text) -> None:
    make_ctx(world)
    resp = authed.post(f"{API}/meetings/{world.meeting.id}/items", json={"raw_text": text})
    assert resp.status_code == 422
    assert problem_code(resp) == "VALIDATION_ERROR"


def test_unauthenticated_is_401(client: TestClient, world: World) -> None:
    make_ctx(world)
    assert post_item(client, world.meeting.id).status_code == 401


def test_foreign_host_meeting_is_404(client: TestClient, world: World) -> None:
    make_ctx(world)
    login_as(client, make_user(world.db))
    assert post_item(client, world.meeting.id).status_code == 404


def test_foreign_series_meeting_is_404(authed: TestClient, world: World) -> None:
    _, _, _, series = foreign_host(world)
    meeting = make_meeting(world.db, series, status=MeetingStatus.LIVE)
    assert post_item(authed, meeting.id).status_code == 404
