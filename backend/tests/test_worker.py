import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.fr("infra")

MODULES_DIR = Path(__file__).resolve().parents[1] / "app" / "modules"


def _modules_with_tasks() -> set[str]:
    return {p.parent.name for p in MODULES_DIR.glob("*/tasks.py")}


def test_registered_tasks_come_only_from_modules_with_tasks_py() -> None:
    from app.worker import celery_app

    celery_app.loader.import_default_modules()
    expected = _modules_with_tasks()
    registered = {
        name.split(".")[2] for name in celery_app.tasks if name.startswith("app.modules.")
    }
    assert registered <= expected


def test_every_module_with_tasks_py_is_discovered() -> None:
    from app.worker import celery_app

    celery_app.loader.import_default_modules()
    for name in _modules_with_tasks():
        importlib.import_module(f"app.modules.{name}.tasks")
        assert any(t.startswith(f"app.modules.{name}.") for t in celery_app.tasks), name
