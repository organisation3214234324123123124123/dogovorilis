import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import Item, Person
from app.modules.llm import repository
from app.modules.llm.client import LLMClient, LLMError, LLMResponse, LLMTimeout
from app.modules.llm.models import LlmCall, LlmOutcome, LlmPrompt, LlmTask
from app.modules.llm.schemas import PromptCreate

TIMEOUT_SECONDS = 8
QUESTION_MAX = 120
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
INFINITIVE = re.compile(r"^[а-яё]+(?:ть|ти|чь)(?:ся|сь)?$")
# существительные на -ть/-ти/-чь (и слова, похожие на инфинитив), которые не глаголы
NOT_INFINITIVES = frozenset(
    {
        "часть",
        "сеть",
        "путь",
        "пути",
        "связь",
        "область",
        "власть",
        "новость",
        "честь",
        "жизнь",
        "мысль",
        "помощь",
        "ночь",
        "дочь",
        "вещь",
        "речь",
        "печь",
        "очередь",
        "запись",
        "цель",
        "роль",
        "кисть",
        "плоть",
        "масть",
        "пасть",
        "страсть",
        "радость",
        "болезнь",
        "точность",
        "готовность",
        "возможность",
        "скорость",
        "сложность",
        "зависимость",
        "стоимость",
        "мощь",
        "смесь",
        "лесть",
        "участь",
        "напасть",
        "зависть",
        "ненависть",
        "кость",
        "горсть",
        "цепь",
        "память",
        "пять",
        "десять",
        "суть",
        "жуть",
        "муть",
        "прыть",
        "подать",
        "рать",
        "ответственность",
        "лопасть",
        "снасть",
        "сласть",
        "верность",
        "гласность",
        "плеть",
        "клеть",
        "нить",
        "степень",
        "ступень",
    }
)


@dataclass
class ParseResult:
    outcome: LlmOutcome
    what: str | None = None
    assignee_id: str | None = None
    due_date: date | None = None
    due_text: str | None = None
    done_criteria: str | None = None
    vague_action: bool | None = None
    question: str | None = None


def list_prompts(db: Session) -> list[LlmPrompt]:
    return repository.list_prompts(db)


def get_active_prompt(db: Session, task: LlmTask) -> LlmPrompt | None:
    return repository.get_active_prompt(db, task)


def create_prompt(db: Session, body: PromptCreate) -> LlmPrompt:
    repository.lock_task(db, body.task)
    if body.activate:
        repository.deactivate_prompts(db, body.task)
    prompt = LlmPrompt(
        task=body.task,
        version=repository.next_version(db, body.task),
        text=body.text,
        is_active=body.activate,
    )
    return repository.add_prompt(db, prompt)


def _first_name(person: Person) -> str:
    parts = person.full_name.split()
    return parts[0] if parts else ""


def _text(value: Any, limit: int) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= limit:
        return value
    return None


def _what(value: Any) -> str | None:
    text = _text(value, 500)
    if text is None:
        return None
    first = text.split()[0].strip(".,:;!?\"'«»()").lower()
    return text if INFINITIVE.match(first) and first not in NOT_INFINITIVES else None


def _due_date(value: Any, today: date) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed >= today else None


def _validate(data: dict, today: date, participant_ids: set[str]) -> dict[str, Any]:
    assignee = data.get("assignee_id")
    vague = data.get("vague_action")
    return {
        "what": _what(data.get("what")),
        "assignee_id": assignee
        if isinstance(assignee, str) and assignee in participant_ids
        else None,
        "due_date": _due_date(data.get("due_date"), today),
        "due_text": _text(data.get("due_text"), 100),
        "done_criteria": _text(data.get("done_criteria"), 500),
        "vague_action": vague if isinstance(vague, bool) else None,
        "question": _text(data.get("question"), QUESTION_MAX),
    }


def _load(content: str) -> dict | None:
    try:
        data = json.loads(content)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _run(
    db: Session,
    client: LLMClient,
    prompt: LlmPrompt,
    meeting_id: uuid.UUID | None,
    payload: dict,
) -> tuple[LlmOutcome, dict | None]:
    started = time.perf_counter()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    tokens_in: int | None = None
    tokens_out: int | None = None
    outcome = LlmOutcome.INVALID_OUTPUT
    data: dict | None = None
    for _ in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            outcome = LlmOutcome.TIMEOUT
            break
        try:
            response: LLMResponse = client.complete(prompt.text, payload, timeout=remaining)
        except LLMTimeout:
            outcome = LlmOutcome.TIMEOUT
            break
        except LLMError:
            break
        if response.input_tokens is not None:
            tokens_in = (tokens_in or 0) + response.input_tokens
        if response.output_tokens is not None:
            tokens_out = (tokens_out or 0) + response.output_tokens
        data = _load(response.content)
        if data is not None:
            outcome = LlmOutcome.OK
            break
    repository.add_call(
        db,
        LlmCall(
            meeting_id=meeting_id,
            task=prompt.task,
            provider=client.provider,
            model=client.model,
            prompt_id=prompt.id,
            latency_ms=round((time.perf_counter() - started) * 1000),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            outcome=outcome,
        ),
    )
    return outcome, data


def parse_text(
    db: Session,
    client: LLMClient,
    *,
    meeting_id: uuid.UUID | None,
    text: str,
    today: date,
    persons: list[Person],
) -> ParseResult:
    prompt = repository.get_active_prompt(db, LlmTask.PARSE)
    if prompt is None:
        return ParseResult(LlmOutcome.INVALID_OUTPUT)
    payload = {
        "text": text,
        "today": today.isoformat(),
        "weekday": WEEKDAYS[today.weekday()],
        "participants": [{"id": str(p.id), "name": _first_name(p)} for p in persons],
    }
    outcome, data = _run(db, client, prompt, meeting_id, payload)
    if outcome != LlmOutcome.OK or data is None:
        return ParseResult(outcome)
    return ParseResult(outcome, **_validate(data, today, {str(p.id) for p in persons}))


def enrich_item(
    db: Session, client: LLMClient, item: Item, *, today: date, persons: list[Person]
) -> ParseResult:
    result = parse_text(
        db, client, meeting_id=item.meeting_id, text=item.raw_text, today=today, persons=persons
    )
    if result.outcome != LlmOutcome.OK:
        return result
    manual = set(item.manual_fields or [])
    candidates = {
        "what": result.what,
        "assignee_person_id": uuid.UUID(result.assignee_id) if result.assignee_id else None,
        "due_date": result.due_date,
        "due_text": result.due_text,
        "done_criteria": result.done_criteria,
    }
    for field, value in candidates.items():
        if value is not None and field not in manual and getattr(item, field) in (None, ""):
            setattr(item, field, value)
    db.flush()
    return result


def match_agenda(
    db: Session,
    client: LLMClient,
    *,
    meeting_id: uuid.UUID,
    agenda: list[dict[str, str]],
    decisions: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Один запрос на всю повестку (FR-27). Чужие id отбрасывает вызывающий."""
    prompt = repository.get_active_prompt(db, LlmTask.AGENDA)
    if prompt is None:
        return []
    outcome, data = _run(db, client, prompt, meeting_id, {"agenda": agenda, "decisions": decisions})
    pairs = data.get("pairs") if outcome == LlmOutcome.OK and data else None
    if not isinstance(pairs, list):
        return []
    return [p for p in pairs if isinstance(p, dict)]


def purge_old_calls(db: Session, cutoff: datetime, limit: int) -> int:
    return repository.delete_calls_before(db, cutoff, limit)
