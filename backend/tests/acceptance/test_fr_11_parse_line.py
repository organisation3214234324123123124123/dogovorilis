"""FR-11: разбор строки правилами (parse_line), без сети и LLM; то же через API."""

import socket
import uuid
from dataclasses import dataclass
from datetime import date

import pytest
from fastapi.testclient import TestClient

from tests.acceptance.helpers_items import (
    API,
    TODAY,
    clock,  # noqa: F401
    make_ctx,
    open_gap_types,
)
from tests.factories import World

pytestmark = [
    pytest.mark.fr("FR-11"),
    pytest.mark.usefixtures("clock"),
]

IVAN = uuid.UUID(int=12)
ANNA = uuid.UUID(int=15)


@dataclass(frozen=True)
class P:
    id: uuid.UUID
    full_name: str


COMPOSITION = [P(IVAN, "Иван Петров"), P(ANNA, "Анна Смирнова")]


def parse(text: str, today: date = TODAY, participants=None, manual_fields=()):
    from app.modules.items.parsing import parse_line

    return parse_line(
        text,
        today,
        COMPOSITION if participants is None else participants,
        manual_fields=manual_fields,
    )


@pytest.mark.parametrize(
    ("text", "assignee"),
    [
        ("Иван разберётся с выгрузкой", IVAN),
        ("Поручили Ивану подготовить отчёт", IVAN),
        ("Передать Ивану договор", IVAN),
        ("Отчёт готовит Иван", IVAN),
        ("Договор согласуем с Иваном", IVAN),
        ("Анна подготовит отчёт", ANNA),
        ("с Анной согласовать договор", ANNA),
        ("Поручить Анне отчёт", ANNA),
        ("Договор у Анны", ANNA),
        ("иван сделает отчёт", IVAN),
        ("Иван Петров сделает отчёт", IVAN),
    ],
)
def test_assignee_is_found_by_name_in_any_case(text: str, assignee) -> None:
    assert parse(text).assignee_person_id == assignee


@pytest.mark.parametrize(
    "text",
    [
        "Пётр сделает отчёт",
        "Нужно подготовить отчёт",
        "Договор согласует Сергей",
    ],
)
def test_assignee_not_in_composition_is_empty(text: str) -> None:
    assert parse(text).assignee_person_id is None


def test_namesakes_leave_assignee_empty() -> None:
    twins = [P(uuid.UUID(int=1), "Иван Петров"), P(uuid.UUID(int=2), "Иван Сидоров")]
    assert parse("Иван сделает отчёт", participants=twins).assignee_person_id is None


def test_full_name_disambiguates_namesakes() -> None:
    twins = [P(uuid.UUID(int=1), "Иван Петров"), P(uuid.UUID(int=2), "Иван Сидоров")]
    parsed = parse("Иван Сидоров сделает отчёт", participants=twins)
    assert parsed.assignee_person_id == uuid.UUID(int=2)


def test_empty_composition_has_no_assignee() -> None:
    assert parse("Иван сделает отчёт", participants=[]).assignee_person_id is None


@pytest.mark.parametrize(
    ("text", "due", "phrase"),
    [
        ("Иван разберётся с выгрузкой к пятнице", date(2026, 10, 16), "к пятнице"),
        ("Иван разберётся с выгрузкой до пятницы", date(2026, 10, 16), "до пятницы"),
        ("Иван разберётся с выгрузкой к 20.10", date(2026, 10, 20), "к 20.10"),
        ("Иван разберётся с выгрузкой к 15.11", date(2026, 11, 15), "к 15.11"),
        ("Иван разберётся с выгрузкой до 20.10", date(2026, 10, 20), "до 20.10"),
        ("Иван разберётся с выгрузкой завтра", date(2026, 10, 16), "завтра"),
        ("Пётр сделает отчёт завтра", date(2026, 10, 16), "завтра"),
        ("Иван разберётся с выгрузкой через неделю", date(2026, 10, 22), "через неделю"),
        ("Иван разберётся с выгрузкой до понедельника", date(2026, 10, 19), "до понедельника"),
        ("Иван разберётся с выгрузкой к 1.12", date(2026, 12, 1), "к 1.12"),
        ("Иван разберётся с выгрузкой к 15.10", date(2026, 10, 15), "к 15.10"),
        ("К пятнице Анна подготовит отчёт", date(2026, 10, 16), "К пятнице"),
    ],
)
def test_due_expressions(text: str, due: date, phrase: str) -> None:
    parsed = parse(text)
    assert parsed.due_date == due
    assert parsed.due_text is not None
    assert parsed.due_text.lower() == phrase.lower()


def test_friday_said_on_friday_means_today() -> None:
    parsed = parse("Иван сделает отчёт до пятницы", today=date(2026, 10, 16))
    assert parsed.due_date == date(2026, 10, 16)


def test_friday_said_on_saturday_means_next_friday() -> None:
    parsed = parse("Иван сделает отчёт до пятницы", today=date(2026, 10, 17))
    assert parsed.due_date == date(2026, 10, 23)


def test_date_in_past_gets_current_year() -> None:
    parsed = parse("Иван сделает отчёт к 10.10")
    assert parsed.due_date == date(2026, 10, 10)


def test_tomorrow_at_month_end() -> None:
    parsed = parse("Иван сделает отчёт завтра", today=date(2026, 10, 31))
    assert parsed.due_date == date(2026, 11, 1)


@pytest.mark.parametrize("phrase", ["на днях", "как можно скорее", "потом"])
def test_vague_due_keeps_phrase_without_date(phrase: str) -> None:
    parsed = parse(f"Иван разберётся с выгрузкой {phrase}")
    assert parsed.due_date is None
    assert parsed.due_text == phrase


@pytest.mark.parametrize(
    "text",
    [
        "Иван разберётся с выгрузкой",
        "Анна подготовит отчёт",
        "Подготовить отчёт",
    ],
)
def test_line_without_due_has_no_due_fields(text: str) -> None:
    parsed = parse(text)
    assert (parsed.due_date, parsed.due_text) == (None, None)


def test_what_drops_name_and_due_and_is_not_empty() -> None:
    parsed = parse("Иван разберётся с выгрузкой к пятнице")
    assert parsed.what
    assert "пятниц" not in parsed.what
    assert "Иван" not in parsed.what
    assert "выгрузк" in parsed.what


@pytest.mark.parametrize(
    "field",
    ["assignee_person_id", "due_date", "due_text", "what"],
)
def test_manual_fields_are_not_touched(field: str) -> None:
    parsed = parse("Иван разберётся с выгрузкой к пятнице", manual_fields=(field,))
    assert getattr(parsed, field) is None


def test_manual_field_does_not_block_other_fields() -> None:
    parsed = parse("Иван разберётся с выгрузкой к пятнице", manual_fields=("due_date",))
    assert parsed.assignee_person_id == IVAN


def test_parsing_works_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*args, **kwargs):
        raise AssertionError("сеть недоступна")

    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    parsed = parse("Иван разберётся с выгрузкой к пятнице")
    assert parsed.assignee_person_id == IVAN


def test_parsing_is_deterministic() -> None:
    first = parse("Анна подготовит отчёт через неделю")
    assert parse("Анна подготовит отчёт через неделю") == first


@pytest.fixture
def api_case(authed: TestClient, world: World):
    ctx = make_ctx(world)

    def run(text: str) -> tuple[dict, set[str], object]:
        resp = authed.post(f"{API}/meetings/{world.meeting.id}/items", json={"raw_text": text})
        assert resp.status_code == 201
        body = resp.json()
        card = authed.get(f"{API}/items/{body['id']}").json()
        return card, open_gap_types(world.db, body["id"]), ctx

    return run


def test_api_ivan_friday(api_case) -> None:
    card, _, ctx = api_case("Иван разберётся с выгрузкой к пятнице")
    assert card["assignee"]["id"] == str(ctx.ivan.id)
    assert (card["due_date"], card["due_text"]) == ("2026-10-16", "к пятнице")


def test_api_unknown_assignee_gives_no_assignee_gap(api_case) -> None:
    card, gaps, _ = api_case("Пётр сделает отчёт завтра")
    assert card["assignee"] is None
    assert card["due_date"] == "2026-10-16"
    assert "no_assignee" in gaps


@pytest.mark.parametrize("phrase", ["на днях", "как можно скорее", "потом"])
def test_api_vague_due_gives_due_vague_gap(api_case, phrase: str) -> None:
    card, gaps, _ = api_case(f"Иван сделает отчёт {phrase}")
    assert card["due_date"] is None
    assert card["due_text"] == phrase
    assert "due_vague" in gaps
    assert "no_due" not in gaps


def test_api_no_due_gives_no_due_gap(api_case) -> None:
    card, gaps, _ = api_case("Иван сделает отчёт")
    assert (card["due_date"], card["due_text"]) == (None, None)
    assert "no_due" in gaps


def test_api_past_date_gives_due_past_gap(api_case) -> None:
    card, gaps, _ = api_case("Иван сделает отчёт к 10.10")
    assert card["due_date"] == "2026-10-10"
    assert "due_past" in gaps
