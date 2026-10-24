import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import get_settings

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "what": {"type": ["string", "null"]},
        "assignee_id": {"type": ["string", "null"]},
        "due_date": {"type": ["string", "null"]},
        "due_text": {"type": ["string", "null"]},
        "done_criteria": {"type": ["string", "null"]},
        "vague_action": {"type": "boolean"},
        "question": {"type": ["string", "null"]},
    },
    "required": [
        "what",
        "assignee_id",
        "due_date",
        "due_text",
        "done_criteria",
        "vague_action",
        "question",
    ],
    "additionalProperties": False,
}


logger = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMTimeout(LLMError):
    pass


@dataclass(frozen=True)
class LLMResponse:
    content: str
    input_tokens: int | None
    output_tokens: int | None


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, system_prompt: str, payload: dict, *, timeout: float) -> LLMResponse: ...


class OpenAICompatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = httpx.URL(self.base_url).host
        self.transport = transport

    def _post(self, body: dict, timeout: float) -> httpx.Response:
        try:
            with httpx.Client(transport=self.transport, timeout=timeout) as http:
                return http.post(
                    f"{self.base_url}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.TimeoutException as exc:
            raise LLMTimeout("timeout") from exc
        except httpx.HTTPError as exc:
            raise LLMError(type(exc).__name__) from exc

    def complete(self, system_prompt: str, payload: dict, *, timeout: float) -> LLMResponse:
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "item_parse", "strict": True, "schema": RESULT_SCHEMA},
            },
        }
        resp = self._post(body, timeout)
        if 400 <= resp.status_code < 500:
            # провайдер не принял response_format: повтор без него, результат всё равно проверяется
            body.pop("response_format")
            resp = self._post(body, timeout)
        if resp.status_code >= 400:
            raise LLMError(f"http {resp.status_code}")
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("bad response") from exc
        if not isinstance(content, str):
            raise LLMError("bad response")
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        return LLMResponse(content, usage.get("prompt_tokens"), usage.get("completion_tokens"))


class StubLLMClient:
    provider = "stub"
    model = "stub"

    def complete(self, system_prompt: str, payload: dict, *, timeout: float) -> LLMResponse:
        content = {
            "what": None,
            "assignee_id": None,
            "due_date": None,
            "due_text": None,
            "done_criteria": None,
            "vague_action": False,
            "question": None,
        }
        return LLMResponse(json.dumps(content), None, None)


def get_llm_client() -> LLMClient:
    settings = get_settings()
    if not settings.llm_api_key:
        return StubLLMClient()
    if not settings.llm_model.strip():
        logger.warning("LLM_API_KEY задан, но LLM_MODEL пуст: используется заглушка")
        return StubLLMClient()
    return OpenAICompatClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
