"""FR-20: пробелы по правилам (find_gaps, default_question) и тот же расчёт через
POST /meetings/{id}/items и PATCH /items/{id}. Правила - чистые функции, без БД, сети и LLM."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.models import Gap, Item
from app.modules.gaps.models import GapType
from tests.acceptance.helpers_items import (
    API,
    TODAY,
    clock,  # noqa: F401
    events,
    gap_rows,
    if_match,
    make_ctx,
    open_gap_types,
)
from tests.factories import World, make_gap

pytestmark = [
    pytest.mark.fr("FR-20"),
    pytest.mark.usefixtures("clock"),
]

WHO = "00000000-0000-0000-0000-00000000000c"
YESTERDAY = TODAY - timedelta(days=1)


def fake(what="Подготовить отчёт", assignee=WHO, due_date=TODAY, due_text=None):
    return SimpleNamespace(
        what=what, assignee_person_id=assignee, due_date=due_date, due_text=due_text
    )


def gaps_of(item, today: date = TODAY) -> list[GapType]:
    from app.modules.gaps.rules import find_gaps

    return find_gaps(item, today)


def test_complete_item_has_no_gaps() -> None:
    assert gaps_of(fake()) == []


def test_item_without_assignee_due_and_with_process_verb() -> None:
    item = fake(what="Разобраться с выгрузкой", assignee=None, due_date=None)
    assert gaps_of(item) == [GapType.NO_ASSIGNEE, GapType.NO_DUE, GapType.VAGUE_ACTION]


def test_no_assignee() -> None:
    assert gaps_of(fake(assignee=None)) == [GapType.NO_ASSIGNEE]


def test_no_due_when_neither_date_nor_text() -> None:
    assert gaps_of(fake(due_date=None, due_text=None)) == [GapType.NO_DUE]


def test_empty_due_text_counts_as_missing() -> None:
    assert gaps_of(fake(due_date=None, due_text="")) == [GapType.NO_DUE]


@pytest.mark.parametrize("phrase", ["на днях", "как можно скорее", "потом"])
def test_due_vague_when_text_without_date(phrase: str) -> None:
    assert gaps_of(fake(due_date=None, due_text=phrase)) == [GapType.DUE_VAGUE]


def test_due_text_with_date_is_not_vague() -> None:
    assert gaps_of(fake(due_date=TODAY + timedelta(days=1), due_text="к пятнице")) == []


def test_due_past_when_date_before_today() -> None:
    assert gaps_of(fake(due_date=YESTERDAY)) == [GapType.DUE_PAST]


def test_due_today_is_not_past() -> None:
    assert gaps_of(fake(due_date=TODAY)) == []


def test_due_past_uses_given_today() -> None:
    item = fake(due_date=date(2026, 10, 16))
    assert gaps_of(item, today=date(2026, 10, 17)) == [GapType.DUE_PAST]
    assert gaps_of(item, today=date(2026, 10, 16)) == []


def test_past_date_with_text_is_only_due_past() -> None:
    assert gaps_of(fake(due_date=YESTERDAY, due_text="к 14.10")) == [GapType.DUE_PAST]


@pytest.mark.parametrize(
    "what",
    [
        "Разобраться с выгрузкой",
        "Проработать схему оплаты",
        "Посмотреть логи",
        "Подумать над тарифами",
        "разобраться с выгрузкой",
    ],
)
def test_vague_action_for_process_verbs(what: str) -> None:
    assert gaps_of(fake(what=what)) == [GapType.VAGUE_ACTION]


@pytest.mark.parametrize(
    "what",
    ["Выгрузить отчёт по оттоку", "Подготовить договор", "Отправить письмо клиенту"],
)
def test_result_verbs_are_not_vague(what: str) -> None:
    assert gaps_of(fake(what=what)) == []


def test_gap_types_have_no_duplicates() -> None:
    result = gaps_of(fake(what="Подумать", assignee=None, due_date=None))
    assert len(result) == len(set(result))


@pytest.mark.parametrize(
    ("gap_type", "kwargs", "expected"),
    [
        (GapType.NO_ASSIGNEE, {}, "Кто берёт это на себя?"),
        (GapType.NO_DUE, {"name": "Иван"}, "Иван, к какому числу?"),
        (GapType.NO_DUE, {}, "К какому числу?"),
        (GapType.DUE_VAGUE, {"due_text": "на днях"}, "«На днях» — это к какому числу?"),
        (GapType.DUE_PAST, {}, "Этот срок уже прошёл — какой новый?"),
        (GapType.VAGUE_ACTION, {}, "Что будет результатом?"),
    ],
)
def test_default_questions(gap_type: GapType, kwargs: dict, expected: str) -> None:
    from app.modules.gaps.rules import default_question

    assert default_question(gap_type, **kwargs) == expected


@pytest.mark.parametrize("gap_type", list(GapType))
def test_default_question_is_at_most_120_chars(gap_type: GapType) -> None:
    from app.modules.gaps.rules import default_question

    question = default_question(gap_type, name="Н" * 150, due_text="т" * 100)
    assert 0 < len(question) <= 120


def post(client: TestClient, world: World, text: str) -> dict:
    resp = client.post(f"{API}/meetings/{world.meeting.id}/items", json={"raw_text": text})
    assert resp.status_code == 201
    return resp.json()


def patch(client: TestClient, item, body: dict):
    return client.patch(f"{API}/items/{item.id}", json=body, headers=if_match(item))


def test_api_new_line_opens_rule_gaps(authed: TestClient, world: World) -> None:
    make_ctx(world)
    body = post(authed, world, "Разобраться с выгрузкой")
    assert {g["type"] for g in body["gaps"]} == {"no_assignee", "no_due", "vague_action"}
    assert {g["source"] for g in body["gaps"]} == {"rule"}


def test_api_vague_due_text_gives_due_vague(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id, due_text="на днях")
    resp = patch(authed, item, {"done_criteria": "Файл в папке"})
    assert {g["type"] for g in resp.json()["gaps"]} == {"due_vague"}


def test_api_past_due_gives_due_past(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id, due_date=YESTERDAY)
    resp = patch(authed, item, {"done_criteria": "Файл в папке"})
    assert {g["type"] for g in resp.json()["gaps"]} == {"due_past"}


def test_api_setting_date_resolves_no_due(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id)
    gap = make_gap(world.db, item, type=GapType.NO_DUE)
    resp = patch(authed, item, {"due_date": "2026-10-30"})
    assert "no_due" not in {g["type"] for g in resp.json()["gaps"]}
    world.db.expire_all()
    assert world.db.get(Gap, gap.id).resolved_at is not None
    assert authed.get(f"{API}/items/{item.id}").json()["gaps"] == []


def test_api_condition_true_again_opens_new_gap(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id)
    old = make_gap(world.db, item, type=GapType.NO_DUE)
    first = patch(authed, item, {"due_date": "2026-10-30"})
    item.version = first.json()["version"]
    world.db.expire_all()
    world.db.get(type(item), item.id).due_date = None
    world.db.flush()
    patch(authed, item, {"done_criteria": "Файл в папке"})
    rows = gap_rows(world.db, item.id, GapType.NO_DUE)
    assert len(rows) == 2
    assert [r.resolved_at is None for r in rows] == [False, True]
    assert rows[0].id == old.id


def test_api_gap_detected_and_resolved_events(authed: TestClient, world: World) -> None:
    make_ctx(world)
    body = post(authed, world, "Подготовить отчёт")
    detected = events(world.db, "gap_detected", item_id=body["id"])
    assert {e.props["type"] for e in detected} >= {"no_assignee", "no_due"}
    assert {e.props["source"] for e in detected} == {"rule"}
    item = world.db.get(Item, body["id"])
    patch(authed, item, {"due_date": "2026-10-30"})
    resolved = events(world.db, "gap_resolved", item_id=body["id"])
    [due] = [e for e in resolved if e.props["type"] == "no_due"]
    assert due.props["seconds_since_detected"] >= 0


def test_api_every_question_is_at_most_120_chars(authed: TestClient, world: World) -> None:
    make_ctx(world)
    body = post(authed, world, "Подумать над выгрузкой на днях")
    assert body["gaps"]
    assert all(g["question"] and len(g["question"]) <= 120 for g in body["gaps"])


def test_api_every_edit_recalculates(authed: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = world.item(what="Подготовить отчёт", assignee_person_id=ctx.ivan.id)
    first = patch(authed, item, {"what": "Посмотреть отчёт"})
    assert "vague_action" in {g["type"] for g in first.json()["gaps"]}
    item.version = first.json()["version"]
    second = patch(authed, item, {"what": "Выгрузить отчёт"})
    assert "vague_action" not in {g["type"] for g in second.json()["gaps"]}
    assert open_gap_types(world.db, item.id) == {"no_due"}


def test_no_assignee_not_raised_when_assignee_set_other_gaps_unaffected() -> None:
    item = fake(assignee=None, due_date=None, due_text="потом")
    assert gaps_of(item) == [GapType.NO_ASSIGNEE, GapType.DUE_VAGUE]
    item.assignee_person_id = WHO
    assert gaps_of(item) == [GapType.DUE_VAGUE]


def test_api_no_assignee_opens_with_question_and_resolves_on_assign(
    authed: TestClient, world: World
) -> None:
    ctx = make_ctx(world)
    body = post(authed, world, "Подготовить отчёт к 30.10")
    [gap] = [g for g in body["gaps"] if g["type"] == "no_assignee"]
    assert gap["source"] == "rule"
    assert gap["question"] == "Кто берёт это на себя?"
    item = world.db.get(Item, body["id"])
    item.version = body["version"]
    resp = patch(authed, item, {"assignee_person_id": str(ctx.ivan.id)})
    assert resp.status_code == 200
    assert "no_assignee" not in {g["type"] for g in resp.json()["gaps"]}
    rows = gap_rows(world.db, item.id, GapType.NO_ASSIGNEE)
    assert len(rows) == 1 and rows[0].resolved_at is not None
