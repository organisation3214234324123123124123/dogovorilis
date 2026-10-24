import pytest

from app.core.security import hash_password, verify_password

pytestmark = pytest.mark.fr("infra")


def test_hash_verify_roundtrip() -> None:
    assert verify_password("demo1234", hash_password("demo1234"))


def test_wrong_password_rejected() -> None:
    assert not verify_password("other", hash_password("demo1234"))


def test_hash_is_salted_and_not_plain() -> None:
    first, second = hash_password("demo1234"), hash_password("demo1234")
    assert first != second
    assert "demo1234" not in first
