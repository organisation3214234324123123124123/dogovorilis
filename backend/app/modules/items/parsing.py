"""Разбор строки решения правилами (FR-11): без сети, LLM и базы. LLM только дополняет (FR-12)."""

import calendar
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

WEEKDAYS = (
    ("понедельник", "понедельника", "понедельнику"),
    ("вторник", "вторника", "вторнику"),
    ("среду", "среды", "среде"),
    ("четверг", "четверга", "четвергу"),
    ("пятницу", "пятницы", "пятнице"),
    ("субботу", "субботы", "субботе"),
    ("воскресенье", "воскресенья", "воскресенью"),
)
_ACC = "|".join(acc for acc, _, _ in WEEKDAYS)
_GEN = "|".join(gen for _, gen, _ in WEEKDAYS)
_DAT = "|".join(dat for _, _, dat in WEEKDAYS)
MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]
NUMBERS = {
    "один": 1,
    "одну": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
}
VAGUE_PHRASES = (
    "как можно скорее",
    "в ближайшее время",
    "на днях",
    "при случае",
    "потом",
    "позже",
    "скоро",
)
PAST_DATE_KEEP_DAYS = 183
PREPOSITIONS = {"с", "со", "у", "для", "от", "к", "по"}
_WORD = re.compile(r"[\w-]+", re.UNICODE)
_WEEKDAY = re.compile(rf"\b(?:(в|во)\s+({_ACC})|(к)\s+({_DAT})|(до)\s+({_GEN}))\b", re.I)
_DATE = re.compile(r"\b(?:к|до)\s+(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?(?![\d.]*\d)", re.I)
_DATE_WORDS = re.compile(
    r"\b(?:к|до)\s+(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?\b", re.I
)
_NUMBER = "|".join(NUMBERS)
_IN_N = re.compile(rf"\bчерез\s+(\d{{1,3}}|{_NUMBER})\s+(дн(?:ей|я|ь)|день|недел(?:ю|и|ь))\b", re.I)
_END_OF = re.compile(r"\bдо\s+конца\s+(недели|месяца)\b", re.I)
_NEXT_WEEK = re.compile(rf"\bна\s+следующей\s+неделе(?:\s+(?:в|во)\s+({_ACC}))?\b", re.I)
_IN_WEEK = re.compile(r"\bчерез\s+неделю\b", re.I)
_RELATIVE = (
    (re.compile(r"\bпослезавтра\b", re.I), 2),
    (re.compile(r"\bзавтра\b", re.I), 1),
    (re.compile(r"\bсегодня\b", re.I), 0),
)
_SPACES = re.compile(r"\s+")


class Participant(Protocol):
    id: Any
    full_name: str


@dataclass(frozen=True)
class ParsedLine:
    what: str | None
    assignee_person_id: Any | None
    due_date: date | None
    due_text: str | None


def _norm(word: str) -> str:
    return word.lower().replace("ё", "е")


def _endings(name: str) -> tuple[str, str]:
    if name.endswith("а"):
        return name[:-1], r"(?:а|ы|и|е|у|ой|ою|ей|ею)"
    if name.endswith("я"):
        return name[:-1], r"(?:я|и|е|ю|ей|ею)"
    if name.endswith("ий"):
        return name[:-2], r"(?:ий|ия|ию|ием|ии)"
    if name.endswith("й"):
        return name[:-1], r"(?:й|я|ю|ем|е)"
    if name.endswith("ь"):
        return name[:-1], r"(?:ь|я|ю|ем|е)"
    return name, r"(?:|а|у|ом|е|ем|ы|ым|ой)"


def _stems(name: str) -> list[tuple[str, str]]:
    """У имён с беглой гласной (Павел - Павлу) добавляется вторая основа."""
    stem, endings = _endings(name)
    variants = [(stem, endings)]
    if len(name) >= 3 and name[-2] in "ео" and name[-1] not in "аеёиоуыэюяйь":
        base = name[:-2]
        soft = "ь" if base.endswith("л") and len(base) == 1 else ""
        variants.append((base + soft + name[-1], endings))
    return variants


def _matches(name: str, word: str) -> bool:
    lowered = _norm(word)
    return any(
        len(stem) >= 2 and re.fullmatch(re.escape(stem) + endings, lowered) is not None
        for stem, endings in _stems(_norm(name))
    )


def _find_assignee(
    words: list[re.Match[str]], participants: Sequence[Participant]
) -> tuple[Any | None, set[int]]:
    """Тёзок без фамилии в строке не угадываем и возвращаем None (OQ-08)."""
    hits: list[tuple[Participant, int, int | None]] = []
    for person in participants:
        parts = person.full_name.split()
        if not parts:
            continue
        for i, w in enumerate(words):
            if not _matches(parts[0], w.group()):
                continue
            surname_at = None
            if len(parts) > 1:
                for j in (i - 1, i + 1):
                    if 0 <= j < len(words) and _matches(parts[-1], words[j].group()):
                        surname_at = j
            hits.append((person, i, surname_at))
            break
    if not hits:
        return None, set()
    with_surname = [h for h in hits if h[2] is not None]
    chosen = with_surname if with_surname else hits
    if len(chosen) != 1:
        return None, set()
    person, index, surname_at = chosen[0]
    used = {index} | ({surname_at} if surname_at is not None else set())
    return person.id, used


def _weekday_number(word: str) -> int:
    lowered = _norm(word)
    return next(n for n, forms in enumerate(WEEKDAYS) if lowered in map(_norm, forms))


def _weekday_date(word: str, today: date, same_day_next_week: bool) -> date:
    days = (_weekday_number(word) - today.weekday()) % 7
    if days == 0 and same_day_next_week:
        days = 7
    return today + timedelta(days=days)


def _year_for(day: int, month: int, today: date) -> int:
    """Год не назван. Если дата давно прошла (30.12 при разборе "к 05.01"), берём следующий год
    (OQ-08).
    """
    candidate = _safe_date(today.year, month, day)
    if candidate is not None and (today - candidate).days > PAST_DATE_KEEP_DAYS:
        return today.year + 1
    return today.year


def _end_of_period(period: str, today: date) -> date:
    if period.lower() == "месяца":
        return today.replace(day=calendar.monthrange(today.year, today.month)[1])
    friday = today + timedelta(days=4 - today.weekday())
    return friday if friday >= today else friday + timedelta(days=7)


def _find_due(text: str, today: date) -> tuple[date | None, re.Match[str] | None]:
    if m := _NEXT_WEEK.search(text):
        monday = today + timedelta(days=7 - today.weekday())
        if m.group(1):
            return monday + timedelta(days=_weekday_number(m.group(1))), m
        return _end_of_period("недели", monday), m
    if m := _WEEKDAY.search(text):
        word = m.group(2) or m.group(4) or m.group(6)
        return _weekday_date(word, today, m.group(1) is not None), m
    if m := _DATE.search(text):
        year = m.group(3)
        year_number = (2000 + int(year) if len(year) == 2 else int(year)) if year else None
        year_number = year_number or _year_for(int(m.group(1)), int(m.group(2)), today)
        return _safe_date(year_number, int(m.group(2)), int(m.group(1))), m
    if m := _DATE_WORDS.search(text):
        month = MONTHS.index(m.group(2).lower()) + 1
        year_number = int(m.group(3)) if m.group(3) else _year_for(int(m.group(1)), month, today)
        return _safe_date(year_number, month, int(m.group(1))), m
    if m := _IN_WEEK.search(text):
        return today + timedelta(days=7), m
    if m := _IN_N.search(text):
        count = NUMBERS.get(m.group(1).lower()) or int(m.group(1))
        days = count * 7 if m.group(2).lower().startswith("недел") else count
        return today + timedelta(days=days), m
    if m := _END_OF.search(text):
        return _end_of_period(m.group(1), today), m
    for pattern, delta in _RELATIVE:
        if m := pattern.search(text):
            return today + timedelta(days=delta), m
    for phrase in VAGUE_PHRASES:
        if m := re.search(rf"\b{re.escape(phrase)}\b", text, re.I):
            return None, m
    return None, None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _clean_what(text: str, cut: list[tuple[int, int]], dropped_words: list[re.Match[str]]) -> str:
    spans = sorted(cut + [(w.start(), w.end()) for w in dropped_words])
    out, pos = [], 0
    for start, end in spans:
        if start >= pos:
            out.append(text[pos:start])
            pos = end
    out.append(text[pos:])
    words = _SPACES.sub(" ", "".join(out)).strip(" ,;:.-—").split(" ")
    while words and words[-1].lower() in PREPOSITIONS:
        words.pop()
    return " ".join(words).strip(" ,;:.-—")


def parse_line(
    text: str,
    today: date,
    participants: Sequence[Participant],
    manual_fields: Collection[str] = (),
) -> ParsedLine:
    words = list(_WORD.finditer(text))
    assignee_id, used = _find_assignee(words, participants)
    due, due_match = _find_due(text, today)

    dropped = [words[i] for i in used]
    for i in sorted(used):
        before = words[i - 1] if i > 0 else None
        if before is not None and before.group().lower() in PREPOSITIONS and i - 1 not in used:
            dropped.append(before)
    cut = [due_match.span()] if due_match else []
    what = _clean_what(text, cut, dropped)
    what = what[:1].upper() + what[1:]

    parsed = {
        "what": what[:500] or None,
        "assignee_person_id": assignee_id,
        "due_date": due,
        "due_text": due_match.group().strip()[:100] if due_match else None,
    }
    return ParsedLine(**{k: (None if k in manual_fields else v) for k, v in parsed.items()})
