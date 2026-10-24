import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.modules.series.models import AgendaItemStatus, MeetingStatus
from tests.factories import (
    World,
    make_agenda_item,
    make_meeting,
    make_series,
    make_user,
)

pytestmark = pytest.mark.fr("FR-08")


def add(client: TestClient, meeting, title: str = "Тема"):
    return client.post(f"/api/v1/meetings/{meeting.id}/agenda", json={"title": title})


def test_add_item_is_open_with_next_position(authed: TestClient, world: World) -> None:
    first = add(authed, world.meeting)
    second = add(authed, world.meeting)
    assert first.status_code == second.status_code == 201
    assert (first.json()["status"], first.json()["position"]) == ("open", 1)
    assert second.json()["position"] == 2


def test_twenty_first_item_is_rejected(authed: TestClient, world: World) -> None:
    for _ in range(20):
        make_agenda_item(world.db, world.meeting)
    resp = add(authed, world.meeting)
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_twentieth_item_is_accepted(authed: TestClient, world: World) -> None:
    for _ in range(19):
        make_agenda_item(world.db, world.meeting)
    assert add(authed, world.meeting).status_code == 201


@pytest.mark.parametrize("title", ["", "x" * 301])
def test_bad_title_rejected(authed: TestClient, world: World, title: str) -> None:
    resp = add(authed, world.meeting, title)
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_position_is_unique_within_meeting(world: World) -> None:
    make_agenda_item(world.db, world.meeting, position=1)
    with pytest.raises(IntegrityError), world.db.begin_nested():
        make_agenda_item(world.db, world.meeting, position=1)


def positions(client: TestClient, meeting) -> list[tuple[str, int]]:
    agenda = client.get(f"/api/v1/meetings/{meeting.id}").json()["agenda"]
    return sorted((a["title"], a["position"]) for a in agenda)


def test_patch_changes_title_position_status(authed: TestClient, world: World) -> None:
    agenda = make_agenda_item(world.db, world.meeting, title="A")
    make_agenda_item(world.db, world.meeting, title="B")
    make_agenda_item(world.db, world.meeting, title="C")
    resp = authed.patch(
        f"/api/v1/agenda/{agenda.id}",
        json={"title": "Новая", "position": 3, "status": "covered"},
    )
    assert resp.status_code == 200
    assert (resp.json()["title"], resp.json()["position"], resp.json()["status"]) == (
        "Новая",
        3,
        "covered",
    )


@pytest.mark.parametrize(
    ("moved", "target", "expected"),
    [
        ("A", 3, ["B", "C", "A"]),
        ("C", 1, ["C", "A", "B"]),
        ("B", 1, ["B", "A", "C"]),
        ("B", 3, ["A", "C", "B"]),
        ("B", 2, ["A", "B", "C"]),
    ],
)
def test_reorder_renumbers_without_gaps(
    authed: TestClient, world: World, moved: str, target: int, expected: list[str]
) -> None:
    rows = {t: make_agenda_item(world.db, world.meeting, title=t) for t in "ABC"}
    resp = authed.patch(f"/api/v1/agenda/{rows[moved].id}", json={"position": target})
    assert resp.status_code == 200
    by_position = sorted(positions(authed, world.meeting), key=lambda p: p[1])
    assert by_position == [(t, i) for i, t in enumerate(expected, 1)]


@pytest.mark.parametrize("target", [0, -1, 4, 5])
def test_position_is_bounded_to_1_n_and_stays_contiguous(
    authed: TestClient, world: World, target: int
) -> None:
    rows = [make_agenda_item(world.db, world.meeting, title=t) for t in "ABC"]
    resp = authed.patch(f"/api/v1/agenda/{rows[0].id}", json={"position": target})
    assert resp.status_code in (200, 422)
    if resp.status_code == 422:
        assert resp.json()["code"] == "VALIDATION_ERROR"
    after = authed.get(f"/api/v1/meetings/{world.meeting.id}").json()["agenda"]
    assert sorted(a["position"] for a in after) == [1, 2, 3]


def test_moved_items_are_copied_into_next_meeting(authed: TestClient, world: World) -> None:
    previous = world.meeting
    previous.status = MeetingStatus.SENT
    make_agenda_item(world.db, previous, title="Перенесённая", status=AgendaItemStatus.MOVED)
    make_agenda_item(world.db, previous, title="Обсуждённая", status=AgendaItemStatus.COVERED)
    make_agenda_item(world.db, previous, title="Открытая", status=AgendaItemStatus.OPEN)
    world.db.flush()
    started = authed.post(f"/api/v1/series/{world.series.id}/meetings", json={})
    assert started.status_code == 201
    agenda = started.json()["meeting"]["agenda"]
    assert [(a["title"], a["status"]) for a in agenda] == [("Перенесённая", "open")]


def test_copy_does_not_reuse_source_row(authed: TestClient, world: World) -> None:
    world.meeting.status = MeetingStatus.SENT
    source = make_agenda_item(world.db, world.meeting, status=AgendaItemStatus.MOVED)
    started = authed.post(f"/api/v1/series/{world.series.id}/meetings", json={})
    assert started.status_code == 201
    copied = started.json()["meeting"]["agenda"][0]
    assert copied["id"] != str(source.id)


def test_foreign_meeting_is_404(authed: TestClient, world: World) -> None:
    foreign = make_meeting(world.db, make_series(world.db, make_user(world.db)))
    resp = add(authed, foreign)
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_foreign_agenda_item_patch_is_404(authed: TestClient, world: World) -> None:
    foreign = make_meeting(world.db, make_series(world.db, make_user(world.db)))
    agenda = make_agenda_item(world.db, foreign)
    resp = authed.patch(f"/api/v1/agenda/{agenda.id}", json={"title": "x"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_add_takes_row_lock_on_meeting(authed: TestClient, world: World) -> None:
    from sqlalchemy import event

    statements: list[str] = []

    def spy(conn, cursor, statement, *args):
        statements.append(statement)

    engine = world.db.get_bind().engine
    event.listen(engine, "before_cursor_execute", spy)
    try:
        assert add(authed, world.meeting).status_code == 201
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert any("FROM meetings" in s and "FOR UPDATE" in s for s in statements)
