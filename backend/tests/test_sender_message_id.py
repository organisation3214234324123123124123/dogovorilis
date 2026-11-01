"""Письмо уходит с Message-ID <{id уведомления}@dogovorilis>: по нему письмо находят в ящике."""

import smtplib
import uuid
from typing import Any

import pytest

from app.core.config import get_settings
from app.modules.notifications import sender
from app.modules.notifications.models import NotificationType
from tests.factories import World, make_notification

pytestmark = pytest.mark.fr("infra")


def test_deliver_sets_message_id_of_notification(
    world: World, monkeypatch: pytest.MonkeyPatch, settings_reset: None
) -> None:
    sent: list[Any] = []

    class FakeSMTP:
        def __init__(self, *_a: Any, **_kw: Any) -> None: ...

        def __enter__(self):
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        def has_extn(self, _name: str) -> bool:
            return False

        def login(self, *_a: Any) -> None: ...

        def send_message(self, message) -> None:
            sent.append(message)

    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    get_settings.cache_clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    mail = make_notification(
        world.db,
        id=uuid.uuid4(),
        type=NotificationType.PASSWORD_RESET,
        recipient_user_id=world.user.id,
        payload={"url": "http://app.test/reset-password?token=t"},
    )
    sender.deliver(mail)
    assert len(sent) == 1
    assert sent[0]["Message-ID"] == f"<{mail.id}@dogovorilis>"
