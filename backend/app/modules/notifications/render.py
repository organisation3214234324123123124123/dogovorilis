from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from html import escape
from typing import Any

from app.core.config import get_settings
from app.modules.confirm.tokens import new_token
from app.modules.notifications.models import NotificationType

SUBJECT_MAX = 150
SUBJECTS = {
    NotificationType.SUMMARY: "Итоги встречи «{series_title}»",
    NotificationType.CONFIRM_REQUEST: "Подтвердите договорённость: {what}",
    NotificationType.CONFIRM_REPEAT: "Напоминание: подтвердите договорённости",
    NotificationType.DUE_TOMORROW: "Завтра срок: {what}",
    NotificationType.HOST_RESPONSE: "{person_name} ответил(а) по пункту «{what}»",
    NotificationType.ITEM_CHANGED: "Изменился пункт: {what}",
    NotificationType.ITEM_REASSIGNED: "Пункт передан другому: {what}",
    NotificationType.PASSWORD_RESET: "Сброс пароля в «Договорились»",
}
LEADS = {
    NotificationType.SUMMARY: "Итоги встречи «{series_title}». Ведущий: {host_name}.",
    NotificationType.CONFIRM_REQUEST: "{host_name} просит подтвердить договорённость.",
    NotificationType.CONFIRM_REPEAT: "Вы ещё не подтвердили договорённости со встречи.",
    NotificationType.DUE_TOMORROW: "Завтра срок по договорённости.",
    NotificationType.ITEM_CHANGED: "Ведущий изменил пункт.",
    NotificationType.ITEM_REASSIGNED: "Пункт передан другому исполнителю.",
    NotificationType.PASSWORD_RESET: "Чтобы задать новый пароль, откройте ссылку.",
}


@dataclass
class Rendered:
    subject: str
    text: str
    html: str


TokenFor = Callable[[str], str]


def _url(token_for: TokenFor, payload: dict[str, Any], query: str = "") -> str:
    token = token_for(payload["link_id"])
    return f"{get_settings().public_base_url.rstrip('/')}/p/{token}{query}"


def _subject(text: str) -> str:
    return " ".join(text.split())[:SUBJECT_MAX]


def _date(value: str | None) -> str:
    return date.fromisoformat(value).strftime("%d.%m.%Y") if value else "срока нет"


def _what(payload: dict[str, Any]) -> str:
    item = payload.get("item") or {}
    return item.get("what") or payload.get("what") or "пункт"


def _item_line(item: dict[str, Any]) -> str:
    who = item.get("assignee_name")
    return f"{item['what']} — {who + ', ' if who else ''}{_date(item.get('due_date'))}"


def _body(token_for: TokenFor, kind: NotificationType, payload: dict[str, Any]) -> list[str]:
    lead = LEADS.get(kind, "")
    lines = [lead.format(**{"series_title": "", "host_name": "", **payload})] if lead else []
    if kind == NotificationType.HOST_RESPONSE:
        what = (
            f"предлагает новый срок {_date(payload.get('due_date'))}"
            if payload["kind"] == "reschedule"
            else "считает, что пункт не его"
        )
        lines.append(f"{payload['person_name']} {what}: {_what(payload)}.")
        if payload.get("comment"):
            lines.append(f"Комментарий: {payload['comment']}")
        lines.append(f"Открыть пункт: {payload['card_url']}")
    elif kind == NotificationType.PASSWORD_RESET:
        lines.append(payload["url"])
    elif kind == NotificationType.ITEM_CHANGED:
        for field, change in (payload.get("changes") or {}).items():
            lines.append(f"{field}: {change['from']} → {change['to']}")
    else:
        for item in payload.get("items") or [payload.get("item") or {"what": _what(payload)}]:
            lines.append(("→ " if item.get("mine") else "") + _item_line(item))
        if payload.get("link_id") and not payload.get("buttons"):
            lines.append(f"Открыть страницу: {_url(token_for, payload)}")
    return lines


def render(
    parts: list[tuple[NotificationType, dict[str, Any]]],
    issue_token: TokenFor | None = None,
) -> Rendered:
    """Одно письмо из одной или нескольких слитых строк (BR-16) либо дайджест (BR-11). На каждую
    ссылку выпускается свой токен, `issue_token(link_id)` обязан сохранить его хеш.
    """
    issued: dict[str, str] = {}
    make = issue_token or (lambda _link_id: new_token())

    def token_for(link_id: str) -> str:
        if link_id not in issued:
            issued[link_id] = make(link_id)
        return issued[link_id]

    kind, payload = parts[0]
    first = {**payload, "what": _what(payload)}
    subject = SUBJECTS[kind].format(**{"series_title": "", "person_name": "", **first})
    if len(parts) > 1:
        subject = f"{subject} и ещё {len(parts) - 1}"
    text: list[str] = []
    html: list[str] = []
    for kind, payload in parts:
        body = _body(token_for, kind, payload)
        buttons = payload.get("buttons") or []
        names = {
            i["id"]: i["what"]
            for i in payload.get("items") or [payload.get("item") or {}]
            if "id" in i and "what" in i
        }
        links = [
            (
                f"{b['label']} — {names[b['item_id']]}" if b["item_id"] in names else b["label"],
                _url(token_for, payload, b["query"]),
            )
            for b in buttons
        ]
        text.extend([*body, *(f"{label}: {url}" for label, url in links), ""])
        html.extend(f"<p>{escape(line)}</p>" for line in body)
        html.extend(f'<p><a href="{escape(url)}">{escape(label)}</a></p>' for label, url in links)
    return Rendered(
        _subject(subject), "\n".join(text).strip(), f"<html><body>{''.join(html)}</body></html>"
    )
