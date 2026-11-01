"""/health называет нейросеть: provider и model берутся из настроек, без ключа или модели - stub."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings

pytestmark = pytest.mark.fr("NFR-11")

HEALTH = "/api/v1/health"


def set_llm(monkeypatch: pytest.MonkeyPatch, key: str, model: str) -> None:
    monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("LLM_MODEL", model)
    get_settings.cache_clear()


def test_health_names_real_provider_and_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_llm(monkeypatch, "k" * 20, "qwen/qwen3.8-27b")
    llm = client.get(HEALTH).json()["llm"]
    assert llm["available"] is True
    assert llm["provider"] == "api.groq.com"
    assert llm["model"] == "qwen/qwen3.8-27b"


def test_health_key_without_model_is_stub(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_llm(monkeypatch, "k" * 20, " ")
    llm = client.get(HEALTH).json()["llm"]
    assert (llm["available"], llm["provider"], llm["model"]) == (False, "stub", "stub")


def test_health_without_key_is_stub(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    set_llm(monkeypatch, "", "qwen/qwen3.8-27b")
    llm = client.get(HEALTH).json()["llm"]
    assert (llm["available"], llm["provider"], llm["model"]) == (False, "stub", "stub")
