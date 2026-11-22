from datetime import date
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.llm.models import LlmPrompt, LlmTask
from tests.llm_helpers import activate_prompt

pytestmark = pytest.mark.fr("FR-12")

TOKEN = "t" * 32
URL = "/api/v1/admin/prompts"
ACTIVATE_URL = "/api/v1/admin/prompts/{}/activate"
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


def test_post_without_token_is_unauthorized(client: TestClient) -> None:
    resp = client.post(URL, json={"task": "parse", "text": "x"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


def test_post_with_wrong_token_is_unauthorized(client: TestClient) -> None:
    resp = client.post(
        URL, json={"task": "parse", "text": "x"}, headers={"X-Admin-Token": "x" * 32}
    )
    assert resp.status_code == 401


def test_post_with_host_session_is_unauthorized(authed: TestClient) -> None:
    assert authed.post(URL, json={"task": "parse", "text": "x"}).status_code == 401


def test_post_empty_admin_token_is_unauthorized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "")
    get_settings.cache_clear()
    resp = client.post(URL, json={"task": "parse", "text": "x"}, headers=H)
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


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
    assert body["is_active"] is False
    assert body["version"] == len(rows(db))
    assert body["created_by"] is None


def test_versions_grow_by_one(client: TestClient, db) -> None:
    first = post(client, text="a").json()["version"]
    second = post(client, text="b").json()["version"]
    assert second == first + 1


def test_new_version_does_not_replace_active(client: TestClient, db) -> None:
    activate_prompt(db, text="old")
    created = post(client).json()
    active = [p for p in rows(db) if p.is_active]
    assert [p.text for p in active] == ["old"]
    assert created["is_active"] is False
    from app.modules.llm import service
    from app.modules.llm.models import LlmCall
    from tests.llm_fakes import FakeLLM, reply

    service.parse_text(
        db,
        FakeLLM(reply()),
        meeting_id=None,
        text="Подготовить отчёт",
        today=date(2026, 10, 15),
        persons=[],
    )
    call = db.scalar(select(LlmCall).order_by(LlmCall.created_at.desc()))
    assert call.prompt_id == active[0].id


def test_old_version_is_kept_unchanged(client: TestClient, db) -> None:
    old = activate_prompt(db, text="old text")
    post(client)
    db.refresh(old)
    assert old.text == "old text"
    assert old.is_active is True


def test_new_version_keeps_previous_active(client: TestClient, db) -> None:
    old = activate_prompt(db, text="old")
    created = post(client).json()
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
    created = post(client, text="from admin").json()
    response = client.post(ACTIVATE_URL.format(created["id"]), headers=H)
    assert response.status_code == 200
    from app.modules.llm import service
    from app.modules.llm.models import LlmCall
    from tests.llm_fakes import FakeLLM, reply

    active = service.get_active_prompt(db, LlmTask.PARSE)
    assert active.text == "from admin"
    service.parse_text(
        db,
        FakeLLM(reply()),
        meeting_id=None,
        text="Подготовить отчёт",
        today=date(2026, 10, 15),
        persons=[],
    )
    call = db.scalar(select(LlmCall).order_by(LlmCall.created_at.desc()))
    assert call.prompt_id == active.id


def test_activate_is_repeatable_and_unknown_id_is_404(client: TestClient, db) -> None:
    created = post(client).json()
    path = ACTIVATE_URL.format(created["id"])
    assert client.post(path, headers=H).status_code == 200
    repeated = client.post(path, headers=H)
    assert repeated.status_code == 200
    assert repeated.json()["is_active"] is True
    assert client.post(ACTIVATE_URL.format("00000000-0000-0000-0000-000000000000"), headers=H).status_code == 404


def test_activate_requires_admin_token(client: TestClient, db) -> None:
    created = post(client).json()
    assert client.post(ACTIVATE_URL.format(created["id"])).status_code == 401


def test_activate_old_version_rolls_back_prompt_and_keeps_calls(
    client: TestClient, db
) -> None:
    from app.modules.llm import service
    from app.modules.llm.models import LlmCall
    from tests.llm_fakes import FakeLLM, reply

    old = next(prompt for prompt in rows(db) if prompt.is_active)
    newer = post(client, text="version four").json()
    assert client.post(ACTIVATE_URL.format(newer["id"]), headers=H).status_code == 200
    service.parse_text(
        db,
        FakeLLM(reply()),
        meeting_id=None,
        text="Подготовить отчёт",
        today=date(2026, 10, 15),
        persons=[],
    )
    newer_id = uuid.UUID(newer["id"])
    assert db.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.prompt_id == newer_id)) == 1
    assert client.post(ACTIVATE_URL.format(old.id), headers=H).status_code == 200
    service.parse_text(
        db,
        FakeLLM(reply()),
        meeting_id=None,
        text="Подготовить отчёт",
        today=date(2026, 10, 15),
        persons=[],
    )
    assert db.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.prompt_id == old.id)) == 1
    assert db.get(LlmPrompt, newer["id"]) is not None
    assert db.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.prompt_id == newer_id)) == 1


def test_get_filters_task_and_includes_created_by(client: TestClient, db) -> None:
    activate_prompt(db, LlmTask.AGENDA, text="agenda version")
    response = client.get(URL, params={"task": "agenda"}, headers=H)
    assert response.status_code == 200
    assert response.json()
    assert {row["task"] for row in response.json()} == {"agenda"}
    assert all("created_by" in row for row in response.json())


def test_concurrent_prompt_activation_leaves_one_active() -> None:
    import threading

    from app.core.db import get_engine
    from app.modules.llm import service
    from app.modules.llm.schemas import PromptCreate

    engine = get_engine()
    with Session(engine) as db:
        original_id = db.scalar(
            select(LlmPrompt.id).where(LlmPrompt.task == LlmTask.PARSE, LlmPrompt.is_active)
        )
        first = service.create_prompt(db, PromptCreate(task="parse", text="activation A"))
        second = service.create_prompt(db, PromptCreate(task="parse", text="activation B"))
        first_id, second_id = first.id, second.id
        db.commit()

    failures: list[BaseException] = []
    start = threading.Barrier(2)

    def activate(prompt_id) -> None:
        try:
            with Session(engine) as db:
                start.wait(timeout=10)
                service.activate_prompt(db, prompt_id)
                db.commit()
        except BaseException as exc:
            failures.append(exc)

    threads = [
        threading.Thread(target=activate, args=(prompt_id,))
        for prompt_id in (first_id, second_id)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert all(not thread.is_alive() for thread in threads)
        assert not failures, failures
        with Session(engine) as db:
            active = list(
                db.scalars(
                    select(LlmPrompt).where(
                        LlmPrompt.task == LlmTask.PARSE, LlmPrompt.is_active
                    )
                )
            )
            assert len(active) == 1
            assert active[0].id in {first_id, second_id}
    finally:
        with Session(engine) as db:
            db.execute(delete(LlmPrompt).where(LlmPrompt.id.in_([first_id, second_id])))
            db.execute(
                update(LlmPrompt)
                .values(is_active=False)
                .where(LlmPrompt.task == LlmTask.PARSE)
            )
            db.execute(update(LlmPrompt).where(LlmPrompt.id == original_id).values(is_active=True))
            db.commit()


def test_start_parse_prompt_is_seeded_by_migration(db) -> None:
    active = [p for p in rows(db) if p.is_active]
    assert len(active) == 1
    assert any(p.version == 1 for p in rows(db))  # стартовый промпт посеян миграцией
    assert active[0].version == max(p.version for p in rows(db))  # активен последний


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
                service.create_prompt(s2, PromptCreate(task="parse", text="race-B"))
                s2.commit()
        except BaseException as exc:
            errors.append(exc)

    try:
        with Session(engine) as s1:
            service.create_prompt(s1, PromptCreate(task="parse", text="race-A"))
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
