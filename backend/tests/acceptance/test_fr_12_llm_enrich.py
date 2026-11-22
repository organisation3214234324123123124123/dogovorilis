import logging
import uuid
from contextlib import nullcontext
from datetime import date
from typing import Iterator

import pytest
from sqlalchemy import delete, func, select

from app.core.db import SessionLocal, get_engine
from app.modules.analytics.models import AnalyticsEvent
from app.modules.gaps.models import Gap, GapType
from app.modules.items import service as items_service, tasks as items_tasks
from app.modules.items.models import Item, ItemAction, ItemEvent, ItemStatus
from app.modules.items.schemas import ItemCreate
from app.modules.live.models import MeetingEvent
from app.modules.notifications.models import Notification, NotificationType
from app.modules.llm.client import LLMRateLimited
from app.modules.llm.models import LlmCall, LlmOutcome
from app.modules.series.models import MeetingStatus, SeriesStatus
from app.models import User
from tests.factories import (
    World,
    add_member,
    events_named,
    make_item,
    make_meeting,
    make_participant,
    make_organization,
    make_series,
    make_user,
)
from tests.llm_fakes import FakeLLM, reply, timeout_error
from tests.llm_helpers import activate_prompt

pytestmark = pytest.mark.fr("FR-12")


def set_test_session(monkeypatch: pytest.MonkeyPatch, db) -> None:
    monkeypatch.setattr(items_service, "SessionLocal", lambda **kwargs: nullcontext(db))


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def exists(self, key: str) -> bool:
        return key in self.values

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, script: str, count: int, key: str, value: str) -> int:
        if self.values.get(key) == value:
            del self.values[key]
            return 1
        return 0


def test_create_queues_only_item_id_after_commit(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.meeting.status = MeetingStatus.LIVE
    queued: list[tuple] = []
    monkeypatch.setattr(items_tasks.enrich_item, "delay", lambda *args: queued.append(args))
    result = items_service.create_item(
        world.db, world.user, world.meeting.id, ItemCreate(raw_text="уникальнаястрока")
    )
    assert queued == []
    assert world.db.scalar(select(func.count()).select_from(LlmCall)) == 0
    world.db.commit()
    assert queued == [(str(result.id),)]


def test_api_returns_rule_result_without_waiting_for_llm(
    authed, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.meeting.status = MeetingStatus.LIVE
    queued: list[tuple] = []
    monkeypatch.setattr(items_tasks.enrich_item, "delay", lambda *args: queued.append(args))
    response = authed.post(
        f"/api/v1/meetings/{world.meeting.id}/items", json={"raw_text": "строка для разбора"}
    )
    assert response.status_code == 201
    assert queued == [(response.json()["id"],)]
    events = list(world.db.scalars(select(MeetingEvent.event).where(MeetingEvent.item_id == uuid.UUID(response.json()["id"]))))
    assert {"item.created", "item.updated", "gaps.updated"} <= set(events)
    assert world.db.scalar(select(func.count()).select_from(LlmCall)) == 0


def test_failed_queue_does_not_break_created_item_or_log_text(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world.meeting.status = MeetingStatus.LIVE

    def fail(*args):
        raise ConnectionError("redis is unavailable")

    monkeypatch.setattr(items_tasks.enrich_item, "delay", fail)
    with caplog.at_level(logging.WARNING):
        result = items_service.create_item(
            world.db, world.user, world.meeting.id, ItemCreate(raw_text="секретнаястрока")
        )
        world.db.commit()
    assert world.db.get(Item, result.id) is not None
    assert world.db.scalar(select(func.count()).select_from(LlmCall)) == 0
    assert "секретнаястрока" not in caplog.text
    assert "разбор пункта" in caplog.text


@pytest.mark.parametrize(
    ("responses", "outcome"),
    [
        ([timeout_error(), timeout_error()], LlmOutcome.TIMEOUT),
        (["bad json", "still bad"], LlmOutcome.INVALID_OUTPUT),
        ([LLMRateLimited("http 429")], LlmOutcome.INVALID_OUTPUT),
    ],
)
def test_llm_failures_are_logged_and_do_not_change_item(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    responses: list,
    outcome: LlmOutcome,
) -> None:
    activate_prompt(world.db)
    item = world.item(raw_text="секретнаястрока")
    before_version = item.version
    set_test_session(monkeypatch, world.db)
    monkeypatch.setattr(items_service, "get_llm_client", lambda: FakeLLM(*responses))
    monkeypatch.setattr(items_service, "get_engine", lambda: None)
    items_service.enrich_item_background(item.id)
    world.db.refresh(item)
    call = world.db.scalar(select(LlmCall).order_by(LlmCall.created_at.desc()))
    assert call.outcome == outcome
    assert item.version == before_version
    assert item.what is None


def test_stub_skips_llm_and_call_log(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.llm.client import StubLLMClient

    item = world.item(raw_text="секретнаястрока")
    set_test_session(monkeypatch, world.db)
    monkeypatch.setattr(items_service, "get_llm_client", StubLLMClient)
    monkeypatch.setattr(items_service, "get_engine", lambda: None)
    before = world.db.scalar(select(func.count()).select_from(LlmCall))
    assert items_service.enrich_item_background(item.id) is False
    assert world.db.scalar(select(func.count()).select_from(LlmCall)) == before
    assert item.what is None


def test_sent_meeting_does_not_queue_or_enrich(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.meeting.status = MeetingStatus.SENT
    queued: list[tuple] = []
    monkeypatch.setattr(items_tasks.enrich_item, "delay", lambda *args: queued.append(args))
    result = items_service.create_item(
        world.db, world.user, world.meeting.id, ItemCreate(raw_text="строка после рассылки")
    )
    world.db.commit()
    assert result.status == ItemStatus.PENDING
    assert queued == []
    assert world.db.get(Item, result.id) is not None
    assert world.db.query(Notification).filter_by(type=NotificationType.CONFIRM_REQUEST).count() == 0


def test_apply_fills_only_empty_and_non_manual_fields(world: World) -> None:
    from app.modules.llm import service as llm_service

    item = world.item(
        what="Уже записано",
        due_text="в ноябре",
        due_date=date(2026, 11, 30),
        manual_fields=["done_criteria"],
    )
    parsed = llm_service.ParseResult(
        LlmOutcome.OK,
        what="Подготовить отчёт",
        due_text="к пятнице",
        due_date=date(2026, 12, 4),
        done_criteria="Отчёт лежит в общей папке",
    )
    items_service.apply_llm_result(world.db, item.id, parsed)
    world.db.commit()
    world.db.refresh(item)
    assert item.what == "Уже записано"
    assert (item.due_date, item.due_text) == (date(2026, 11, 30), "в ноябре")
    assert item.done_criteria is None
    assert item.manual_fields == ["done_criteria"]
    assert item.version == 1


def test_manual_edit_and_clear_while_llm_runs_are_kept(world: World) -> None:
    from app.modules.llm import service as llm_service

    activate_prompt(world.db)
    item = world.item(raw_text="исходная строка", due_date=None)
    parsed = llm_service.ParseResult(
        LlmOutcome.OK,
        what="Подготовить отчёт",
        due_date=date(2026, 12, 4),
        due_text="к пятнице",
    )
    item.due_date = date(2026, 11, 30)
    item.manual_fields = ["due_date"]
    item.what = None
    item.manual_fields.append("what")
    world.db.flush()
    items_service.apply_llm_result(world.db, item.id, parsed)
    world.db.commit()
    world.db.refresh(item)
    assert item.due_date == date(2026, 11, 30)
    assert item.what is None
    assert item.manual_fields == ["due_date", "what"]


def test_accepted_item_keeps_all_agreed_fields(
    world: World,
) -> None:
    from app.modules.llm import service as llm_service

    item = world.item(
        what="Согласованное действие",
        due_date=date(2026, 12, 4),
        due_text="к пятнице",
        done_criteria="согласованный результат",
        accepted_in_meeting=True,
    )
    parsed = llm_service.ParseResult(
        LlmOutcome.OK,
        what="Другое действие",
        assignee_id=None,
        due_date=date(2026, 12, 11),
        due_text="через неделю",
        done_criteria="другой результат",
    )
    items_service.apply_llm_result(world.db, item.id, parsed)
    world.db.commit()
    world.db.refresh(item)
    assert (item.what, item.due_date, item.due_text, item.done_criteria) == (
        "Согласованное действие",
        date(2026, 12, 4),
        "к пятнице",
        "согласованный результат",
    )
    assert item.assignee_person_id is None
    assert item.version == 1


def test_assignee_removed_while_llm_works_is_not_applied(world: World) -> None:
    from app.modules.llm import service as llm_service
    from app.modules.series.models import SeriesMember
    from tests.factories import add_member, make_person

    person = make_person(world.db, world.user, world.org, full_name="Анна Смирнова")
    add_member(world.db, world.series, person)
    item = world.item()
    world.db.delete(world.db.get(SeriesMember, (world.series.id, person.id)))
    world.db.flush()
    parsed = llm_service.ParseResult(
        LlmOutcome.OK,
        assignee_id=str(person.id),
    )
    items_service.apply_llm_result(world.db, item.id, parsed)
    world.db.commit()
    world.db.refresh(item)
    assert item.assignee_person_id is None
    assert item.version == 1


def test_deleted_item_discards_result(world: World) -> None:
    from app.modules.llm import service as llm_service

    item = world.item()
    item_id = item.id
    world.db.delete(item)
    world.db.flush()
    parsed = llm_service.ParseResult(LlmOutcome.OK, what="Новое действие")
    assert items_service.apply_llm_result(world.db, item_id, parsed) is False


@pytest.mark.parametrize("state", ["sent", "archived"])
def test_result_is_ignored_after_meeting_or_series_closes(world: World, state: str) -> None:
    from app.modules.llm import service as llm_service

    item = world.item()
    if state == "sent":
        world.meeting.status = MeetingStatus.SENT
        item.status = ItemStatus.PENDING
    else:
        world.series.status = SeriesStatus.ARCHIVED
    parsed = llm_service.ParseResult(LlmOutcome.OK, what="Новое действие")
    items_service.apply_llm_result(world.db, item.id, parsed)
    world.db.commit()
    world.db.refresh(item)
    assert item.what is None
    assert item.version == 1


def test_changed_result_updates_journal_gaps_and_panel(world: World) -> None:
    from app.modules.gaps.service import recalc
    from app.modules.llm import service as llm_service

    add_member(world.db, world.series, world.person)
    make_participant(world.db, world.meeting, world.person)
    item = world.item(
        what="Подготовить отчёт", due_date=None, assignee_person_id=world.person.id
    )
    recalc(world.db, item, date(2026, 11, 22), "Host")
    blocked_before = items_service.to_reads(world.db, [item], world.series.timezone)[0]
    gap = world.db.scalar(select(Gap).where(Gap.item_id == item.id, Gap.type == GapType.NO_DUE))
    parsed = llm_service.ParseResult(
        LlmOutcome.OK,
        what="Подготовить отчёт",
        due_date=date(2026, 12, 4),
        done_criteria="Отчёт в общей папке",
    )
    items_service.apply_llm_result(world.db, item.id, parsed)
    world.db.commit()
    world.db.refresh(item)
    event = world.db.scalar(
        select(ItemEvent).where(ItemEvent.item_id == item.id, ItemEvent.action == ItemAction.UPDATED)
    )
    frames = list(world.db.scalars(select(MeetingEvent).where(MeetingEvent.item_id == item.id)))
    assert item.version == 2
    assert event.data["fields"] == ["due_date", "done_criteria"]
    assert event.data["by"] == "llm"
    assert gap.resolved_at is not None
    assert events_named(world.db, "gap_resolved", item_id=str(item.id))
    blocked_after = items_service.to_reads(world.db, [item], world.series.timezone)[0]
    assert blocked_before.accept_blocked_reason is not None
    assert blocked_after.accept_blocked_reason is None
    assert [frame.event for frame in frames].count("item.updated") == 1
    assert [frame.event for frame in frames].count("gaps.updated") == 1
    assert next(frame for frame in frames if frame.event == "item.updated").data["version"] == 2


def test_item_added_after_send_keeps_rule_fields_and_confirmation_mail(
    authed, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.factories import add_member

    world.meeting.status = MeetingStatus.SENT
    world.person.full_name = "Иван Петров"
    add_member(world.db, world.series, world.person)
    queued: list[tuple] = []
    monkeypatch.setattr(items_tasks.enrich_item, "delay", lambda *args: queued.append(args))
    response = authed.post(
        f"/api/v1/meetings/{world.meeting.id}/items",
        json={"raw_text": "Иван Петров подготовит отчёт к пятнице"},
    )
    assert response.status_code == 201
    item_id = uuid.UUID(response.json()["id"])
    assert response.json()["status"] == ItemStatus.PENDING
    assert response.json()["what"] is not None
    assert response.json()["due_date"] is not None
    assert queued == []
    assert world.db.query(Notification).filter_by(
        item_id=item_id, type=NotificationType.CONFIRM_REQUEST
    ).count() == 1


@pytest.fixture
def analytics_event_count(migrated_db: None) -> Iterator[int]:
    with SessionLocal(bind=get_engine()) as db:
        count = db.scalar(select(func.count()).select_from(AnalyticsEvent))
    yield count
    with SessionLocal(bind=get_engine()) as db:
        remaining = db.scalar(select(func.count()).select_from(AnalyticsEvent))
    assert remaining == count


@pytest.fixture
def committed_item(migrated_db: None, analytics_event_count: int) -> Iterator[str]:
    with SessionLocal(bind=get_engine()) as db:
        user = make_user(db)
        organization = make_organization(db, user, is_own=True)
        series = make_series(db, user)
        meeting = make_meeting(db, series, status=MeetingStatus.LIVE)
        item = make_item(db, meeting, raw_text="СекретноеУникальноеСлово подготовить отчёт")
        item_id = str(item.id)
        meeting_id = meeting.id
        user_id = user.id
        db.commit()
    yield item_id
    with SessionLocal(bind=get_engine()) as db:
        db.execute(delete(LlmCall).where(LlmCall.meeting_id == meeting_id))
        db.execute(
            delete(AnalyticsEvent).where(
                (AnalyticsEvent.item_id == uuid.UUID(item_id))
                | (AnalyticsEvent.meeting_id == meeting_id)
            )
        )
        db.execute(delete(Item).where(Item.id == uuid.UUID(item_id)))
        db.execute(delete(User).where(User.id == user_id))
        db.commit()


def test_committed_task_retry_does_not_call_llm_or_write_again(
    committed_item: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.modules.llm import service as llm_service

    client = FakeLLM(
        reply(
            what="Подготовить отчёт",
            due_date=None,
            due_text=None,
            done_criteria="Отчёт лежит в общей папке",
            vague_action=False,
            question=None,
        )
    )
    cache = MemoryRedis()
    monkeypatch.setattr(items_service, "get_llm_client", lambda: client)
    monkeypatch.setattr(items_tasks.redis.Redis, "from_url", lambda url: cache)
    with caplog.at_level(logging.DEBUG):
        first = items_tasks.enrich_item.apply(args=[committed_item]).get()
        with SessionLocal(bind=get_engine()) as db:
            item = db.get(Item, uuid.UUID(committed_item))
            version = item.version
            event_count = db.scalar(
                select(func.count()).select_from(ItemEvent).where(ItemEvent.item_id == item.id)
            )
            call_count = db.scalar(select(func.count()).select_from(LlmCall))
            item_events = list(db.scalars(select(ItemEvent).where(ItemEvent.item_id == item.id)))
            frame_rows = list(db.scalars(select(MeetingEvent).where(MeetingEvent.item_id == item.id)))
            frame_count = db.scalar(
                select(func.count()).select_from(MeetingEvent).where(MeetingEvent.item_id == item.id)
            )
        cache.values.clear()
        second = items_tasks.enrich_item.apply(args=[committed_item]).get()
    assert first is True
    assert second is False
    assert len(client.calls) == 1
    assert any(event.data.get("fields") == ["what", "done_criteria"] for event in item_events)
    assert sum(frame.event == "item.updated" for frame in frame_rows) == 1
    assert sum(frame.event == "gaps.updated" for frame in frame_rows) == 1
    assert "СекретноеУникальноеСлово" not in caplog.text
    with SessionLocal(bind=get_engine()) as db:
        item = db.get(Item, uuid.UUID(committed_item))
        assert item.llm_enrichment_done is True
        assert item.version == version
        assert db.scalar(
            select(func.count()).select_from(ItemEvent).where(ItemEvent.item_id == item.id)
        ) == event_count
        assert db.scalar(select(func.count()).select_from(LlmCall)) == call_count
        assert db.scalar(
            select(func.count()).select_from(MeetingEvent).where(MeetingEvent.item_id == item.id)
        ) == frame_count
