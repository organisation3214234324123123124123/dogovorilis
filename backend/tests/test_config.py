import pytest

from app.core.config import Settings

pytestmark = pytest.mark.fr("infra")

LONG_KEY = "k" * 32


def test_default_is_not_dev() -> None:
    assert Settings.model_fields["dev_mode"].default is False
    assert Settings(dev_mode=False, secret_key=LONG_KEY).is_dev is False


def test_dev_version_without_flag_is_not_dev() -> None:
    assert Settings(dev_mode=False, app_version="dev", secret_key=LONG_KEY).is_dev is False


def test_dev_version_without_flag_rejects_default_secret() -> None:
    with pytest.raises(ValueError):
        Settings(dev_mode=False, app_version="dev")


def test_flag_enables_dev_for_any_version() -> None:
    assert Settings(dev_mode=True, app_version="1.2.3").is_dev is True


def test_empty_flag_from_env_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_MODE", "")
    monkeypatch.setenv("SECRET_KEY", LONG_KEY)
    assert Settings().is_dev is False


def test_prod_with_default_secret_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(dev_mode=False, app_version="1.2.3")


def test_prod_with_short_secret_rejected() -> None:
    with pytest.raises(ValueError):
        Settings(dev_mode=False, app_version="1.2.3", secret_key="k" * 31)


def test_prod_with_long_secret_accepted() -> None:
    s = Settings(dev_mode=False, app_version="1.2.3", secret_key=LONG_KEY)
    assert s.secret_key == LONG_KEY


def test_dev_with_default_secret_accepted() -> None:
    assert Settings(dev_mode=True).is_dev is True


def test_prod_with_short_admin_token_rejected() -> None:
    with pytest.raises(ValueError, match="ADMIN_TOKEN"):
        Settings(dev_mode=False, app_version="1.2.3", secret_key=LONG_KEY, admin_token="a" * 31)


def test_prod_with_long_admin_token_accepted() -> None:
    assert Settings(dev_mode=False, app_version="1.2.3", secret_key=LONG_KEY, admin_token="a" * 32)


def test_prod_with_empty_admin_token_accepted() -> None:
    s = Settings(dev_mode=False, app_version="1.2.3", secret_key=LONG_KEY, admin_token="")
    assert s.admin_token == ""


def test_dev_accepts_any_admin_token() -> None:
    assert Settings(dev_mode=True, admin_token="x").admin_token == "x"
