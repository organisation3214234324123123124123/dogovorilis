# ruff: noqa: F811
"""FR-30 п.4: у каждого письма со ссылкой свой случайный токен, в базе только его SHA-256."""

import base64
import logging
import re
from datetime import UTC, datetime, timedelta

import pytest

from tests.acceptance.helpers import act
from tests.acceptance.helpers_mailing import (  # noqa: F401
    TUESDAY,
    Mailbox,
    Scenario,
    frozen,
    hash_is_stored,
    host,
    link_for,
    mailbox,
    raw_token_stored_anywhere,
    sc,
    send,
)
from tests.factories import add_member

pytestmark = pytest.mark.fr("FR-30")

REPEAT_AT = datetime(2030, 1, 16, 12, 0, tzinfo=UTC)
REMINDER_AT = datetime(2030, 1, 24, 6, 0, tzinfo=UTC)


def tokens_to(box: Mailbox, person) -> list[str]:
    return [t for letter in box.to(person) for t in dict.fromkeys(box.tokens(letter))]


def deliver_everything(box: Mailbox) -> None:
    for at in (TUESDAY, REPEAT_AT, REMINDER_AT):
        box.run(at)


def page(client, token: str) -> int:
    return client.get(f"/api/v1/p/{token}").status_code


def test_every_token_is_32_bytes_base64url(host, sc: Scenario, mailbox: Mailbox) -> None:
    send(host, sc.world)
    deliver_everything(mailbox)
    tokens = tokens_to(mailbox, sc.ivan)
    assert len(tokens) >= 3
    for token in tokens:
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
        assert len(base64.urlsafe_b64decode(token + "=")) == 32


def test_summary_and_confirm_request_to_one_person_carry_different_tokens(
    host, sc: Scenario, mailbox: Mailbox
) -> None:
    send(host, sc.world)
    mailbox.run(TUESDAY)
    letters = mailbox.to(sc.ivan)
    assert len(letters) == 2
    first, second = ({*mailbox.tokens(letter)} for letter in letters)
    assert len(first) == len(second) == 1
    assert first != second


def test_all_tokens_of_one_link_open_the_page(host, sc: Scenario, mailbox: Mailbox) -> None:
    send(host, sc.world)
    deliver_everything(mailbox)
    tokens = set(tokens_to(mailbox, sc.ivan))
    assert len(tokens) == len(mailbox.to(sc.ivan)) >= 3
    assert {page(host, token) for token in tokens} == {200}
    assert len(link_for(sc.world.db, sc.ivan, sc.world.meeting)) == 1


def test_repeat_letter_gets_a_token_not_seen_before(host, sc: Scenario, mailbox: Mailbox) -> None:
    send(host, sc.world)
    mailbox.run(TUESDAY)
    before = set(tokens_to(mailbox, sc.ivan))
    mailbox.run(REPEAT_AT)
    after = tokens_to(mailbox, sc.ivan)
    assert len(after) == 3
    (repeat_token,) = set(after) - before
    assert page(host, repeat_token) == 200
    assert {page(host, token) for token in before} == {200}


def test_due_tomorrow_letter_gets_a_token_not_seen_before(
    host, sc: Scenario, mailbox: Mailbox
) -> None:
    send(host, sc.world)
    mailbox.run(TUESDAY)
    before = set(tokens_to(mailbox, sc.ivan))
    mailbox.run(REMINDER_AT)
    reminder = mailbox.to(sc.ivan)[-1]
    assert "Завтра срок" in reminder["subject"]
    (token,) = set(mailbox.tokens(reminder))
    assert token not in before
    assert page(host, token) == 200


def test_every_token_hash_is_stored_and_no_raw_token_anywhere(
    host, sc: Scenario, mailbox: Mailbox
) -> None:
    send(host, sc.world)
    deliver_everything(mailbox)
    tokens = set(tokens_to(mailbox, sc.ivan))
    assert len(tokens) >= 3
    for token in tokens:
        assert hash_is_stored(sc.world.db, token)
        assert not raw_token_stored_anywhere(sc.world.db, token)


def test_tokens_do_not_depend_on_secret_key(
    host, sc: Scenario, mailbox: Mailbox, monkeypatch
) -> None:
    from app.core.config import get_settings

    send(host, sc.world)
    deliver_everything(mailbox)
    tokens = set(tokens_to(mailbox, sc.ivan))
    assert len(tokens) >= 3
    monkeypatch.setenv("SECRET_KEY", "another-" + "k" * 40)
    get_settings.cache_clear()
    assert {page(host, token) for token in tokens} == {200}


def test_revoking_the_link_kills_all_its_tokens(host, sc: Scenario, mailbox: Mailbox) -> None:
    send(host, sc.world)
    deliver_everything(mailbox)
    tokens = set(tokens_to(mailbox, sc.ivan))
    assert len(tokens) >= 3
    (link,) = link_for(sc.world.db, sc.ivan, sc.world.meeting)
    link.revoked_at = datetime.now(UTC)
    sc.world.db.flush()
    assert {page(host, token) for token in tokens} == {410}


def test_expired_link_kills_all_its_tokens(host, sc: Scenario, mailbox: Mailbox, frozen) -> None:
    send(host, sc.world)
    deliver_everything(mailbox)
    tokens = set(tokens_to(mailbox, sc.ivan))
    assert len(tokens) >= 3
    (link,) = link_for(sc.world.db, sc.ivan, sc.world.meeting)
    frozen.move_to(link.expires_at + timedelta(minutes=1))
    assert {page(host, token) for token in tokens} == {410}


def test_reassigned_item_is_hidden_behind_every_old_token(
    host, sc: Scenario, mailbox: Mailbox
) -> None:
    add_member(sc.world.db, sc.world.series, sc.oleg)
    send(host, sc.world)
    deliver_everything(mailbox)
    tokens = set(tokens_to(mailbox, sc.ivan))
    assert len(tokens) >= 3
    assert act(host, sc.pending, "reassign", assignee_person_id=str(sc.oleg.id)).status_code == 200
    for token in tokens:
        assert str(sc.pending.id) not in host.get(f"/api/v1/p/{token}").text


def test_unknown_token_is_404_like_before(host, sc: Scenario) -> None:
    assert page(host, base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode()) == 404


def test_no_token_reaches_logs(host, sc: Scenario, mailbox: Mailbox, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    send(host, sc.world)
    deliver_everything(mailbox)
    tokens = set(tokens_to(mailbox, sc.ivan))
    for token in tokens:
        page(host, token)
    app_log = "\n".join(r.getMessage() for r in caplog.records if r.name != "httpx")
    assert not any(token in app_log for token in tokens)
