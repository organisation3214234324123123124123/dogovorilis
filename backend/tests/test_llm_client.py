import json

import httpx
import pytest

from app.core.config import get_settings

pytestmark = pytest.mark.fr("FR-12")

PAYLOAD = {
    "text": "Иван разберётся с выгрузкой к пятнице",
    "today": "2026-10-15",
    "weekday": "четверг",
    "participants": [{"id": "p_12", "name": "Иван"}],
}
CONTENT = '{"what": "Разобраться", "vague_action": false}'


def make_client(handler, **kw):
    from app.modules.llm.client import OpenAICompatClient

    return OpenAICompatClient(
        base_url="https://llm.example.test/v1",
        api_key="secret-key",
        model="yandexgpt-lite",
        transport=httpx.MockTransport(handler),
        **kw,
    )


def ok_handler(captured: list):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": CONTENT}}],
                "usage": {"prompt_tokens": 111, "completion_tokens": 22},
            },
        )

    return handler


def test_request_goes_to_chat_completions_with_bearer() -> None:
    sent: list = []
    make_client(ok_handler(sent)).complete("system", PAYLOAD, timeout=8)
    assert str(sent[0].url) == "https://llm.example.test/v1/chat/completions"
    assert sent[0].headers["authorization"] == "Bearer secret-key"


def test_request_body_has_model_zero_temperature_and_schema() -> None:
    sent: list = []
    make_client(ok_handler(sent)).complete("system", PAYLOAD, timeout=8)
    body = json.loads(sent[0].content)
    assert body["model"] == "yandexgpt-lite"
    assert body["temperature"] == 0
    assert body["response_format"]["type"] == "json_schema"


def test_system_prompt_comes_from_argument_and_user_message_is_payload_only() -> None:
    sent: list = []
    make_client(ok_handler(sent)).complete("PROMPT-FROM-DB", PAYLOAD, timeout=8)
    messages = json.loads(sent[0].content)["messages"]
    assert messages[0] == {"role": "system", "content": "PROMPT-FROM-DB"}
    assert json.loads(messages[1]["content"]) == PAYLOAD


def test_response_content_and_usage_are_parsed() -> None:
    resp = make_client(ok_handler([])).complete("system", PAYLOAD, timeout=8)
    assert resp.content == CONTENT
    assert (resp.input_tokens, resp.output_tokens) == (111, 22)


def test_missing_usage_gives_none_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": CONTENT}}]})

    resp = make_client(handler).complete("system", PAYLOAD, timeout=8)
    assert (resp.input_tokens, resp.output_tokens) == (None, None)


def test_transport_timeout_becomes_llm_timeout() -> None:
    from app.modules.llm.client import LLMTimeout

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMTimeout):
        make_client(handler).complete("system", PAYLOAD, timeout=8)


@pytest.mark.parametrize("status", [500, 503])
def test_http_error_becomes_llm_error(status: int) -> None:
    from app.modules.llm.client import LLMError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "x"})

    with pytest.raises(LLMError):
        make_client(handler).complete("system", PAYLOAD, timeout=8)


def test_connection_failure_becomes_llm_error() -> None:
    from app.modules.llm.client import LLMError

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(LLMError):
        make_client(handler).complete("system", PAYLOAD, timeout=8)


def test_response_without_choices_becomes_llm_error() -> None:
    from app.modules.llm.client import LLMError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(LLMError):
        make_client(handler).complete("system", PAYLOAD, timeout=8)


def test_empty_key_gives_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.llm.client import StubLLMClient, get_llm_client

    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    assert isinstance(get_llm_client(), StubLLMClient)


def test_filled_key_gives_real_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.llm.client import OpenAICompatClient, get_llm_client

    monkeypatch.setenv("LLM_API_KEY", "k" * 20)
    monkeypatch.setenv("LLM_MODEL", "m")
    get_settings.cache_clear()
    assert isinstance(get_llm_client(), OpenAICompatClient)


def test_stub_returns_parsable_json_without_network() -> None:
    from app.modules.llm.client import StubLLMClient

    resp = StubLLMClient().complete("system", PAYLOAD, timeout=8)
    assert isinstance(json.loads(resp.content), dict)


def format_rejecting_handler(captured: list, status: int = 400):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if "response_format" in json.loads(request.content):
            return httpx.Response(status, json={"error": "response_format not supported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": CONTENT}}]})

    return handler


@pytest.mark.parametrize("status", [400, 422])
def test_4xx_on_response_format_retries_without_it(status: int) -> None:
    seen: list = []
    resp = make_client(format_rejecting_handler(seen, status)).complete("s", PAYLOAD, timeout=8)
    assert resp.content == CONTENT
    assert len(seen) == 2
    assert "response_format" in json.loads(seen[0].content)
    assert "response_format" not in json.loads(seen[1].content)


def test_fallback_request_keeps_prompt_and_payload() -> None:
    seen: list = []
    make_client(format_rejecting_handler(seen)).complete("system", PAYLOAD, timeout=8)
    first, second = (json.loads(r.content) for r in seen)
    assert second["messages"] == first["messages"]
    assert second["model"] == first["model"]


def test_second_4xx_is_llm_error_after_exactly_two_requests() -> None:
    from app.modules.llm.client import LLMError

    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json={})

    with pytest.raises(LLMError):
        make_client(handler).complete("s", PAYLOAD, timeout=8)
    assert len(seen) <= 2


@pytest.mark.parametrize("status", [500, 503])
def test_other_statuses_are_not_retried(status: int) -> None:
    from app.modules.llm.client import LLMError

    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={})

    with pytest.raises(LLMError):
        make_client(handler).complete("s", PAYLOAD, timeout=8)
    assert len(seen) == 1


def test_empty_model_with_key_gives_stub_and_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.modules.llm.client import StubLLMClient, get_llm_client

    monkeypatch.setenv("LLM_API_KEY", "k" * 20)
    monkeypatch.setenv("LLM_MODEL", "")
    get_settings.cache_clear()
    with caplog.at_level("WARNING"):
        client = get_llm_client()
    assert isinstance(client, StubLLMClient)
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_blank_model_with_key_gives_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.llm.client import StubLLMClient, get_llm_client

    monkeypatch.setenv("LLM_API_KEY", "k" * 20)
    monkeypatch.setenv("LLM_MODEL", "   ")
    get_settings.cache_clear()
    assert isinstance(get_llm_client(), StubLLMClient)
