# ruff: noqa: F811
"""Режим закрытия, автопереход по таймеру и повестка.

Проверяются ``app.modules.series.tasks.open_due_closures(db, now_utc)``,
``app.modules.series.service.uncovered_agenda(db, meeting_id)``,
``app.modules.series.service.apply_agenda_matches(db, meeting_id, pairs)``.
"""

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import AgendaItem
from app.modules.series.models import AgendaItemStatus, ClosureTrigger, MeetingStatus
from tests.acceptance.helpers_mailing import API, TUESDAY, events, frozen, host, make_closing  # noqa: F401
from tests.factories import World, make_gap

END = datetime(2030, 1, 15, 14, 0, tzinfo=UTC)  # позже planned_start (11:30)


def go_live(world: World, start: datetime = TUESDAY) -> None:
    world.meeting.status = MeetingStatus.LIVE
    world.meeting.started_at = start - timedelta(minutes=30)
    world.meeting.planned_start = start - timedelta(minutes=30)
    world.meeting.planned_end = start + timedelta(minutes=30)
    world.db.flush()


def set_end(world: World, end: datetime) -> None:
    world.meeting.planned_end = end
    world.db.flush()


def open_closure(client: TestClient, world: World):
    return client.post(f"{API}/meetings/{world.meeting.id}/closure")


@pytest.mark.fr("FR-22")
def test_closure_moves_live_meeting_to_closing_by_button(host, world: World) -> None:
    go_live(world)
    resp = open_closure(host, world)
    assert resp.status_code == 200
    world.db.refresh(world.meeting)
    assert world.meeting.status == MeetingStatus.CLOSING
    assert world.meeting.closure_opened_at is not None
    assert world.meeting.closure_trigger == ClosureTrigger.BUTTON
    assert resp.json()["status"] == "closing"


@pytest.mark.fr("FR-22")
def test_closure_writes_closure_opened_event(host, world: World) -> None:
    go_live(world)
    open_closure(host, world)
    (event,) = events(world.db, "closure_opened", meeting_id=world.meeting.id)
    assert event.props["trigger"] == "button"
    assert event.props["minutes_to_planned_end"] == 30


@pytest.mark.fr("FR-22")
def test_closure_response_counts_open_gaps(host, world: World) -> None:
    go_live(world)
    make_gap(world.db, world.item(what="Без срока"))
    make_gap(world.db, world.item(what="Без исполнителя"))
    assert open_closure(host, world).json()["open_gaps_count"] == 2


@pytest.mark.fr("FR-22")
def test_closure_of_planned_meeting_is_409_meeting_not_live(host, world: World) -> None:
    world.meeting.status = MeetingStatus.PLANNED
    world.db.flush()
    resp = open_closure(host, world)
    assert resp.status_code == 409
    assert resp.json()["code"] == "MEETING_NOT_LIVE"


@pytest.mark.fr("FR-23")
def test_timer_opens_closure_five_minutes_before_planned_end(world: World, frozen) -> None:
    from app.modules.series.tasks import open_due_closures

    go_live(world)
    set_end(world, END)
    assert open_due_closures(world.db, END - timedelta(minutes=5)) == 1
    world.db.refresh(world.meeting)
    assert world.meeting.status == MeetingStatus.CLOSING
    assert world.meeting.closure_trigger == ClosureTrigger.TIMER


@pytest.mark.fr("FR-23")
def test_timer_does_nothing_before_the_five_minute_mark(world: World, frozen) -> None:
    from app.modules.series.tasks import open_due_closures

    go_live(world)
    set_end(world, END)
    assert open_due_closures(world.db, END - timedelta(minutes=5, seconds=1)) == 0
    world.db.refresh(world.meeting)
    assert world.meeting.status == MeetingStatus.LIVE


@pytest.mark.fr("FR-23")
def test_timer_writes_closure_opened_event_with_timer_trigger(world: World, frozen) -> None:
    from app.modules.series.tasks import open_due_closures

    go_live(world)
    set_end(world, END)
    open_due_closures(world.db, END - timedelta(minutes=5))
    (event,) = events(world.db, "closure_opened", meeting_id=world.meeting.id)
    assert event.props["trigger"] == "timer"


@pytest.mark.fr("FR-23")
def test_timer_ignores_meeting_already_closed_by_button(host, world: World) -> None:
    from app.modules.series.tasks import open_due_closures

    go_live(world)
    open_closure(host, world)
    set_end(world, END)
    assert open_due_closures(world.db, END - timedelta(minutes=4)) == 0
    world.db.refresh(world.meeting)
    assert world.meeting.closure_trigger == ClosureTrigger.BUTTON
    assert len(events(world.db, "closure_opened", meeting_id=world.meeting.id)) == 1


@pytest.mark.fr("FR-23")
def test_timer_follows_a_moved_planned_end(world: World, frozen) -> None:
    from app.modules.series.tasks import open_due_closures

    go_live(world)
    set_end(world, END + timedelta(minutes=30))
    assert open_due_closures(world.db, END - timedelta(minutes=4)) == 0
    assert open_due_closures(world.db, END + timedelta(minutes=25)) == 1


def agenda(world: World, count: int = 2) -> list[AgendaItem]:
    rows = [
        AgendaItem(meeting_id=world.meeting.id, position=i + 1, title=f"Тема {i + 1}")
        for i in range(count)
    ]
    world.db.add_all(rows)
    world.db.flush()
    return rows


@pytest.mark.fr("FR-27")
def test_agenda_item_without_linked_decision_is_uncovered(world: World, frozen) -> None:
    from app.modules.series.service import uncovered_agenda

    make_closing(world)
    first, second = agenda(world)
    world.item(what="Решение", agenda_item_id=first.id)
    assert [a.id for a in uncovered_agenda(world.db, world.meeting.id)] == [second.id]


@pytest.mark.fr("FR-27")
def test_linking_a_decision_makes_agenda_item_covered(world: World, frozen) -> None:
    from app.modules.series.service import uncovered_agenda

    make_closing(world)
    (topic,) = agenda(world, 1)
    item = world.item(what="Решение")
    assert [a.id for a in uncovered_agenda(world.db, world.meeting.id)] == [topic.id]
    item.agenda_item_id = topic.id
    world.db.flush()
    assert uncovered_agenda(world.db, world.meeting.id) == []


@pytest.mark.fr("FR-27")
def test_llm_matches_with_foreign_ids_are_discarded(world: World, frozen) -> None:
    from app.modules.series.service import apply_agenda_matches

    other = World(world.db)
    foreign_topic = agenda(other, 1)[0]
    foreign_item = other.item(what="Чужое")
    make_closing(world)
    (topic,) = agenda(world, 1)
    item = world.item(what="Своё")
    pairs = [
        {"agenda_item_id": topic.id, "item_id": item.id},
        {"agenda_item_id": foreign_topic.id, "item_id": item.id},
        {"agenda_item_id": topic.id, "item_id": foreign_item.id},
        {"agenda_item_id": uuid.uuid4(), "item_id": uuid.uuid4()},
    ]
    assert apply_agenda_matches(world.db, world.meeting.id, pairs) == 1
    world.db.refresh(item)
    assert item.agenda_item_id == topic.id


@pytest.mark.fr("FR-27")
@pytest.mark.parametrize("status", ["covered", "moved"])
def test_agenda_resolution_is_saved_and_tracked(host, world: World, status: str) -> None:
    make_closing(world)
    (topic,) = agenda(world, 1)
    resp = host.patch(f"{API}/agenda/{topic.id}", json={"status": status})
    assert resp.status_code == 200
    world.db.refresh(topic)
    assert topic.status == AgendaItemStatus(status)
    (event,) = events(world.db, "agenda_item_uncovered", meeting_id=world.meeting.id)
    assert event.props["position"] == 1
    assert event.props["resolution"] == status


@contextmanager
def _task_session(db):
    """Отдельная сессия задачи на соединении теста: commit сессии запроса её не затрагивает."""
    session = Session(
        bind=db.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        session.close()


@pytest.mark.fr("FR-27")
def test_closure_matches_agenda_in_a_task_and_publishes_agenda_updated(
    host, world: World, monkeypatch
) -> None:
    from app.modules.series import service as series_service
    from app.modules.series import tasks as series_tasks
    from tests.llm_fakes import FakeLLM

    go_live(world)
    (topic,) = agenda(world, 1)
    item = world.item(what="Решение по теме")
    fake = FakeLLM(
        json.dumps({"pairs": [{"agenda_item_id": str(topic.id), "item_id": str(item.id)}]})
    )
    monkeypatch.setattr(series_service, "get_llm_client", lambda: fake)
    # задача открывает свою сессию; в тесте она работает в той же транзакции, что и запрос
    monkeypatch.setattr(series_tasks, "SessionLocal", lambda **_: _task_session(world.db))
    with host.websocket_connect(f"/ws/meetings/{world.meeting.id}") as ws:
        assert open_closure(host, world).status_code == 200
        assert len(fake.calls) == 1  # без этой проверки ожидание кадра повисло бы
        frames = []
        while not frames or frames[-1]["type"] != "agenda.updated":
            frames.append(ws.receive_json())
    assert frames[-1]["meeting_id"] == str(world.meeting.id)
    assert len(fake.calls) == 1
    world.db.refresh(item)
    assert item.agenda_item_id == topic.id
