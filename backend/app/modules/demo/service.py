import math
import re
import time
import uuid
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import RateLimited
from app.modules.auth.models import User
from app.modules.demo.schemas import (
    FastForwardRequest,
    FastForwardResult,
    GapView,
    Letter,
    LetterLink,
    LiveEvents,
    LlmCallRead,
    LlmCheckRequest,
    LlmCheckResult,
    LlmView,
    Mailbox,
    MovedMail,
    RulesView,
)
from app.modules.gaps.rules import find_gaps, question_for
from app.modules.items.parsing import parse_line
from app.modules.live import service as live_service
from app.modules.llm import service as llm_service
from app.modules.llm.client import StubLLMClient, get_llm_client
from app.modules.llm.models import LlmOutcome
from app.modules.notifications import service as notifications_service
from app.modules.series import service as series_service
from app.modules.series.models import Person

CHECK_LIMIT = 20
CHECK_WINDOW_SEC = 60
MAILBOX_SCAN = 50

mailbox_transport: httpx.BaseTransport | None = None

NO_MAILBOX = (
    "Ящик выключен: задайте DEMO_MAILBOX_URL (локально это делает compose.override.yaml с Mailpit)"
)
MESSAGE_ID = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})@dogovorilis"
)
URL = re.compile(r"https?://[^\s<>\"']+")
SLOW = {
    LlmOutcome.TIMEOUT: "не ответила вовремя (timeout)",
    LlmOutcome.INVALID_OUTPUT: "вернула невалидный ответ (invalid_output)",
}


def _check_rate(db: Session) -> None:
    """Квота Groq общая: одна проверка за раз и не больше 20 в минуту на весь стенд."""
    if not llm_service.try_check_lock(db):
        raise RateLimited("Проверка уже идёт, повторите через секунду", retry_after=1)
    count, oldest, now = llm_service.standalone_window(db, CHECK_WINDOW_SEC)
    if count >= CHECK_LIMIT and oldest is not None:
        wait = CHECK_WINDOW_SEC - (now - oldest).total_seconds()
        raise RateLimited("Слишком много проверок за минуту", retry_after=math.ceil(wait))


def llm_check(db: Session, user: User, body: LlmCheckRequest) -> LlmCheckResult:
    _check_rate(db)
    series = (
        series_service.get_owned_series(db, user, body.series_id)
        if body.series_id
        else series_service.first_active_series(db, user)
    )
    persons = series_service.member_persons(db, series.id) if series else []
    today = datetime.now(ZoneInfo(series.timezone if series else user.timezone)).date()
    return _compare(db, body.text, persons, today)


def _compare(db: Session, text: str, persons: list[Person], today: date) -> LlmCheckResult:
    names = {str(p.id): p.full_name for p in persons}
    parsed = parse_line(text, today, persons)
    assignee = next((p.full_name for p in persons if p.id == parsed.assignee_person_id), None)
    first_name = assignee.split()[0] if assignee else None
    gaps = [
        GapView(
            type=gap,
            question=question_for(None, gap, name=first_name, due_text=parsed.due_text),
        )
        for gap in find_gaps(parsed, today)
    ]

    client = get_llm_client()
    started = time.perf_counter()
    result = llm_service.parse_text(
        db, client, meeting_id=None, text=text, today=today, persons=persons
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000)

    live = not isinstance(client, StubLLMClient)
    if not live:
        verdict = (
            "Нейросеть не подключена: работает заглушка. "
            "Задайте LLM_API_KEY, LLM_BASE_URL, LLM_MODEL в .env"
        )
    elif result.outcome == LlmOutcome.OK:
        verdict = f"Нейросеть ответила за {elapsed_ms} мс: {client.model} ({client.provider})"
    else:
        verdict = f"Нейросеть {SLOW[result.outcome]}, сервис работает на правилах"
    return LlmCheckResult(
        live=live,
        provider=client.provider,
        model=client.model,
        elapsed_ms=elapsed_ms,
        verdict=verdict,
        rules=RulesView(
            what=parsed.what,
            assignee=assignee,
            due_date=parsed.due_date,
            due_text=parsed.due_text,
            gaps=gaps,
        ),
        llm=LlmView(
            outcome=result.outcome,
            what=result.what,
            assignee=names.get(result.assignee_id or ""),
            due_date=result.due_date,
            due_text=result.due_text,
            done_criteria=result.done_criteria,
            vague_action=result.vague_action,
            question=result.question,
        ),
    )


def llm_calls(db: Session, user: User, limit: int) -> list[LlmCallRead]:
    return [
        LlmCallRead.model_validate(c, from_attributes=True)
        for c in llm_service.recent_calls(db, limit, user.id)
    ]


def _links(text: str) -> list[LetterLink]:
    found: dict[str, LetterLink] = {}
    for raw in URL.findall(text):
        url = raw.rstrip(".,;)")
        parts = urlsplit(url)
        match = re.search(r"/p/([^/]+)$", parts.path)
        if match and url not in found:
            query = parse_qs(parts.query)
            found[url] = LetterLink(
                url=url,
                token=match.group(1),
                action=(query.get("action") or [None])[0],
                item_id=(query.get("item") or [None])[0],
                kind=(query.get("n") or [None])[0],
            )
    return list(found.values())


def _notification_id(message_id: str | None) -> uuid.UUID | None:
    match = MESSAGE_ID.fullmatch((message_id or "").strip("<>"))
    return uuid.UUID(match.group(1)) if match else None


def mailbox(db: Session, user: User, limit: int) -> Mailbox:
    base = get_settings().demo_mailbox_url.rstrip("/")
    if not base:
        return Mailbox(enabled=False, hint=NO_MAILBOX, letters=[])
    letters: list[Letter] = []
    try:
        with httpx.Client(transport=mailbox_transport, timeout=5) as http:
            listing = http.get(f"{base}/api/v1/messages", params={"limit": MAILBOX_SCAN})
            listing.raise_for_status()
            messages = listing.json()["messages"]
            ids = {m["ID"]: _notification_id(m.get("MessageID")) for m in messages}
            mine = notifications_service.owned_ids(db, user.id, [i for i in ids.values() if i])
            for msg in messages:
                if len(letters) == limit:
                    break
                if ids[msg["ID"]] not in mine:
                    continue
                full = http.get(f"{base}/api/v1/message/{msg['ID']}")
                full.raise_for_status()
                text = full.json().get("Text") or ""
                letters.append(
                    Letter(
                        received_at=msg["Created"],
                        to=[a["Address"] for a in msg.get("To") or []],
                        subject=msg.get("Subject") or "",
                        text=text,
                        links=_links(text),
                    )
                )
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return Mailbox(enabled=True, hint=f"Mailpit не отвечает: {type(exc).__name__}", letters=[])
    return Mailbox(enabled=True, letters=letters)


def fast_forward(db: Session, user: User, body: FastForwardRequest) -> FastForwardResult:
    if body.series_id:
        series_service.get_owned_series(db, user, body.series_id)
    moved = notifications_service.fast_forward(
        db, user.id, body.types, body.series_id, datetime.now(UTC)
    )
    if not moved:
        return FastForwardResult(moved=[], hint="Запланированных писем этих типов нет")
    return FastForwardResult(
        moved=[MovedMail(**m) for m in moved],
        hint="Celery Beat отправит их в течение минуты. Смотрите GET /api/v1/demo/mailbox",
    )


def live_events(db: Session, user: User, meeting_id: uuid.UUID, since: int) -> LiveEvents:
    series_service.get_owned_meeting(db, user, meeting_id)
    frames, version = live_service.resync_frames(db, meeting_id, since)
    return LiveEvents(version=version, frames=frames)
