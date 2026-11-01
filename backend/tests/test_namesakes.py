"""Тёзки в составе (OQ-08): правила не выбирают, вопрос называет всех полностью."""

import uuid
from dataclasses import dataclass
from datetime import date

import pytest

from app.modules.gaps.models import GapType
from app.modules.gaps.rules import question_for
from app.modules.items.parsing import parse_line

pytestmark = pytest.mark.fr("OQ-08")

TODAY = date(2026, 10, 15)


@dataclass
class P:
    full_name: str
    id: uuid.UUID = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.id = uuid.uuid4()


TEAM = [P("Дмитрий Белов"), P("Дмитрий Козлов"), P("Анна Смирнова")]


def test_namesakes_without_surname_leave_assignee_empty() -> None:
    parsed = parse_line("Дмитрий пришлёт отчёт к пятнице", TODAY, TEAM)
    assert parsed.assignee_person_id is None
    assert parsed.namesakes == ["Дмитрий Белов", "Дмитрий Козлов"]


def test_surname_picks_one_namesake() -> None:
    parsed = parse_line("Дмитрий Козлов пришлёт отчёт", TODAY, TEAM)
    assert parsed.assignee_person_id == TEAM[1].id
    assert parsed.namesakes == []


def test_manual_assignee_clears_namesakes() -> None:
    parsed = parse_line("Дмитрий пришлёт отчёт", TODAY, TEAM, {"assignee_person_id"})
    assert parsed.namesakes == []


def test_namesake_question_lists_full_names() -> None:
    q = question_for(
        None,
        GapType.NO_ASSIGNEE,
        name=None,
        due_text=None,
        namesakes=["Дмитрий Белов", "Дмитрий Козлов"],
    )
    assert q == "Какой Дмитрий: Дмитрий Белов или Дмитрий Козлов?"


def test_no_namesakes_keeps_default_question() -> None:
    q = question_for(None, GapType.NO_ASSIGNEE, name=None, due_text=None, namesakes=[])
    assert q == "Кто берёт это на себя?"
