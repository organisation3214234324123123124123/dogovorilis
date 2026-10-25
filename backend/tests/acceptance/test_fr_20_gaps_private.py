import hashlib
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.modules.gaps.models import GapType
from app.modules.items.models import ItemStatus
from tests.acceptance.helpers_items import API, make_ctx
from tests.factories import World, make_access_link, make_gap

pytestmark = pytest.mark.fr("FR-20")

TOKEN = "tok-" + "c" * 28


def test_gap_questions_never_reach_executor_page(client: TestClient, world: World) -> None:
    ctx = make_ctx(world)
    item = world.item(
        what="Подготовить отчёт",
        assignee_person_id=ctx.ivan.id,
        status=ItemStatus.PENDING,
        sent_at=datetime.now(UTC),
    )
    make_gap(world.db, item, type=GapType.NO_DUE, question="СЕКРЕТНЫЙ ВОПРОС ВЕДУЩЕМУ")
    make_access_link(
        world.db, ctx.ivan, world.meeting, token_hash=hashlib.sha256(TOKEN.encode()).digest()
    )
    page = client.get(f"{API}/p/{TOKEN}")
    assert page.status_code == 200
    assert page.json()["items"]
    assert "СЕКРЕТНЫЙ ВОПРОС" not in json.dumps(page.json(), ensure_ascii=False)
    assert "gaps" not in page.json()["items"][0]
