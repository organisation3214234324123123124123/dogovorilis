# ruff: noqa: F811
"""На каждое действие пишется ровно одно событие, а в его props нет текста (FR-50, п.3).

Допустимы числа, флаги и короткие служебные значения; названий, имён и e-mail там быть не должно.
"""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from tests.acceptance.helpers_mailing import (  # noqa: F401
    TUESDAY,
    Scenario,
    events,
    frozen,
    host,
    mailbox,
    mails,
    sc,
    send,
)
from tests.acceptance.test_fr_22_23_closure import go_live, open_closure
from tests.acceptance.test_fr_28_send import summary_text
from tests.acceptance.test_fr_31_link_actions import Page, ivan  # noqa: F401
from tests.factories import World

pytestmark = pytest.mark.fr("FR-50")

COMMENT = "Это не моё, секретный комментарий исполнителя"
SERVICE_VALUE = re.compile(r"[a-z][a-z_]{0,31}|[A-Z][A-Z_]{0,31}")


def personal_strings(sc: Scenario) -> list[str]:
    people = (sc.ivan, sc.anna, sc.oleg, sc.world.user)
    items = (sc.pending, sc.agreed, sc.unassigned)
    return [
        *(p.full_name for p in people if getattr(p, "full_name", None)),
        *(p.email for p in people if getattr(p, "email", None)),
        *(i.what for i in items),
        sc.world.series.title,
        COMMENT,
    ]


def assert_props_are_plain(props: dict, forbidden: list[str]) -> None:
    for key, value in props.items():
        if isinstance(value, str):
            assert SERVICE_VALUE.fullmatch(value), f"props.{key} похоже на текст: {value!r}"
        else:
            assert value is None or isinstance(value, bool | int | float), f"props.{key}: {value!r}"
    dump = json.dumps(props, ensure_ascii=False)
    for text in forbidden:
        assert text not in dump


def test_closure_opened_has_no_text_in_props(host, world: World) -> None:
    go_live(world)
    open_closure(host, world)
    (event,) = events(world.db, "closure_opened", meeting_id=world.meeting.id)
    assert_props_are_plain(event.props, [world.series.title, world.user.email])


def test_closure_opened_by_timer_has_no_text_in_props(world: World, frozen) -> None:
    from app.modules.series.tasks import open_due_closures
    from tests.acceptance.test_fr_22_23_closure import END, set_end

    go_live(world)
    set_end(world, END)
    open_due_closures(world.db, END - timedelta(minutes=5))
    (event,) = events(world.db, "closure_opened", meeting_id=world.meeting.id)
    assert event.props["trigger"] == "timer"
    assert_props_are_plain(event.props, [world.series.title, world.user.email])


def test_summary_sent_has_no_text_in_props(host, sc: Scenario) -> None:
    send(host, sc.world)
    (event,) = events(sc.world.db, "summary_sent", meeting_id=sc.world.meeting.id)
    assert_props_are_plain(event.props, personal_strings(sc))


def test_summary_sent_is_written_once_for_repeated_send(host, sc: Scenario) -> None:
    send(host, sc.world)
    send(host, sc.world)
    assert len(events(sc.world.db, "summary_sent", meeting_id=sc.world.meeting.id)) == 1


def test_summary_copied_has_no_text_in_props(host, sc: Scenario) -> None:
    summary_text(host, sc.world)
    (event,) = events(sc.world.db, "summary_copied", meeting_id=sc.world.meeting.id)
    assert_props_are_plain(event.props, personal_strings(sc))


def test_link_opened_has_no_text_in_props(ivan: Page) -> None:
    ivan.client.get(f"/api/v1/p/{ivan.token}")
    (event,) = events(ivan.sc.world.db, "link_opened", meeting_id=ivan.sc.world.meeting.id)
    assert_props_are_plain(event.props, [*personal_strings(ivan.sc), ivan.token])


def test_link_answered_has_no_comment_or_text_in_props(ivan: Page) -> None:
    resp = ivan.act("decline", {"comment": COMMENT})
    assert resp.status_code == 200
    (event,) = events(ivan.sc.world.db, "link_answered", item_id=ivan.item.id)
    assert event.props["answer"] == "decline"
    assert_props_are_plain(event.props, [*personal_strings(ivan.sc), ivan.token])


def test_reminder_sent_has_no_text_in_props(host, sc: Scenario) -> None:
    from app.modules.notifications.sender import send_due

    send(host, sc.world)
    send_due(sc.world.db, TUESDAY + timedelta(minutes=5))
    send_due(sc.world.db, datetime(2030, 1, 24, 6, 0, tzinfo=UTC))
    assert mails(sc.world.db, "DUE_TOMORROW", item_id=sc.pending.id)
    (event,) = events(sc.world.db, "reminder_sent", item_id=sc.pending.id)
    assert_props_are_plain(event.props, personal_strings(sc))
