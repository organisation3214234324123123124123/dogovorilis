# ruff: noqa: F811
"""Рассылка: гонка отправки, повторная отправка после release_stuck,
кнопки писем с несколькими пунктами, срок пункта без исполнителя."""

import re
from datetime import timedelta

import pytest
from sqlalchemy import update

from app.modules.items.models import Item, ItemStatus
from app.modules.notifications import content, sender
from app.modules.notifications.models import Notification, NotificationStatus, NotificationType
from app.modules.notifications.render import render
from app.modules.series import service as series_service
from app.modules.series.schemas import SendRequest
from tests.acceptance.helpers_mailing import (  # noqa: F401
    API,
    DUE_FRIDAY,
    TUESDAY,
    Scenario,
    draft,
    frozen,
    host,
    link_for,
    mails,
    sc,
)


@pytest.mark.fr("FR-28")
def test_assignee_set_after_items_read_still_gets_link_and_letter(sc: Scenario) -> None:
    """Исполнитель, назначенный уже после чтения пунктов, всё равно получает ссылку и письмо."""
    db = sc.world.db
    late = draft(sc.world, None, None, what="Позвонить заказчику")
    db.flush()
    # объект уже в сессии рассылки (прочитан без исполнителя), а в базе другой процесс
    # успел назначить исполнителя - так выглядит гонка при чтении без блокировки
    assert late.assignee_person_id is None
    db.execute(
        update(Item).where(Item.id == late.id).values(assignee_person_id=sc.oleg.id),
        execution_options={"synchronize_session": False},
    )
    series_service.send_summary(
        db, sc.world.user, sc.world.meeting.id, SendRequest(confirm_open_gaps=True)
    )
    row = db.execute(Item.__table__.select().where(Item.id == late.id)).one()
    assert row.status == ItemStatus.PENDING and row.assignee_person_id == sc.oleg.id
    assert link_for(db, sc.oleg, sc.world.meeting), "у исполнителя нет ссылки"
    requests = mails(db, "CONFIRM_REQUEST", meeting=sc.world.meeting, item_id=late.id)
    assert len(requests) == 1, "pending-пункт с исполнителем остался без письма"


def _row(sc: Scenario, kind: str, key: str, when) -> Notification:
    mail = Notification(
        type=kind,
        recipient_person_id=sc.ivan.id,
        meeting_id=sc.world.meeting.id,
        item_id=sc.pending.id,
        payload={"what": sc.pending.what},
        dedupe_key=key,
        scheduled_for=when,
        status=NotificationStatus.QUEUED,
    )
    sc.world.db.add(mail)
    sc.world.db.flush()
    return mail


@pytest.mark.fr("FR-30")
def test_row_taken_over_by_another_run_is_not_sent_by_the_first_run(
    sc: Scenario, monkeypatch
) -> None:
    """Строку, перехваченную другим прогоном, первый прогон не отправляет."""
    # A держит две строки, B через 11 минут забирает вторую; A её повторно слать не должен.
    db = sc.world.db
    first = _row(sc, "ITEM_CHANGED", "race-1", TUESDAY - timedelta(minutes=1))
    second = _row(sc, "CONFIRM_REQUEST", "race-2", TUESDAY)
    delivered: list[str] = []
    state = {"nested": False}

    def deliver(mail) -> None:
        delivered.append(str(mail.type.value if hasattr(mail.type, "value") else mail.type))
        if not state["nested"]:
            state["nested"] = True
            sender.send_due(db, TUESDAY + timedelta(minutes=11))  # прогон B

    monkeypatch.setattr(sender, "deliver", deliver)
    sender.send_due(db, TUESDAY)  # прогон A
    assert [d for d in delivered if d == "CONFIRM_REQUEST"] == ["CONFIRM_REQUEST"]
    db.refresh(second)
    assert second.status == NotificationStatus.SENT
    assert first.id != second.id


def _summary_payload(sc: Scenario, kind: NotificationType) -> dict:
    items = [sc.pending, sc.agreed]
    sc.agreed.status = ItemStatus.PENDING
    link_id = "00000000-0000-4000-8000-000000000001"
    return {
        "series_title": "Планёрка",
        "host_name": "Ведущий",
        "items": [content.entry(i, "Иван") for i in items],
        "link_id": link_id,
        "buttons": [b for i in items for b in content.buttons(("accept",), kind, i.id)],
    }


def _accept_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if "Принимаю" in line]


@pytest.mark.fr("FR-35")
@pytest.mark.parametrize("kind", [NotificationType.SUMMARY, NotificationType.CONFIRM_REPEAT])
def test_each_accept_button_names_its_item(sc: Scenario, kind: NotificationType) -> None:
    letter = render([(kind, _summary_payload(sc, kind))])
    lines = _accept_lines(letter.text)
    assert len(lines) == 2
    for line, item in zip(lines, (sc.pending, sc.agreed), strict=True):
        assert item.what in line, f"кнопка без названия пункта: {line!r}"
    paragraphs = re.findall(r"<p>(.*?)</p>", letter.html)
    anchors = [n for n, p in enumerate(paragraphs) if "Принимаю" in p]
    for n, item in zip(anchors, (sc.pending, sc.agreed), strict=True):
        assert item.what in " ".join(paragraphs[n - 1 : n + 1]), "в HTML кнопка без названия"


@pytest.mark.fr("FR-36")
def test_summary_text_keeps_due_of_unassigned_item(host, sc: Scenario) -> None:
    sc.unassigned.due_date = DUE_FRIDAY
    sc.world.db.flush()
    text = host.post(f"{API}/meetings/{sc.world.meeting.id}/summary-text").json()["text"]
    line = next(line for line in text.splitlines() if "Найти подрядчика" in line)
    assert "не назначен" in line
    assert "25.01" in line, "срок пункта без исполнителя потерян"
