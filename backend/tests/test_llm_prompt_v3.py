"""Промпт разбора строки v3 после миграций (FR-12): пустые слова вместо дела и чужой исполнитель."""

import pytest
from sqlalchemy import select

from app.modules.llm.models import LlmPrompt, LlmTask

pytestmark = pytest.mark.fr("FR-12")


def _parse_prompts(db) -> dict[int, LlmPrompt]:
    rows = db.scalars(select(LlmPrompt).where(LlmPrompt.task == LlmTask.PARSE)).all()
    return {p.version: p for p in rows}


def test_active_parse_prompt_is_v3(db) -> None:
    prompts = _parse_prompts(db)
    active = [p for p in prompts.values() if p.is_active]
    assert [p.version for p in active] == [3]
    assert "нет в команде" in active[0].text
    assert "what равно null" in active[0].text
    assert not prompts[2].is_active
