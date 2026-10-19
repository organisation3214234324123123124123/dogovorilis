import pytest

from app.core.config import Settings

pytestmark = pytest.mark.fr("infra")

LONG_KEY = "k" * 32


def test_prod_with_default_secret_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(app_version="1.2.3")


def test_prod_with_short_secret_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(app_version="1.2.3", secret_key="k" * 31)


def test_prod_with_long_secret_accepted() -> None:
    assert Settings(app_version="1.2.3", secret_key=LONG_KEY).secret_key == LONG_KEY


def test_dev_with_default_secret_accepted() -> None:
    assert Settings(app_version="dev").app_version == "dev"


def test_prod_with_short_admin_token_rejected() -> None:
    with pytest.raises(ValueError, match="ADMIN_TOKEN"):
        Settings(app_version="1.2.3", secret_key=LONG_KEY, admin_token="a" * 31)


def test_prod_with_long_admin_token_accepted() -> None:
    assert Settings(app_version="1.2.3", secret_key=LONG_KEY, admin_token="a" * 32)


def test_prod_with_empty_admin_token_accepted() -> None:
    assert Settings(app_version="1.2.3", secret_key=LONG_KEY, admin_token="").admin_token == ""


def test_dev_accepts_any_admin_token() -> None:
    assert Settings(app_version="dev", admin_token="x").admin_token == "x"
