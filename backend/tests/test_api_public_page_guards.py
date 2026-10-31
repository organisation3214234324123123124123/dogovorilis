import hashlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.modules.items.models import ItemStatus
from tests.factories import World, make_access_link

pytestmark = pytest.mark.fr("FR-31")

TOKEN = "tok-" + "c" * 28
URL = f"/api/v1/p/{TOKEN}"


@pytest.fixture
def own_item(world: World):
    token_hash = hashlib.sha256(TOKEN.encode()).digest()
    make_access_link(world.db, world.person, world.meeting, token_hash=token_hash)
    return world.item(
        assignee_person_id=world.person.id,
        status=ItemStatus.PENDING,
        sent_at=datetime.now(UTC),
        what="Mine",
    )


@pytest.mark.fr("BR-12")
@pytest.mark.parametrize("action", ["accept", "decline", "done", "reschedule"])
def test_get_with_action_changes_nothing(
    client: TestClient, world: World, own_item, action: str
) -> None:
    client.get(f"{URL}?action={action}&item={own_item.id}")
    world.db.refresh(own_item)
    assert own_item.status == ItemStatus.PENDING
    assert own_item.version == 1


@pytest.mark.fr("FR-30")
@pytest.mark.parametrize("kind", ["SUMMARY", "CONFIRM_REQUEST", "CONFIRM_REPEAT", "DUE_TOMORROW"])
def test_known_notification_kinds_open_the_page(client: TestClient, own_item, kind: str) -> None:
    assert client.get(f"{URL}?n={kind}").status_code == 200


@pytest.mark.fr("FR-30")
def test_unknown_notification_kind_is_still_rejected(client: TestClient, own_item) -> None:
    assert client.get(f"{URL}?n=NOPE").status_code == 422
