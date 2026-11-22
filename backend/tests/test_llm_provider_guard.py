import pytest
import uuid
from sqlalchemy import func, select

from app.core.config import get_settings
from app.modules.llm.client import OpenAICompatClient, StubLLMClient, get_llm_client
from app.modules.llm.models import LlmCall
from app.modules.series.models import MeetingStatus

pytestmark = pytest.mark.fr("FR-12")


def production_llm(monkeypatch: pytest.MonkeyPatch, confirmed: str | None) -> None:
    monkeypatch.setenv("DEV_MODE", "0")
    monkeypatch.setenv("SECRET_KEY", "s" * 64)
    monkeypatch.setenv("LLM_API_KEY", "k" * 20)
    monkeypatch.setenv("LLM_MODEL", "model-1")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.example/v1")
    if confirmed is None:
        monkeypatch.delenv("LLM_DPA_CONFIRMED", raising=False)
    else:
        monkeypatch.setenv("LLM_DPA_CONFIRMED", confirmed)
    get_settings.cache_clear()


def test_production_without_confirmation_uses_stub_and_health(
    client,
    world,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.modules.health import heartbeat
    from app.modules.items import service as items_service, tasks
    from tests.conftest import login_as

    production_llm(monkeypatch, None)
    monkeypatch.setattr(heartbeat, "last_run", lambda: None)
    world.meeting.status = MeetingStatus.LIVE
    login_as(client, world.user)
    queued: list[tuple] = []
    monkeypatch.setattr(tasks.enrich_item, "delay", lambda *args: queued.append(args))
    monkeypatch.setattr(
        OpenAICompatClient,
        "_post",
        lambda *args, **kwargs: pytest.fail("HTTP request should not be sent"),
    )
    with caplog.at_level("WARNING"):
        provider = get_llm_client()
        response = client.post(
            f"/api/v1/meetings/{world.meeting.id}/items", json={"raw_text": "строка теста"}
        )
        result = items_service.enrich_item_background(uuid.UUID(response.json()["id"]))
    health = client.get("/api/v1/health").json()["llm"]
    assert isinstance(provider, StubLLMClient)
    assert response.status_code == 201
    assert result is False
    assert len(queued) == 1 and len(queued[0]) == 1
    assert world.db.scalar(select(func.count()).select_from(LlmCall)) == 0
    assert (health["available"], health["provider"], health["model"]) == (
        False,
        "stub",
        "stub",
    )
    assert "договор" in caplog.text.lower()
    assert "k" * 20 not in caplog.text


def test_blank_confirmation_uses_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    production_llm(monkeypatch, "")
    assert isinstance(get_llm_client(), StubLLMClient)


@pytest.mark.parametrize("mode,confirmed", [("0", "1"), ("1", None)])
def test_confirmation_or_dev_mode_enables_provider(
    client,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    confirmed: str | None,
) -> None:
    from app.modules.health import heartbeat

    production_llm(monkeypatch, confirmed)
    monkeypatch.setenv("DEV_MODE", mode)
    get_settings.cache_clear()
    assert isinstance(get_llm_client(), OpenAICompatClient)
    monkeypatch.setattr(heartbeat, "last_run", lambda: None)
    health = client.get("/api/v1/health").json()["llm"]
    assert health["available"] is True
    assert health["provider"] == "provider.example"
