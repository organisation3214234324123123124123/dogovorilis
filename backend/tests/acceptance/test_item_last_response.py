# ruff: noqa: F811
"""Карточка пункта: last_response {kind: accept|reschedule|not_mine|done, comment}."""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.items.models import CommentKind, ItemComment, ItemStatus
from tests.acceptance.helpers_mailing import (  # noqa: F401
    Scenario,
    frozen,
    host,
    mailbox,
    sc,
    send,
)
from tests.acceptance.test_fr_31_link_actions import NEW_DUE, Page, ivan  # noqa: F401
from tests.factories import World

pytestmark = pytest.mark.fr("FR-45")


def last_response(page: Page) -> dict | None:
    resp = page.client.get(f"/api/v1/items/{page.item.id}")
    assert resp.status_code == 200
    return resp.json()["last_response"]


def test_not_mine_comment_is_in_last_response_comment(ivan: Page) -> None:
    assert ivan.act("decline", {"comment": "Это к Анне"}).status_code == 200
    last = last_response(ivan)
    assert last["kind"] == "not_mine"
    assert last["comment"] == "Это к Анне"


def test_reschedule_comment_is_in_last_response_comment(ivan: Page) -> None:
    body = {"due_date": NEW_DUE.isoformat(), "comment": "Жду данные"}
    assert ivan.act("reschedule", body).status_code == 200
    last = last_response(ivan)
    assert last["kind"] == "reschedule"
    assert last["comment"] == "Жду данные"
    assert last["due_date"] == NEW_DUE.isoformat()


def test_accept_is_last_response_kind_accept(ivan: Page) -> None:
    assert ivan.act("accept").status_code == 200
    last = last_response(ivan)
    assert last["kind"] == "accept"
    assert last["at"]


def test_done_is_last_response_kind_done(ivan: Page) -> None:
    assert ivan.act("accept").status_code == 200
    assert ivan.act("done").status_code == 200
    assert last_response(ivan)["kind"] == "done"


def test_last_response_is_the_latest_answer(ivan: Page) -> None:
    assert ivan.act("accept").status_code == 200
    assert ivan.act("done").status_code == 200
    assert last_response(ivan)["kind"] == "done"


def test_last_response_is_null_before_any_answer(ivan: Page) -> None:
    assert last_response(ivan) is None


def test_seeded_comment_is_exposed_as_comment(authed, world: World) -> None:
    item = world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.PENDING,
        sent_at=datetime(2026, 10, 14, tzinfo=UTC),
    )
    world.db.add(
        ItemComment(
            item_id=item.id,
            author_person_id=world.person.id,
            kind=CommentKind.NOT_MINE,
            text="Это к Анне",
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    world.db.flush()
    last = authed.get(f"/api/v1/items/{item.id}").json()["last_response"]
    assert last["kind"] == "not_mine"
    assert last["comment"] == "Это к Анне"
