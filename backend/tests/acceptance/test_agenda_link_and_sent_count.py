"""Условная привязка повестки (FR-27) и счёт писем по встречам (BR-11)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.modules.items import service as items_service
from app.modules.items.models import Item
from app.modules.notifications import repository as notif_repo
from app.modules.notifications.models import NotificationStatus, NotificationType
from app.modules.series import repository as series_repo
from app.modules.series.service import apply_agenda_matches
from tests.factories import World, make_agenda_item, make_meeting, make_notification


def _linked(world: World, item: Item) -> uuid.UUID | None:
    world.db.expire(item)
    return item.agenda_item_id


@pytest.mark.fr("FR-27")
def test_repeated_matching_does_not_relink_or_recount(world: World) -> None:
    first, second = (
        make_agenda_item(world.db, world.meeting),
        make_agenda_item(world.db, world.meeting),
    )
    item = world.item(what="Решение")
    assert (
        apply_agenda_matches(
            world.db, world.meeting.id, [{"agenda_item_id": first.id, "item_id": item.id}]
        )
        == 1
    )
    again = [{"agenda_item_id": second.id, "item_id": item.id}]
    assert apply_agenda_matches(world.db, world.meeting.id, again) == 0
    assert _linked(world, item) == first.id


@pytest.mark.fr("FR-27")
def test_same_item_twice_in_one_response_is_linked_once(world: World) -> None:
    first, second = (
        make_agenda_item(world.db, world.meeting),
        make_agenda_item(world.db, world.meeting),
    )
    item = world.item(what="Решение")
    pairs = [
        {"agenda_item_id": first.id, "item_id": item.id},
        {"agenda_item_id": second.id, "item_id": item.id},
    ]
    assert apply_agenda_matches(world.db, world.meeting.id, pairs) == 1
    assert _linked(world, item) == first.id


@pytest.mark.fr("FR-27")
def test_link_set_by_host_during_matching_is_not_overwritten(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Гонка: ведущий привязал пункт после чтения, но до записи ответа LLM."""
    host_topic, llm_topic = (
        make_agenda_item(world.db, world.meeting),
        make_agenda_item(world.db, world.meeting),
    )
    item = world.item(what="Решение")
    real = items_service.meeting_items

    def stale_read(db, meeting_id):
        rows = real(db, meeting_id)  # в этот момент привязки ещё нет
        db.execute(
            update(Item).where(Item.id == item.id).values(agenda_item_id=host_topic.id),
            execution_options={"synchronize_session": False},
        )
        return rows

    monkeypatch.setattr(items_service, "meeting_items", stale_read)
    pairs = [{"agenda_item_id": llm_topic.id, "item_id": item.id}]
    assert apply_agenda_matches(world.db, world.meeting.id, pairs) == 0
    assert _linked(world, item) == host_topic.id


@pytest.mark.fr("FR-27")
def test_link_if_free_is_conditional_and_scoped_to_meeting(world: World) -> None:
    first, second = (
        make_agenda_item(world.db, world.meeting),
        make_agenda_item(world.db, world.meeting),
    )
    item = world.item(what="Решение")
    other = make_meeting(world.db, world.series)
    assert series_repo.link_agenda_if_free(world.db, other.id, item.id, first.id) is False
    assert series_repo.link_agenda_if_free(world.db, world.meeting.id, item.id, first.id) is True
    assert series_repo.link_agenda_if_free(world.db, world.meeting.id, item.id, second.id) is False
    assert _linked(world, item) == first.id


def _sent(world: World, meeting, at: datetime, kind=NotificationType.SUMMARY, **kw):
    return make_notification(
        world.db,
        type=kind,
        recipient_person_id=world.person.id,
        meeting_id=meeting.id,
        status=NotificationStatus.SENT,
        sent_at=at,
        **kw,
    )


@pytest.mark.fr("FR-28")
def test_sent_count_counts_letters_about_different_meetings_separately(world: World) -> None:
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    second = make_meeting(world.db, world.series)
    _sent(world, world.meeting, now)
    _sent(world, second, now)  # тот же тип, то же время, другая встреча: другое письмо
    count = notif_repo.sent_count(
        world.db, world.person.id, world.series.id, now - timedelta(days=7), now
    )
    assert count == 2


@pytest.mark.fr("FR-28")
def test_sent_count_still_merges_rows_of_one_letter(world: World) -> None:
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    _sent(world, world.meeting, now)
    _sent(world, world.meeting, now)  # две строки одного письма
    count = notif_repo.sent_count(
        world.db, world.person.id, world.series.id, now - timedelta(days=7), now
    )
    assert count == 1
