import json
import logging
from datetime import date

import pytest
from sqlalchemy import func, select

from app.models import Person
from app.modules.analytics.models import AnalyticsEvent
from app.modules.llm.models import LlmCall, LlmOutcome, LlmTask
from tests.factories import make_person
from tests.llm_fakes import FakeLLM, llm_error, reply, timeout_error
from tests.llm_helpers import activate_prompt

pytestmark = pytest.mark.fr("FR-12")

TODAY = date(2026, 10, 15)
TEXT = "Секретный Иван разберётся с выгрузкой Сбербанка к пятнице"
COLUMNS = {
    "id",
    "meeting_id",
    "task",
    "provider",
    "model",
    "prompt_id",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "outcome",
    "created_at",
}


@pytest.fixture
def persons(world) -> list[Person]:
    world.person.full_name = "Иван Петров"
    world.person.email = "ivan.petrov@alfa.test"
    anna = make_person(world.db, world.user, world.org, full_name="Анна Смирнова")
    world.db.flush()
    return [world.person, anna]


@pytest.fixture
def prompt(world):
    return activate_prompt(world.db, text="SYSTEM-PROMPT-TEXT")


def run(world, persons, client):
    from app.modules.llm import service

    return service.parse_text(
        world.db, client, meeting_id=world.meeting.id, text=TEXT, today=TODAY, persons=persons
    )


def calls(world) -> list[LlmCall]:
    return list(world.db.scalars(select(LlmCall).where(LlmCall.meeting_id == world.meeting.id)))


def test_ok_call_writes_one_row(world, persons, prompt) -> None:
    run(world, persons, FakeLLM(reply()))
    rows = calls(world)
    assert len(rows) == 1
    assert rows[0].outcome == LlmOutcome.OK


def test_row_has_task_provider_model_prompt(world, persons, prompt) -> None:
    run(world, persons, FakeLLM(reply()))
    row = calls(world)[0]
    assert row.task == LlmTask.PARSE
    assert (row.provider, row.model) == ("fake", "fake-1")
    assert row.prompt_id == prompt.id


def test_row_has_tokens_and_latency(world, persons, prompt) -> None:
    run(world, persons, FakeLLM(reply()))
    row = calls(world)[0]
    assert (row.input_tokens, row.output_tokens) == (120, 40)
    assert row.latency_ms is not None
    assert row.latency_ms >= 0


def test_timeout_is_logged(world, persons, prompt) -> None:
    run(world, persons, FakeLLM(timeout_error()))
    assert [r.outcome for r in calls(world)] == [LlmOutcome.TIMEOUT]


def test_invalid_output_is_logged_once_after_retry(world, persons, prompt) -> None:
    run(world, persons, FakeLLM("x", "y"))
    assert [r.outcome for r in calls(world)] == [LlmOutcome.INVALID_OUTPUT]


def test_successful_retry_is_logged_as_ok(world, persons, prompt) -> None:
    run(world, persons, FakeLLM("x", reply()))
    assert [r.outcome for r in calls(world)] == [LlmOutcome.OK]


def test_provider_error_does_not_crash_and_does_not_log_ok(world, persons, prompt) -> None:
    result = run(world, persons, FakeLLM(llm_error()))
    assert result.outcome != LlmOutcome.OK
    assert [r.outcome for r in calls(world)] == [LlmOutcome.INVALID_OUTPUT]


def test_row_values_contain_no_personal_data(world, persons, prompt) -> None:
    run(world, persons, FakeLLM(reply(question="Иван, что будет результатом?")))
    row = calls(world)[0]
    dumped = json.dumps({c: str(getattr(row, c)) for c in COLUMNS - {"id"}}, ensure_ascii=False)
    for secret in ("Секретный", "Сбербанка", "Иван", "Петров", "alfa.test", "SYSTEM-PROMPT-TEXT"):
        assert secret not in dumped


def test_call_is_not_written_to_analytics_events(world, persons, prompt) -> None:
    before = world.db.scalar(select(func.count()).select_from(AnalyticsEvent))
    run(world, persons, FakeLLM(reply()))
    assert world.db.scalar(select(func.count()).select_from(AnalyticsEvent)) == before


def test_request_text_is_not_logged(world, persons, prompt, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        run(world, persons, FakeLLM(reply()))
    assert "Сбербанка" not in caplog.text
    assert "Секретный" not in caplog.text


def test_response_text_is_not_logged(world, persons, prompt, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        run(world, persons, FakeLLM(reply(what="Разобраться с уникальнымтекстом")))
    assert "уникальнымтекстом" not in caplog.text


def test_new_prompt_version_is_recorded_in_next_call(world, persons, prompt) -> None:
    run(world, persons, FakeLLM(reply()))
    newer = activate_prompt(world.db, text="V2")
    run(world, persons, FakeLLM(reply()))
    ids = [r.prompt_id for r in sorted(calls(world), key=lambda r: r.created_at)]
    assert set(ids) == {prompt.id, newer.id}


def test_second_call_uses_new_prompt_text(world, persons, prompt) -> None:
    activate_prompt(world.db, text="V2")
    client = FakeLLM(reply())
    run(world, persons, client)
    assert client.calls[0]["system_prompt"] == "V2"


def test_table_has_no_columns_for_request_or_response() -> None:
    assert {c.name for c in LlmCall.__table__.columns} == COLUMNS
