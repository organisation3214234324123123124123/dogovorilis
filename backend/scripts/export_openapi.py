import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app

TARGET = Path(__file__).resolve().parents[2] / "docs" / "api" / "openapi.yaml"


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        yaml.safe_dump(app.openapi(), sort_keys=True, allow_unicode=True), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
