import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import Person, SeriesMember
from tests.conftest import login_as
from tests.factories import (
    World,
    add_member,
    make_organization,
    make_person,
    make_series,
    make_user,
)

pytestmark = pytest.mark.fr("FR-05")


def member_body(world: World, **kw) -> dict:
    return {
        "full_name": "Иван Петров",
        "email": "ivan@example.com",
        "organization_name": "ООО Ромашка",
        **kw,
    }


def url(series, suffix: str = "") -> str:
    return f"/api/v1/series/{series.id}/members{suffix}"


def count(world: World, model, *where) -> int:
    return world.db.scalar(select(func.count()).select_from(model).where(*where))


def test_add_new_person_creates_person_and_membership(authed: TestClient, world: World) -> None:
    resp = authed.post(url(world.series), json=member_body(world))
    assert resp.status_code == 201
    person = world.db.scalar(select(Person).where(Person.email == "ivan@example.com"))
    assert person is not None
    assert person.owner_user_id == world.user.id
    assert count(world, SeriesMember, SeriesMember.person_id == person.id) == 1


def test_same_email_any_case_reuses_person_across_series(authed: TestClient, world: World) -> None:
    other = make_series(world.db, world.user)
    first = authed.post(url(world.series), json=member_body(world))
    second = authed.post(url(other), json=member_body(world, email="IVAN@Example.COM"))
    assert first.status_code == second.status_code == 201
    assert first.json()["person_id"] == second.json()["person_id"]
    assert (
        count(
            world, Person, Person.owner_user_id == world.user.id, Person.email == "ivan@example.com"
        )
        == 1
    )


def test_same_email_other_host_is_separate_person(
    client: TestClient, authed: TestClient, world: World
) -> None:
    stranger = make_user(world.db)
    their_series = make_series(world.db, stranger)
    make_organization(world.db, stranger, is_own=True)
    mine = authed.post(url(world.series), json=member_body(world))
    login_as(client, stranger)
    theirs = client.post(url(their_series), json=member_body(world))
    assert mine.status_code == theirs.status_code == 201
    assert mine.json()["person_id"] != theirs.json()["person_id"]


def test_repeated_add_keeps_single_membership(authed: TestClient, world: World) -> None:
    first = authed.post(url(world.series), json=member_body(world))
    second = authed.post(url(world.series), json=member_body(world))
    assert first.status_code == 201
    assert second.status_code < 300
    assert count(world, SeriesMember, SeriesMember.series_id == world.series.id) == 1


def test_list_members_shows_name_email_and_side(authed: TestClient, world: World) -> None:
    outsider_org = make_organization(world.db, world.user, name="Партнёры")
    outsider = make_person(world.db, world.user, outsider_org, full_name="Пётр")
    add_member(world.db, world.series, world.person)
    add_member(world.db, world.series, outsider)
    resp = authed.get(url(world.series))
    assert resp.status_code == 200
    by_id = {m["person_id"]: m for m in resp.json()}
    own = by_id[str(world.person.id)]
    assert (own["full_name"], own["email"], own["is_own_side"]) == (
        world.person.full_name,
        world.person.email,
        True,
    )
    assert by_id[str(outsider.id)]["organization_name"] == "Партнёры"
    assert by_id[str(outsider.id)]["is_own_side"] is False


def test_delete_member_removes_membership_only(authed: TestClient, world: World) -> None:
    add_member(world.db, world.series, world.person)
    item = world.item(assignee_person_id=world.person.id, status="pending")
    resp = authed.delete(url(world.series, f"/{world.person.id}"))
    assert resp.status_code == 204
    assert count(world, SeriesMember, SeriesMember.person_id == world.person.id) == 0
    world.db.refresh(item)
    assert item.assignee_person_id == world.person.id
    assert item.status == "pending"


@pytest.mark.parametrize(
    "patch",
    [{"email": "not-an-email"}, {"full_name": "x" * 201}, {"full_name": ""}],
    ids=str,
)
def test_invalid_member_data(authed: TestClient, world: World, patch: dict) -> None:
    resp = authed.post(url(world.series), json=member_body(world, **patch))
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_foreign_series_members_are_404(authed: TestClient, world: World) -> None:
    foreign = make_series(world.db, make_user(world.db))
    person = world.person
    for resp in (
        authed.get(url(foreign)),
        authed.post(url(foreign), json=member_body(world)),
        authed.delete(url(foreign, f"/{person.id}")),
    ):
        assert resp.status_code == 404
        assert resp.json()["code"] == "NOT_FOUND"


def test_member_added_from_panel_can_be_assignee(authed: TestClient, world: World) -> None:
    created = authed.post(url(world.series), json=member_body(world))
    assert created.status_code == 201
    item = world.item(assignee_person_id=world.person.id, status="pending")
    resp = authed.post(
        f"/api/v1/items/{item.id}/actions",
        json={"action": "reassign", "assignee_person_id": created.json()["person_id"]},
        headers={"If-Match": str(item.version)},
    )
    assert resp.status_code == 200
    assert resp.json()["assignee"]["id"] == created.json()["person_id"]
