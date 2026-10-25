import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.modules.llm.models import LlmPrompt, LlmTask
from tests.llm_helpers import activate_prompt

pytestmark = pytest.mark.fr("FR-12")

TOKEN = "t" * 32
URL = "/api/v1/admin/llm/prompts"
H = {"X-Admin-Token": TOKEN}


@pytest.fixture(autouse=True)
def admin_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    get_settings.cache_clear()


def rows(db, task: str = "parse") -> list[LlmPrompt]:
    return list(
        db.scalars(
            select(LlmPrompt).where(LlmPrompt.task == LlmTask(task)).order_by(LlmPrompt.version)
        )
    )


def post(client: TestClient, **body):
    return client.post(URL, json={"task": "parse", "text": "new text", **body}, headers=H)


def test_post_without_token_is_forbidden(client: TestClient) -> None:
    resp = client.post(URL, json={"task": "parse", "text": "x"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "ADMIN_FORBIDDEN"


def test_post_with_wrong_token_is_forbidden(client: TestClient) -> None:
    resp = client.post(
        URL, json={"task": "parse", "text": "x"}, headers={"X-Admin-Token": "x" * 32}
    )
    assert resp.status_code == 403


def test_post_with_host_session_is_forbidden(authed: TestClient) -> None:
    assert authed.post(URL, json={"task": "parse", "text": "x"}).status_code == 403


def test_post_disabled_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "")
    get_settings.cache_clear()
    resp = client.post(URL, json={"task": "parse", "text": "x"}, headers=H)
    assert resp.status_code == 403
    assert resp.json()["code"] == "ADMIN_DISABLED"


def test_post_without_text_is_422(client: TestClient) -> None:
    assert client.post(URL, json={"task": "parse"}, headers=H).status_code == 422


def test_post_with_empty_text_is_422(client: TestClient) -> None:
    assert post(client, text="").status_code == 422


def test_post_with_unknown_task_is_422(client: TestClient) -> None:
    assert post(client, task="translate").status_code == 422


def test_post_creates_version_and_returns_201(client: TestClient, db) -> None:
    resp = post(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["task"] == "parse"
    assert body["text"] == "new text"
    assert body["is_active"] is True
    assert body["version"] == len(rows(db))


def test_versions_grow_by_one(client: TestClient, db) -> None:
    first = post(client, text="a").json()["version"]
    second = post(client, text="b").json()["version"]
    assert second == first + 1


def test_new_version_becomes_the_only_active(client: TestClient, db) -> None:
    activate_prompt(db, text="old")
    created = post(client).json()
    active = [p for p in rows(db) if p.is_active]
    assert [str(p.id) for p in active] == [created["id"]]


def test_old_version_is_kept_unchanged(client: TestClient, db) -> None:
    old = activate_prompt(db, text="old text")
    post(client)
    db.refresh(old)
    assert old.text == "old text"
    assert old.is_active is False


def test_activate_false_keeps_previous_active(client: TestClient, db) -> None:
    old = activate_prompt(db, text="old")
    created = post(client, activate=False).json()
    assert created["is_active"] is False
    db.refresh(old)
    assert old.is_active is True


def test_other_task_active_prompt_is_untouched(client: TestClient, db) -> None:
    gaps = activate_prompt(db, LlmTask.GAPS, text="gaps")
    assert post(client).status_code == 201
    db.refresh(gaps)
    assert gaps.is_active is True


def test_versions_are_counted_per_task(client: TestClient, db) -> None:
    activate_prompt(db, LlmTask.GAPS, text="g1")
    activate_prompt(db, LlmTask.GAPS, text="g2")
    before = len(rows(db, "question"))
    resp = post(client, task="question")
    assert resp.json()["version"] == before + 1


def test_get_lists_new_version_first(client: TestClient, db) -> None:
    activate_prompt(db, text="old")
    created = post(client, text="newest").json()
    listed = [p for p in client.get(URL, headers=H).json() if p["task"] == "parse"]
    assert listed[0]["id"] == created["id"]
    assert listed[0]["version"] > listed[1]["version"]


def test_post_writes_exactly_one_row(client: TestClient, db) -> None:
    before = db.scalar(select(func.count()).select_from(LlmPrompt))
    post(client)
    assert db.scalar(select(func.count()).select_from(LlmPrompt)) == before + 1


def test_activation_is_visible_to_service(client: TestClient, db) -> None:
    post(client, text="from admin")
    from app.modules.llm import service

    assert service.get_active_prompt(db, LlmTask.PARSE).text == "from admin"


def test_start_parse_prompt_is_seeded_by_migration(db) -> None:
    active = [p for p in rows(db) if p.is_active]
    assert len(active) == 1
    assert active[0].version == 1


def seeded_text(db) -> str:
    return next(p for p in rows(db) if p.version == 1).text.lower()


def test_seed_prompt_says_text_is_data_not_instructions(db) -> None:
    text = seeded_text(db)
    assert "данные" in text
    assert "не инструкц" in text


def test_seed_prompt_tells_to_ignore_instructions_inside_text(db) -> None:
    text = seeded_text(db)
    assert "игнорируй" in text


def test_seed_prompt_pins_participants_and_today(db) -> None:
    text = seeded_text(db)
    assert "participants" in text
    assert "today" in text
    assert "не меня" in text


def test_concurrent_prompt_creation_has_no_500() -> None:
    """Параллельное создание версии не падает на уникальном индексе (нужен advisory lock)."""
    import threading
    import time

    from sqlalchemy.orm import Session

    from app.core.db import get_engine
    from app.modules.llm import service
    from app.modules.llm.schemas import PromptCreate

    engine = get_engine()
    with Session(engine) as s0:
        base = max(
            v for v in s0.scalars(select(LlmPrompt.version).where(LlmPrompt.task == LlmTask.PARSE))
        )
        active_id = s0.scalar(
            select(LlmPrompt.id).where(LlmPrompt.task == LlmTask.PARSE, LlmPrompt.is_active)
        )
    errors: list[BaseException] = []

    def second() -> None:
        try:
            with Session(engine) as s2:
                service.create_prompt(s2, PromptCreate(task="parse", text="race-B", activate=True))
                s2.commit()
        except BaseException as exc:
            errors.append(exc)

    try:
        with Session(engine) as s1:
            service.create_prompt(s1, PromptCreate(task="parse", text="race-A", activate=True))
            t = threading.Thread(target=second)
            t.start()
            time.sleep(1.0)
            s1.commit()
            t.join(timeout=15)
        assert not errors, errors
    finally:
        with Session(engine) as c:
            c.execute(LlmPrompt.__table__.delete().where(LlmPrompt.version > base))
            c.execute(
                LlmPrompt.__table__.update().where(LlmPrompt.id == active_id).values(is_active=True)
            )
            c.commit()
