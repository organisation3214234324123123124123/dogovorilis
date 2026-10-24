"""Общие помощники приёмочных тестов пунктов живой встречи (FR-10...FR-26).

"Сегодня" = четверг 2026-10-15 (FR-11); время замораживается фикстурой `clock`."""

import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AnalyticsEvent, Gap, MeetingParticipant, Notification, SeriesMember
from app.modules.series.models import MeetingStatus
from tests.factories import World, make_organization, make_person, make_series, make_user

API = "/api/v1"
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
TODAY = date(2026, 10, 15)


@pytest.fixture
def clock() -> Iterator[None]:
    with freeze_time(NOW, tick=True):
        yield


@dataclass
class Ctx:
    """Живая встреча ведущего: в составе серии Иван, Анна и базовый `world.person`."""

    world: World
    ivan: Any
    anna: Any

    @property
    def db(self) -> Session:
        return self.world.db

    @property
    def meeting(self) -> Any:
        return self.world.meeting


def add_member(world: World, person: Any, *, present: bool = True) -> None:
    world.db.add(SeriesMember(series_id=world.series.id, person_id=person.id))
    world.db.add(
        MeetingParticipant(meeting_id=world.meeting.id, person_id=person.id, present=present)
    )
    world.db.flush()


def make_ctx(world: World, status: MeetingStatus = MeetingStatus.LIVE) -> Ctx:
    world.meeting.status = status
    ivan = make_person(world.db, world.user, world.org, full_name="Иван Петров")
    anna = make_person(world.db, world.user, world.org, full_name="Анна Смирнова")
    for person in (world.person, ivan, anna):
        add_member(world, person)
    return Ctx(world, ivan, anna)


def outsider(world: World, name: str = "Пётр Сидоров") -> Any:
    """Человек ведущего, но не из состава серии."""
    return make_person(world.db, world.user, world.org, full_name=name)


def foreign_host(world: World) -> Any:
    """Другой ведущий с серией, встречей и составом."""
    user = make_user(world.db)
    org = make_organization(world.db, user, is_own=True)
    person = make_person(world.db, user, org)
    series = make_series(world.db, user)
    return user, org, person, series


def if_match(item: Any) -> dict[str, str]:
    return {"If-Match": str(item.version)}


def problem_code(resp: Any) -> str:
    return resp.json()["code"]


def events(db: Session, name: str, **filters: Any) -> list[AnalyticsEvent]:
    stmt = select(AnalyticsEvent).where(AnalyticsEvent.name == name)
    for key, value in filters.items():
        stmt = stmt.where(getattr(AnalyticsEvent, key) == value)
    return list(db.scalars(stmt.order_by(AnalyticsEvent.id)))


def gap_rows(db: Session, item_id: Any, gap_type: Any = None) -> list[Gap]:
    db.expire_all()
    stmt = select(Gap).where(Gap.item_id == item_id)
    if gap_type is not None:
        stmt = stmt.where(Gap.type == gap_type)
    return list(db.scalars(stmt.order_by(Gap.detected_at)))


def open_gap_types(db: Session, item_id: Any) -> set[str]:
    return {
        str(g.type)
        for g in gap_rows(db, item_id)
        if g.resolved_at is None and g.dismissed_at is None
    }


def notifications(db: Session, ntype: Any, item_id: Any) -> list[Notification]:
    db.expire_all()
    stmt = select(Notification).where(Notification.type == ntype, Notification.item_id == item_id)
    return list(db.scalars(stmt.order_by(Notification.created_at)))


def frame_name(frame: Any) -> str:
    if isinstance(frame, dict):
        for key in ("type", "event", "name"):
            if isinstance(frame.get(key), str):
                return frame[key]
    return ""


def ws_events(
    client: TestClient,
    meeting_id: Any,
    act: Callable[[], Any],
    want: tuple[str, ...],
    timeout: float = 3.0,
) -> list[dict]:
    """Открывает WS-соединение панели, выполняет `act` и собирает кадры до `want` или таймаута.

    Формат кадра: JSON-объект, имя события в поле `type` (допустимо `event` или `name`).
    """
    got: list[dict] = []

    def reader(ws: Any) -> None:
        try:
            while True:
                got.append(json.loads(ws.receive_text()))
        except Exception:
            return

    with client.websocket_connect(f"/ws/meetings/{meeting_id}") as ws:
        thread = threading.Thread(target=reader, args=(ws,), daemon=True)
        thread.start()
        act()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(w in {frame_name(f) for f in list(got)} for w in want):
                break
            if not thread.is_alive():
                break
            time.sleep(0.05)
    return got
