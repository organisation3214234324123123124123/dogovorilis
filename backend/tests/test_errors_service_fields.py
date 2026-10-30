"""Контракт ошибок (NFR-13): 422 в OpenAPI - Problem, нет ручек с 501, errors[] у ошибок."""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.acceptance.helpers_items import API, clock, if_match, make_ctx, outsider  # noqa: F401
from tests.acceptance.test_fr_16_edit_after_send import sent_item
from tests.factories import World, make_agenda_item, make_meeting

pytestmark = [
    pytest.mark.fr("NFR-13"),
    pytest.mark.usefixtures("clock"),
]


def _operations() -> list[tuple[str, dict]]:
    schema = create_app().openapi()
    return [
        (f"{method.upper()} {path}", op)
        for path, item in schema["paths"].items()
        for method, op in item.items()
        if isinstance(op, dict) and "responses" in op
    ]


def _json_schema_ref(response: dict) -> str | None:
    for media in response.get("content", {}).values():
        return media.get("schema", {}).get("$ref")
    return None


def test_every_422_in_openapi_is_problem_not_http_validation_error() -> None:
    bad = {}
    seen = 0
    for name, op in _operations():
        response = op["responses"].get("422")
        if response is None:
            continue
        seen += 1
        ref = _json_schema_ref(response)
        if ref != "#/components/schemas/Problem":
            bad[name] = ref
    assert seen > 0
    assert not bad, bad


def test_http_validation_error_schema_is_not_published() -> None:
    schemas = create_app().openapi()["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas


def test_openapi_has_no_501_responses() -> None:
    offenders = [name for name, op in _operations() if "501" in op["responses"]]
    assert offenders == []


def test_app_has_no_not_implemented_error() -> None:
    from app.core import errors

    assert not hasattr(errors, "NotImplementedYet")
    assert 501 not in errors._ERROR_TEXT


def _fields(resp) -> list[str]:
    assert resp.status_code == 422
    return [e["field"] for e in resp.json().get("errors", [])]


def test_register_without_consent_points_at_pd_consent(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "new@example.com",
            "password": "secret123",
            "full_name": "Новый Ведущий",
            "pd_consent": False,
        },
    )
    assert resp.json()["code"] == "VALIDATION_ERROR"
    assert _fields(resp) == ["pd_consent"]


def test_audio_capture_mode_points_at_capture_mode(authed: TestClient) -> None:
    resp = authed.post(
        "/api/v1/series",
        json={
            "title": "Планёрка",
            "timezone": "Europe/Moscow",
            "planned_duration_min": 30,
            "capture_mode": "audio",
        },
    )
    assert _fields(resp) == ["capture_mode"]


def test_start_with_end_before_now_points_at_planned_end(authed: TestClient, world: World) -> None:
    resp = authed.post(
        f"{API}/series/{world.series.id}/meetings", json={"planned_end": "2000-01-01T00:00:00Z"}
    )
    assert _fields(resp) == ["planned_end"]


def test_patch_meeting_end_before_start_points_at_planned_end(
    authed: TestClient, world: World
) -> None:
    end = (world.meeting.planned_start - timedelta(hours=1)).isoformat()
    resp = authed.patch(f"{API}/meetings/{world.meeting.id}", json={"planned_end": end})
    assert _fields(resp) == ["planned_end"]


def test_patch_meeting_stranger_participant_points_at_participants(
    authed: TestClient, world: World
) -> None:
    stranger = outsider(world)
    resp = authed.patch(
        f"{API}/meetings/{world.meeting.id}",
        json={"participants": [{"person_id": str(stranger.id), "present": True}]},
    )
    assert resp.json()["code"] == "NOT_A_PARTICIPANT"
    assert _fields(resp) == ["participants.0.person_id"]


def test_foreign_agenda_item_points_at_agenda_item_id(authed: TestClient, world: World) -> None:
    make_ctx(world)
    foreign = make_agenda_item(world.db, make_meeting(world.db, world.series))
    resp = authed.post(
        f"{API}/meetings/{world.meeting.id}/items",
        json={"raw_text": "Иван сделает отчёт", "agenda_item_id": str(foreign.id)},
    )
    assert _fields(resp) == ["agenda_item_id"]


def test_unknown_agenda_item_points_at_agenda_item_id(authed: TestClient, world: World) -> None:
    make_ctx(world)
    resp = authed.post(
        f"{API}/meetings/{world.meeting.id}/items",
        json={"raw_text": "Иван сделает отчёт", "agenda_item_id": str(uuid.uuid4())},
    )
    assert _fields(resp) == ["agenda_item_id"]


def test_use_reassign_points_at_assignee_person_id(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = sent_item(world, ctx)
    resp = authed.patch(
        f"{API}/items/{item.id}",
        json={"assignee_person_id": str(ctx.anna.id)},
        headers=if_match(item),
    )
    assert resp.json()["code"] == "USE_REASSIGN"
    assert _fields(resp) == ["assignee_person_id"]
