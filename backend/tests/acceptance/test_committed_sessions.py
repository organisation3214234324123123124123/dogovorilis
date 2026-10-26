"""Проверки, которым нужны настоящие коммиты и отдельные сессии.

Изоляция откатом здесь не годится: флаг чистки в триггере item_events действует до конца
транзакции, а гонку нумерации можно воспроизвести только двумя соединениями. Данные
создаются коммитом и удаляются в конце теста (каскадом от пользователя).
"""

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, get_engine
from app.models import Person
from app.modules.series import service as series_service
from tests.factories import (
    make_event,
    make_item,
    make_meeting,
    make_organization,
    make_person,
    make_series,
    make_user,
)

pytestmark = pytest.mark.fr("FR-52")


@pytest.fixture
def committed_owner(migrated_db: None) -> Iterator[dict]:
    with SessionLocal(bind=get_engine()) as s:
        user = make_user(s)
        org = make_organization(s, user, is_own=True)
        persons = [make_person(s, user, org), make_person(s, user, org)]
        s.commit()
        data = {"user_id": user.id, "person_ids": [p.id for p in persons]}
    yield data
    with SessionLocal(bind=get_engine()) as s:
        s.execute(text("DELETE FROM users WHERE id = :id"), {"id": data["user_id"]})
        s.commit()


def test_concurrent_anonymization_of_one_host_gives_distinct_numbers(
    committed_owner: dict,
) -> None:
    a_id, b_id = committed_owner["person_ids"]
    first = SessionLocal(bind=get_engine())
    second = SessionLocal(bind=get_engine())
    errors: list[BaseException] = []
    try:
        series_service.anonymize_person(first, a_id)  # держит блокировку нумерации

        def other() -> None:
            try:
                series_service.anonymize_person(second, b_id)
                second.commit()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=other)
        thread.start()
        time.sleep(1.0)  # вторая сессия должна ждать, а не взять тот же номер
        assert thread.is_alive(), "вторая сессия не дождалась блокировки нумерации"
        first.commit()
        thread.join(timeout=15)
        assert not thread.is_alive()
        assert not errors, errors
    finally:
        first.close()
        second.close()
    with SessionLocal(bind=get_engine()) as s:
        names = {p.full_name for p in s.scalars(select(Person).where(Person.id.in_([a_id, b_id])))}
    assert names == {"Участник 1", "Участник 2"}


def test_direct_delete_of_item_events_still_refused_after_cleanup_task(migrated_db: None) -> None:
    from app.modules.analytics.tasks import cleanup_retention

    with SessionLocal(bind=get_engine()) as s:
        user = make_user(s)
        series = make_series(s, user)
        item = make_item(s, make_meeting(s, series))
        old = make_event(s, item, created_at=datetime.now(UTC) - timedelta(days=400))
        fresh = make_event(s, item)
        user_id, old_id, fresh_id = user.id, old.id, fresh.id
        s.commit()
    try:
        cleanup_retention()
        with SessionLocal(bind=get_engine()) as s:
            left = {r[0] for r in s.execute(text("SELECT id FROM item_events"))}
            assert old_id not in left
            assert fresh_id in left
            with pytest.raises(DBAPIError, match="append-only"):
                s.execute(text("DELETE FROM item_events WHERE id = :id"), {"id": fresh_id})
    finally:
        with SessionLocal(bind=get_engine()) as s:
            s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            s.commit()
