import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.models import ItemEvent, Notification, Person
from app.modules.items.models import ActorType, CommentKind, ItemAction, ItemComment, ItemStatus
from app.modules.notifications.models import NotificationStatus, NotificationType
from tests.conftest import login_as
from tests.factories import (
    World,
    make_access_link,
    make_event,
    make_organization,
    make_person,
    make_user,
)

pytestmark = pytest.mark.fr("FR-52")

TOKEN = "a" * 32
LINK = "tok-" + "z" * 28


@pytest.fixture
def admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    get_settings.cache_clear()
    return {"X-Admin-Token": TOKEN}


def url(person: Person) -> str:
    return f"/api/v1/admin/persons/{person.id}/anonymize"


def add_comment(w: World, item, person: Person, text: str = "Это не моё, спросите Анну") -> None:
    w.db.add(
        ItemComment(
            item_id=item.id, author_person_id=person.id, kind=CommentKind.NOT_MINE, text=text
        )
    )
    w.db.flush()


def add_notification(w: World, person: Person, status: NotificationStatus, key: str):
    row = Notification(
        type=NotificationType.CONFIRM_REQUEST,
        recipient_person_id=person.id,
        meeting_id=w.meeting.id,
        dedupe_key=key,
        scheduled_for=datetime.now(UTC) + timedelta(hours=1),
        status=status,
        payload={"k": "v"},
    )
    w.db.add(row)
    w.db.flush()
    return row


def test_without_token_is_admin_forbidden(client: TestClient, world: World, admin) -> None:
    resp = client.post(url(world.person))
    assert resp.status_code == 403
    assert resp.json()["code"] == "ADMIN_FORBIDDEN"


def test_host_session_is_admin_forbidden(client: TestClient, world: World, admin) -> None:
    login_as(client, world.user)
    resp = client.post(url(world.person))
    assert resp.status_code == 403
    assert resp.json()["code"] == "ADMIN_FORBIDDEN"
    assert world.db.get(Person, world.person.id).anonymized_at is None


def test_empty_admin_token_is_admin_disabled(
    client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "")
    get_settings.cache_clear()
    resp = client.post(url(world.person), headers={"X-Admin-Token": TOKEN})
    assert resp.json()["code"] == "ADMIN_DISABLED"


def test_unknown_person_is_404(client: TestClient, admin) -> None:
    import uuid

    resp = client.post(f"/api/v1/admin/persons/{uuid.uuid4()}/anonymize", headers=admin)
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_name_email_and_timestamp_are_replaced(client: TestClient, world: World, admin) -> None:
    resp = client.post(url(world.person), headers=admin)
    assert resp.status_code == 200
    person = world.db.get(Person, world.person.id)
    world.db.refresh(person)
    assert re.fullmatch(r"Участник \d+", person.full_name)
    assert person.email is None
    assert person.anonymized_at is not None
    body = resp.json()
    assert body["person_id"] == str(person.id)
    assert body["full_name"] == person.full_name


def test_two_persons_get_different_numbers(client: TestClient, world: World, admin) -> None:
    other = make_person(world.db, world.user, world.org)
    client.post(url(world.person), headers=admin)
    client.post(url(other), headers=admin)
    world.db.refresh(world.person)
    world.db.refresh(other)
    assert world.person.full_name != other.full_name


def test_repeated_call_is_idempotent(client: TestClient, world: World, admin) -> None:
    first = client.post(url(world.person), headers=admin)
    second = client.post(url(world.person), headers=admin)
    assert second.status_code == 200
    assert second.json()["full_name"] == first.json()["full_name"]


def test_all_links_of_the_person_are_revoked(client: TestClient, world: World, admin) -> None:
    from tests.factories import make_meeting

    link = make_access_link(world.db, world.person, world.meeting)
    other_meeting = make_meeting(world.db, world.series)
    link2 = make_access_link(world.db, world.person, other_meeting)
    resp = client.post(url(world.person), headers=admin)
    world.db.refresh(link)
    world.db.refresh(link2)
    assert link.revoked_at is not None
    assert link2.revoked_at is not None
    assert resp.json()["links_revoked"] == 2


def test_links_of_other_people_stay_active(client: TestClient, world: World, admin) -> None:
    other = make_person(world.db, world.user, world.org)
    link = make_access_link(world.db, other, world.meeting)
    assert client.post(url(world.person), headers=admin).status_code == 200
    world.db.refresh(link)
    assert link.revoked_at is None


def test_old_link_returns_410_link_expired(client: TestClient, world: World, admin) -> None:
    make_access_link(
        world.db, world.person, world.meeting, token_hash=hashlib.sha256(LINK.encode()).digest()
    )
    assert client.get(f"/api/v1/p/{LINK}").status_code == 200
    client.post(url(world.person), headers=admin)
    resp = client.get(f"/api/v1/p/{LINK}")
    assert resp.status_code == 410
    assert resp.json()["code"] == "LINK_EXPIRED"


def test_comments_of_the_person_are_cleared(client: TestClient, world: World, admin) -> None:
    item = world.item(assignee_person_id=world.person.id)
    add_comment(world, item, world.person)
    resp = client.post(url(world.person), headers=admin)
    row = world.db.scalars(select(ItemComment).where(ItemComment.item_id == item.id)).one()
    world.db.refresh(row)
    assert row.text is None
    assert row.anonymized_at is not None
    assert resp.json()["comments_cleared"] == 1


def test_comments_of_other_people_are_untouched(client: TestClient, world: World, admin) -> None:
    other = make_person(world.db, world.user, world.org)
    item = world.item()
    add_comment(world, item, other, "Чужой текст")
    assert client.post(url(world.person), headers=admin).status_code == 200
    row = world.db.scalars(select(ItemComment).where(ItemComment.item_id == item.id)).one()
    world.db.refresh(row)
    assert row.text == "Чужой текст"
    assert row.anonymized_at is None


def test_items_and_statuses_stay(client: TestClient, world: World, admin) -> None:
    item = world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.DONE,
        sent_at=datetime.now(UTC),
        done_at=datetime.now(UTC),
    )
    assert client.post(url(world.person), headers=admin).status_code == 200
    world.db.refresh(item)
    assert item.assignee_person_id == world.person.id
    assert item.status == ItemStatus.DONE
    assert world.db.get(Person, world.person.id) is not None


def test_item_events_are_unchanged(client: TestClient, world: World, admin) -> None:
    item = world.item(assignee_person_id=world.person.id)
    make_event(
        world.db,
        item,
        action=ItemAction.ACCEPTED,
        actor_type=ActorType.ASSIGNEE,
        actor_person_id=world.person.id,
        data={"status_to": "accepted"},
    )

    def snapshot():
        rows = world.db.scalars(select(ItemEvent).where(ItemEvent.item_id == item.id)).all()
        return [(r.id, r.actor_person_id, r.action, r.data, r.created_at) for r in rows]

    before = snapshot()
    assert client.post(url(world.person), headers=admin).status_code == 200
    assert snapshot() == before


def test_queued_letters_to_the_person_are_cancelled(
    client: TestClient, world: World, admin
) -> None:
    queued = add_notification(world, world.person, NotificationStatus.QUEUED, "q1")
    sent = add_notification(world, world.person, NotificationStatus.SENT, "s1")
    client.post(url(world.person), headers=admin)
    world.db.refresh(queued)
    world.db.refresh(sent)
    assert queued.status == NotificationStatus.CANCELLED
    assert sent.status == NotificationStatus.SENT


def test_queued_letters_to_other_people_stay_queued(
    client: TestClient, world: World, admin
) -> None:
    other = make_person(world.db, world.user, world.org)
    queued = add_notification(world, other, NotificationStatus.QUEUED, "q2")
    assert client.post(url(world.person), headers=admin).status_code == 200
    world.db.refresh(queued)
    assert queued.status == NotificationStatus.QUEUED


def test_numbering_is_per_host(client: TestClient, world: World, admin) -> None:
    other_user = make_user(world.db)
    other_org = make_organization(world.db, other_user, is_own=True)
    other_person = make_person(world.db, other_user, other_org)
    r1 = client.post(url(world.person), headers=admin)
    r2 = client.post(url(other_person), headers=admin)
    assert r1.json()["full_name"] == "Участник 1"
    assert r2.json()["full_name"] == "Участник 1"


def test_numbers_within_one_host_are_sequential(client: TestClient, world: World, admin) -> None:
    other = make_person(world.db, world.user, world.org)
    r1 = client.post(url(world.person), headers=admin)
    r2 = client.post(url(other), headers=admin)
    assert (r1.json()["full_name"], r2.json()["full_name"]) == ("Участник 1", "Участник 2")
