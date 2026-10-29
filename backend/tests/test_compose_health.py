import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.fr("NFR-18")

ROOT = Path(__file__).resolve().parents[2]
WAITING = ("backend", "worker", "beat")


def compose_cli() -> str:
    docker = shutil.which("docker")
    problem = "docker не найден в PATH"
    if docker is not None:
        probe = subprocess.run(
            [docker, "compose", "version"], capture_output=True, text=True, check=False
        )
        if probe.returncode == 0:
            return docker
        problem = "нет плагина docker compose v2"
    if os.environ.get("CI"):
        pytest.fail(f"{problem}: в CI нужен `docker compose config` (демон не нужен)")
    pytest.skip(f"{problem}: нужен `docker compose config` (демон не нужен)")


def compose_services(tmp_path: Path, *, with_override: bool) -> dict[str, Any]:
    """Сервисы из `docker compose config`: без .env репозитория и без демона."""
    docker = compose_cli()
    names = ["compose.yaml", *(["compose.override.yaml"] if with_override else [])]
    for name in names:
        shutil.copy(ROOT / name, tmp_path / name)
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    cmd = [docker, "compose", *(arg for name in names for arg in ("-f", name))]
    result = subprocess.run(
        [*cmd, "config", "--format", "json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"docker compose config упал: {result.stderr.strip()}"
    return json.loads(result.stdout)["services"]


@pytest.fixture(params=[True, False], ids=["with-override", "compose-only"])
def services(request: pytest.FixtureRequest, tmp_path: Path) -> dict[str, Any]:
    return compose_services(tmp_path, with_override=request.param)


def test_redis_has_ping_healthcheck(services: dict[str, Any]) -> None:
    healthcheck = services["redis"].get("healthcheck")
    assert healthcheck, "у redis нет healthcheck"
    assert healthcheck["test"] == ["CMD", "redis-cli", "ping"]


@pytest.mark.parametrize("name", WAITING)
def test_service_waits_for_healthy_redis(services: dict[str, Any], name: str) -> None:
    depends_on = services[name].get("depends_on", {})
    assert depends_on.get("redis", {}).get("condition") == "service_healthy"
