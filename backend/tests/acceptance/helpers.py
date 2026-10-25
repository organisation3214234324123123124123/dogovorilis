from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from freezegun import freeze_time

from app.models import Item
from tests.conftest import login_as


@contextmanager
def frozen(client: TestClient, user, when: str) -> Iterator[None]:
    with freeze_time(when):
        login_as(client, user)
        yield


def act(client: TestClient, item: Item, action: str, *, version: int | None = None, **body):
    return client.post(
        f"/api/v1/items/{item.id}/actions",
        json={"action": action, **body},
        headers={"If-Match": str(item.version if version is None else version)},
    )
