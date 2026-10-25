import json
from datetime import date

import pytest

from app.models import Person
from app.modules.llm.models import LlmOutcome
from tests.factories import make_person
from tests.llm_fakes import FakeLLM, llm_error, reply, timeout_error
from tests.llm_helpers import activate_prompt

pytestmark = pytest.mark.fr("FR-12")

TODAY = date(2026, 10, 15)
TEXT = "Иван разберётся с выгрузкой к пятнице"


@pytest.fixture
def persons(world) -> list[Person]:
    world.person.full_name = "Иван Петров"
    world.person.email = "ivan.petrov@alfa.test"
    anna = make_person(world.db, world.user, world.org, full_name="Анна Смирнова")
    world.db.flush()
    return [world.person, anna]


@pytest.fixture(autouse=True)
def prompt(world):
    return activate_prompt(world.db, text="SYSTEM-PARSE-V")


def run(world, persons, client, text: str = TEXT):
    from app.modules.llm import service

    return service.parse_text(
        world.db, client, meeting_id=world.meeting.id, text=text, today=TODAY, persons=persons
    )


def test_payload_has_only_contract_keys(world, persons) -> None:
    client = FakeLLM(reply())
    run(world, persons, client)
    assert set(client.calls[0]["payload"]) == {"text", "today", "weekday", "participants"}


def test_payload_values(world, persons) -> None:
    client = FakeLLM(reply())
    run(world, persons, client)
    payload = client.calls[0]["payload"]
    assert payload["text"] == TEXT
    assert payload["today"] == "2026-10-15"
    assert payload["weekday"] == "четверг"


def test_participants_are_id_and_first_name_only(world, persons) -> None:
    client = FakeLLM(reply())
    run(world, persons, client)
    assert client.calls[0]["payload"]["participants"] == [
        {"id": str(persons[0].id), "name": "Иван"},
        {"id": str(persons[1].id), "name": "Анна"},
    ]


def test_surname_and_email_never_reach_the_client(world, persons) -> None:
    client = FakeLLM(reply())
    run(world, persons, client)
    sent = json.dumps(client.calls[0], ensure_ascii=False)
    assert "Петров" not in sent
    assert "Смирнова" not in sent
    assert "alfa.test" not in sent


def test_system_prompt_is_active_version_from_db(world, persons) -> None:
    client = FakeLLM(reply())
    run(world, persons, client)
    assert client.calls[0]["system_prompt"] == "SYSTEM-PARSE-V"


def test_timeout_argument_is_eight_seconds(world, persons) -> None:
    client = FakeLLM(reply())
    run(world, persons, client)
    assert 7 < client.calls[0]["timeout"] <= 8


@pytest.mark.fr("FR-21")
def test_valid_reply_is_returned(world, persons) -> None:
    client = FakeLLM(reply(assignee_id=str(persons[0].id)))
    res = run(world, persons, client)
    assert res.outcome == LlmOutcome.OK
    assert res.what == "Разобраться с выгрузкой"
    assert res.assignee_id == str(persons[0].id)
    assert res.due_date == date(2026, 10, 16)
    assert res.due_text == "к пятнице"
    assert res.vague_action is True
    assert res.question.startswith("Иван, что будет результатом")


def test_assignee_outside_participants_is_dropped(world, persons) -> None:
    res = run(world, persons, FakeLLM(reply(assignee_id="p_999")))
    assert res.assignee_id is None
    assert res.what == "Разобраться с выгрузкой"


@pytest.mark.parametrize("due", ["2026-13-45", "not-a-date", "2026-10-14", "2020-01-01", 12345])
def test_invalid_or_past_due_date_is_dropped(world, persons, due) -> None:
    res = run(world, persons, FakeLLM(reply(due_date=due)))
    assert res.due_date is None


def test_due_date_equal_to_today_is_kept(world, persons) -> None:
    res = run(world, persons, FakeLLM(reply(due_date="2026-10-15")))
    assert res.due_date == TODAY


@pytest.mark.parametrize("what", ["Выгрузка готова", "Иван сделал отчёт", "готово", ""])
def test_what_without_infinitive_is_dropped(world, persons, what) -> None:
    res = run(world, persons, FakeLLM(reply(what=what)))
    assert res.what is None


@pytest.mark.parametrize("what", ["Подготовить отчёт", "Отправить письмо", "Принести ноутбук"])
def test_what_with_infinitive_is_kept(world, persons, what) -> None:
    res = run(world, persons, FakeLLM(reply(what=what)))
    assert res.what == what


@pytest.mark.fr("FR-21")
def test_question_of_121_chars_is_dropped(world, persons) -> None:
    res = run(world, persons, FakeLLM(reply(question="я" * 121)))
    assert res.question is None


@pytest.mark.fr("FR-21")
def test_question_of_120_chars_is_kept(world, persons) -> None:
    res = run(world, persons, FakeLLM(reply(question="я" * 120)))
    assert res.question == "я" * 120


def test_one_bad_field_does_not_drop_the_rest(world, persons) -> None:
    res = run(world, persons, FakeLLM(reply(question="я" * 500)))
    assert res.what == "Разобраться с выгрузкой"
    assert res.due_date == date(2026, 10, 16)


def test_invalid_json_is_retried_once(world, persons) -> None:
    client = FakeLLM("not json", reply())
    res = run(world, persons, client)
    assert len(client.calls) == 2
    assert res.outcome == LlmOutcome.OK
    assert res.what == "Разобраться с выгрузкой"


def test_two_invalid_json_give_invalid_output_without_third_try(world, persons) -> None:
    client = FakeLLM("not json", "{broken")
    res = run(world, persons, client)
    assert len(client.calls) == 2
    assert res.outcome == LlmOutcome.INVALID_OUTPUT


def test_invalid_output_leaves_all_fields_empty(world, persons) -> None:
    res = run(world, persons, FakeLLM("nope", "nope"))
    assert (res.what, res.assignee_id, res.due_date, res.question) == (None, None, None, None)


@pytest.mark.parametrize("content", ["[]", '"text"', "42", "null"])
def test_json_that_is_not_an_object_counts_as_invalid(world, persons, content) -> None:
    res = run(world, persons, FakeLLM(content, content))
    assert res.outcome == LlmOutcome.INVALID_OUTPUT


@pytest.mark.fr("FR-21")
def test_timeout_falls_back_without_exception(world, persons) -> None:
    client = FakeLLM(timeout_error())
    res = run(world, persons, client)
    assert res.outcome == LlmOutcome.TIMEOUT
    assert res.what is None
    assert len(client.calls) == 1


def test_provider_error_falls_back_without_exception(world, persons) -> None:
    res = run(world, persons, FakeLLM(llm_error()))
    assert res.outcome != LlmOutcome.OK
    assert (res.what, res.due_date, res.question) == (None, None, None)


def enrich(world, persons, client, item):
    from app.modules.llm import service

    return service.enrich_item(world.db, client, item, today=TODAY, persons=persons)


def test_enrich_fills_empty_fields(world, persons) -> None:
    item = world.item(raw_text=TEXT)
    enrich(world, persons, FakeLLM(reply(assignee_id=str(persons[0].id))), item)
    assert item.what == "Разобраться с выгрузкой"
    assert item.assignee_person_id == persons[0].id
    assert item.due_date == date(2026, 10, 16)
    assert item.due_text == "к пятнице"


def test_enrich_keeps_fields_found_by_rules(world, persons) -> None:
    item = world.item(
        raw_text=TEXT,
        what="Выгрузка",
        assignee_person_id=persons[1].id,
        due_date=date(2026, 11, 1),
        due_text="к 1 ноября",
    )
    enrich(world, persons, FakeLLM(reply(assignee_id=str(persons[0].id))), item)
    assert item.what == "Выгрузка"
    assert item.assignee_person_id == persons[1].id
    assert item.due_date == date(2026, 11, 1)
    assert item.due_text == "к 1 ноября"


def test_enrich_adds_only_what_rules_missed(world, persons) -> None:
    item = world.item(raw_text=TEXT, assignee_person_id=persons[1].id)
    enrich(world, persons, FakeLLM(reply(assignee_id=str(persons[0].id))), item)
    assert item.assignee_person_id == persons[1].id
    assert item.due_date == date(2026, 10, 16)


@pytest.mark.parametrize(
    ("field", "column"),
    [("what", "what"), ("due_date", "due_date"), ("due_text", "due_text")],
)
def test_manual_field_is_never_changed_even_when_empty(world, persons, field, column) -> None:
    item = world.item(raw_text=TEXT, manual_fields=[field])
    enrich(world, persons, FakeLLM(reply()), item)
    assert getattr(item, column) is None


def test_manual_assignee_is_never_changed(world, persons) -> None:
    item = world.item(raw_text=TEXT, manual_fields=["assignee_person_id"])
    enrich(world, persons, FakeLLM(reply(assignee_id=str(persons[0].id))), item)
    assert item.assignee_person_id is None


def test_manual_field_with_value_survives(world, persons) -> None:
    item = world.item(raw_text=TEXT, what="Моя правка", manual_fields=["what"])
    enrich(world, persons, FakeLLM(reply()), item)
    assert item.what == "Моя правка"


def test_manual_fields_list_is_not_modified_by_llm(world, persons) -> None:
    item = world.item(raw_text=TEXT, manual_fields=["what"])
    enrich(world, persons, FakeLLM(reply()), item)
    assert item.manual_fields == ["what"]


def test_enrich_on_timeout_leaves_item_untouched(world, persons) -> None:
    item = world.item(raw_text=TEXT, what="Выгрузка")
    res = enrich(world, persons, FakeLLM(timeout_error()), item)
    assert res.outcome == LlmOutcome.TIMEOUT
    assert (item.what, item.due_date, item.assignee_person_id) == ("Выгрузка", None, None)


def test_enrich_on_invalid_output_leaves_item_untouched(world, persons) -> None:
    item = world.item(raw_text=TEXT)
    enrich(world, persons, FakeLLM("x", "y"), item)
    assert (item.what, item.due_date, item.assignee_person_id) == (None, None, None)


def test_enrich_sends_raw_text_of_item(world, persons) -> None:
    client = FakeLLM(reply())
    enrich(world, persons, client, world.item(raw_text=TEXT))
    assert client.calls[0]["payload"]["text"] == TEXT


def test_llm_never_notifies_anyone(world, persons) -> None:
    from app.modules.notifications.models import Notification

    run(world, persons, FakeLLM(reply()))
    assert world.db.query(Notification).count() == 0


# --- общий дедлайн 8 с, стоп-список существительных ---


class SlowLLM(FakeLLM):
    """Каждый вызов "тратит" время на поддельных часах; не уложился в timeout - LLMTimeout."""

    def __init__(self, clock: "Clock", spend: float, *replies, honor_timeout: bool = True) -> None:
        super().__init__(*replies)
        self.clock = clock
        self.spend = spend
        self.honor_timeout = honor_timeout

    def complete(self, system_prompt: str, payload: dict, *, timeout: float):
        self.clock.offset += min(self.spend, timeout) if self.honor_timeout else self.spend
        if self.honor_timeout and self.spend >= timeout:
            self.calls.append(
                {"system_prompt": system_prompt, "payload": payload, "timeout": timeout}
            )
            raise timeout_error()
        return super().complete(system_prompt, payload, timeout=timeout)


class Clock:
    offset = 0.0


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    import time

    c = Clock()
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + c.offset)
    return c


def test_deadline_timeouts_never_exceed_eight_seconds_in_total(world, persons, clock) -> None:
    client = SlowLLM(clock, 100.0, "not json")
    res = run(world, persons, client)
    assert res.outcome == LlmOutcome.TIMEOUT
    assert sum(c["timeout"] for c in client.calls) <= 8
    assert clock.offset <= 8


def test_no_retry_when_first_attempt_used_whole_budget(world, persons, clock) -> None:
    client = SlowLLM(clock, 8.5, "not json", reply(), honor_timeout=False)
    res = run(world, persons, client)
    assert len(client.calls) == 1
    assert res.outcome in (LlmOutcome.INVALID_OUTPUT, LlmOutcome.TIMEOUT)


def test_retry_gets_only_remaining_time(world, persons, clock) -> None:
    client = SlowLLM(clock, 3.0, "not json", reply())
    res = run(world, persons, client)
    assert res.outcome == LlmOutcome.OK
    assert len(client.calls) == 2
    first, second = (c["timeout"] for c in client.calls)
    assert first <= 8
    assert second <= 5.01


def test_deadline_timeout_is_logged_as_timeout(world, persons, clock) -> None:
    from sqlalchemy import select

    from app.modules.llm.models import LlmCall

    run(world, persons, SlowLLM(clock, 100.0, "x"))
    rows = list(world.db.scalars(select(LlmCall).where(LlmCall.meeting_id == world.meeting.id)))
    assert [r.outcome for r in rows] == [LlmOutcome.TIMEOUT]


@pytest.mark.parametrize(
    "what",
    [
        "Часть работы готова",
        "Сеть упала",
        "Путь согласован",
        "Пути согласованы",
        "Связь налажена",
        "Область определена",
        "Власть передана",
        "Новость опубликована",
    ],
)
def test_nouns_ending_on_t_are_not_infinitives(world, persons, what) -> None:
    assert run(world, persons, FakeLLM(reply(what=what))).what is None


@pytest.mark.parametrize("what", ["Идти на встречу", "Привести в порядок", "Помочь коллеге"])
def test_stoplist_does_not_break_real_infinitives(world, persons, what) -> None:
    assert run(world, persons, FakeLLM(reply(what=what))).what == what
