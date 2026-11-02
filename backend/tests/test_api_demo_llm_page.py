"""Тесты публичной страницы «Как отвечает нейросеть» и ссылки на неё в Swagger.

Кэш ответов примеров лежит в памяти процесса (`app.modules.demo.service.reset_example_cache()`).
Ручки без входа: GET /llm/examples, POST /llm/examples/{id}/run, POST /llm/try, GET /llm/page.
Замок и лимит те же, что у /demo/llm/check (`_check_rate`).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.modules.llm.models import LlmCall
from tests.factories import World
from tests.llm_fakes import FakeLLM, reply
from tests.test_api_demo import add_checks, use_llm

pytestmark = pytest.mark.fr("infra")

API = "/api/v1/demo/llm"


@pytest.fixture(autouse=True)
def reset_example_cache() -> None:
    from app.modules.demo import service

    service.reset_example_cache()


@pytest.fixture
def demo_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_DATA", "1")


@pytest.fixture
def anon(demo_on: None, client: TestClient) -> TestClient:
    return client


def test_page_and_examples_absent_without_demo_data(client: TestClient) -> None:
    assert client.get(f"{API}/page").status_code == 404
    assert client.get(f"{API}/examples").status_code == 404


def test_page_is_public_html(anon: TestClient) -> None:
    resp = anon.get(f"{API}/page")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "charset=utf-8" in resp.headers["content-type"].lower()


def test_page_has_title_and_swagger_link(anon: TestClient) -> None:
    body = anon.get(f"{API}/page").text
    assert "Как нейросеть помогает" in body
    assert "/api/docs" in body


def test_page_is_not_in_openapi_paths(anon: TestClient) -> None:
    paths = anon.get("/api/openapi.json").json()["paths"]
    assert f"{API}/page" not in paths


def test_examples_public_with_three_fixed_examples(anon: TestClient) -> None:
    resp = anon.get(f"{API}/examples")
    assert resp.status_code == 200
    examples = resp.json()["examples"]
    assert [e["id"] for e in examples] == [1, 2, 3]
    assert all(e["text"] for e in examples)
    assert all(e["result"] is None and e["saved_at"] is None for e in examples)


def test_run_public_with_fake_llm(anon: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    resp = anon.post(f"{API}/examples/1/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["live"] is True
    assert body["verdict"].startswith("Нейросеть ответила")


def test_examples_return_saved_result_after_run(
    anon: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    run = anon.post(f"{API}/examples/2/run").json()
    examples = {e["id"]: e for e in anon.get(f"{API}/examples").json()["examples"]}
    assert examples[2]["result"]["verdict"] == run["verdict"]
    assert examples[2]["saved_at"] is not None
    assert examples[1]["result"] is None


def test_run_writes_llm_call_without_meeting(
    anon: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    before = len(world.db.scalars(select(LlmCall)).all())
    assert anon.post(f"{API}/examples/3/run").status_code == 200
    calls = world.db.scalars(select(LlmCall)).all()
    assert len(calls) == before + 1
    assert any(c.meeting_id is None and c.provider == "fake" for c in calls)


def test_run_unknown_example_404(anon: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    assert anon.post(f"{API}/examples/1/run").status_code == 200
    resp = anon.post(f"{API}/examples/99/run")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_run_rate_limited_after_20_per_minute(
    anon: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    add_checks(world, 20, 5)
    resp = anon.post(f"{API}/examples/1/run")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


def test_run_busy_lock_returns_429(anon: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import text

    from app.core.db import get_engine
    from app.modules.llm import repository

    use_llm(monkeypatch, FakeLLM(reply()))
    key = repository.CHECK_LOCK_KEY
    with get_engine().connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        try:
            resp = anon.post(f"{API}/examples/1/run")
        finally:
            other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            other.commit()
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "1"


def test_examples_calls_hide_meeting_id_and_row_id(
    anon: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    anon.post(f"{API}/examples/1/run")
    calls = anon.get(f"{API}/examples").json()["calls"]
    assert calls
    for call in calls:
        assert "meeting_id" not in call
        assert "id" not in call
        assert {"task", "provider", "model", "latency_ms", "outcome", "created_at"} <= set(call)


def test_openapi_description_links_to_page_with_demo(anon: TestClient) -> None:
    schema = anon.get("/api/openapi.json").json()
    assert "/api/v1/demo/llm/page" in schema["info"]["description"]


def test_openapi_description_has_no_link_without_demo(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()
    assert "/api/v1/demo/llm/page" not in schema["info"].get("description", "")


def test_try_own_text_public(anon: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    resp = anon.post(f"{API}/try", json={"text": "Дима до пятницы скинет цифры"})
    assert resp.status_code == 200
    assert resp.json()["verdict"].startswith("Нейросеть ответила")


@pytest.mark.parametrize("text", ["", "   ", "x" * 301])
def test_try_rejects_empty_and_long(anon: TestClient, text: str) -> None:
    assert anon.post(f"{API}/try", json={"text": text}).status_code == 422


def test_try_rate_limited(anon: TestClient, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    use_llm(monkeypatch, FakeLLM(reply()))
    add_checks(world, 20, 5)
    assert anon.post(f"{API}/try", json={"text": "Петя подготовит отчёт"}).status_code == 429
