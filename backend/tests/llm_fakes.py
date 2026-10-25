"""Подменяемый LLMClient для тестов, без сети."""

import json
from typing import Any

VALID = {
    "what": "Разобраться с выгрузкой",
    "assignee_id": None,
    "due_date": "2026-10-16",
    "due_text": "к пятнице",
    "done_criteria": None,
    "vague_action": True,
    "question": "Иван, что будет результатом к пятнице - файл выгрузки или план?",
}


def reply(**override: Any) -> str:
    return json.dumps({**VALID, **override}, ensure_ascii=False)


class FakeLLM:
    provider = "fake"
    model = "fake-1"

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def complete(self, system_prompt: str, payload: dict, *, timeout: float):
        from app.modules.llm.client import LLMResponse

        self.calls.append({"system_prompt": system_prompt, "payload": payload, "timeout": timeout})
        item = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return LLMResponse(content=item, input_tokens=120, output_tokens=40)
        return item


def timeout_error() -> Exception:
    from app.modules.llm.client import LLMTimeout

    return LLMTimeout("timeout")


def llm_error() -> Exception:
    from app.modules.llm.client import LLMError

    return LLMError("boom")
