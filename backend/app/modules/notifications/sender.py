import logging
import smtplib
import uuid
from collections import defaultdict
from datetime import datetime, time, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.core.business_days import add_business_days
from app.core.config import get_settings
from app.modules.analytics import service as analytics_service
from app.modules.auth.models import User
from app.modules.confirm import service as confirm_service
from app.modules.items.models import Item, ItemStatus
from app.modules.notifications import content, repository
from app.modules.notifications.models import Notification, NotificationStatus, NotificationType
from app.modules.notifications.render import render
from app.modules.series.models import Meeting, Person, Series

logger = logging.getLogger(__name__)

CLAIM_BATCH = 50
STUCK_AFTER = timedelta(minutes=10)
RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=60))
WEEK = timedelta(days=7)
WEEKLY_LIMIT = 3
POSTPONED_HOUR = 9
SMTP_TIMEOUT_SEC = 10
SMTPS_PORT = 465
FROM_NAME = "Договорились"
STALE_STATUSES = {
    NotificationType.CONFIRM_REQUEST: (
        ItemStatus.ACCEPTED,
        ItemStatus.DISPUTED,
        ItemStatus.DONE,
        ItemStatus.CANCELLED,
    ),
    NotificationType.DUE_TOMORROW: (ItemStatus.DISPUTED, ItemStatus.DONE, ItemStatus.CANCELLED),
}
CLOSED = (ItemStatus.DONE, ItemStatus.CANCELLED)


class MailError(Exception):
    pass


class TransientMailError(MailError):
    """Временная ошибка отправки: 4xx или таймаут, письмо повторяется (BR-17)."""


class PermanentMailError(MailError):
    """Постоянная ошибка отправки: 5xx или нет адреса, письмо сразу помечается неудачным (BR-17)."""


def _recipient(db: Session, mail: Notification) -> str:
    owner = (
        db.get(Person, mail.recipient_person_id)
        if mail.recipient_person_id
        else db.get(User, mail.recipient_user_id)
    )
    if owner is None or not owner.email:
        raise PermanentMailError("нет адреса получателя")
    return str(owner.email)


def _sender_identity(db: Session, mail: Notification) -> tuple[str, str | None]:
    if mail.meeting_id is None or mail.type == NotificationType.HOST_RESPONSE:
        return FROM_NAME, None
    host = db.scalar(
        select(User)
        .join(Series, Series.owner_user_id == User.id)
        .join(Meeting, Meeting.series_id == Series.id)
        .where(Meeting.id == mail.meeting_id)
    )
    return (f"{host.full_name} через {FROM_NAME}", str(host.email)) if host else (FROM_NAME, None)


def deliver(mail: Notification) -> None:
    """Единственный выход к Postbox/SMTP. Без SMTP_HOST письмо только пишется в лог."""
    db = object_session(mail)
    assert db is not None
    address = _recipient(db, mail)
    settings = get_settings()
    if not settings.smtp_host:
        logger.info("mail %s to %s (SMTP_HOST is empty, not sent)", mail.type.value, address)
        return
    letter = render(
        getattr(mail, "letter_parts", None) or [(mail.type, mail.payload or {})],
        lambda link_id: confirm_service.issue_token(db, uuid.UUID(link_id)),
    )
    db.commit()  # хеши токенов сохранены до SMTP: письмо не уйдёт с мёртвой ссылкой
    name, reply_to = _sender_identity(db, mail)
    message = EmailMessage()
    message["Subject"] = letter.subject
    message["From"] = formataddr((name, settings.mail_from))
    message["To"] = address
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(letter.text)
    message.add_alternative(letter.html, subtype="html")
    smtp_class = smtplib.SMTP_SSL if settings.smtp_port == SMTPS_PORT else smtplib.SMTP
    try:
        with smtp_class(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SEC) as smtp:
            if smtp_class is smtplib.SMTP and smtp.has_extn("starttls"):
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    except smtplib.SMTPRecipientsRefused as exc:
        raise PermanentMailError(f"адрес отклонён: {exc.args[0]}") from exc
    except smtplib.SMTPResponseException as exc:
        error = PermanentMailError if exc.smtp_code >= 500 else TransientMailError
        raise error(f"{exc.smtp_code} {exc.smtp_error!r}") from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise TransientMailError(str(exc)) from exc


def _cancel(mail: Notification) -> bool:
    mail.status = NotificationStatus.CANCELLED
    return False


def _prepare(db: Session, mail: Notification, now: datetime) -> bool:
    """Условие проверяется заново в момент отправки; False - письмо отменено (BR-16)."""
    item = db.get(Item, mail.item_id) if mail.item_id else None
    stale = STALE_STATUSES.get(mail.type, CLOSED)
    if item is not None and mail.type != NotificationType.HOST_RESPONSE and item.status in stale:
        return _cancel(mail)
    person_id = mail.recipient_person_id
    if mail.type == NotificationType.CONFIRM_REPEAT:
        assert person_id and mail.meeting_id
        pending = content.pending_of(db, mail.meeting_id, person_id)
        if not pending:
            return _cancel(mail)
        link_id = confirm_service.ensure_link(db, person_id, mail.meeting_id, now).id
        mail.payload = content.confirm_repeat(db, mail.meeting_id, pending, link_id)
    elif mail.payload is None and item is not None and person_id and mail.meeting_id:
        link_id = confirm_service.ensure_link(db, person_id, mail.meeting_id, now).id
        if mail.type == NotificationType.CONFIRM_REQUEST:
            mail.payload = content.confirm_request(db, item, link_id)
        elif mail.type == NotificationType.DUE_TOMORROW:
            mail.payload = content.due_tomorrow(db, item, link_id)
    return True


def _series_id(db: Session, mail: Notification) -> uuid.UUID | None:
    meeting = db.get(Meeting, mail.meeting_id) if mail.meeting_id else None
    return meeting.series_id if meeting else None


def _postpone(db: Session, series_id: uuid.UUID, group: list[Notification], now: datetime) -> None:
    series = db.get(Series, series_id)
    assert series is not None
    zone = ZoneInfo(series.timezone)
    day = add_business_days(now.astimezone(zone).date(), 1)
    at = datetime.combine(day, time(POSTPONED_HOUR), zone)
    for mail in group:
        mail.status = NotificationStatus.QUEUED
        mail.claimed_at = None
        mail.scheduled_for = at
        mail.postponed = True


def _fail_or_retry(mail: Notification, exc: Exception, now: datetime, *, transient: bool) -> None:
    mail.attempts += 1
    mail.last_error = str(exc)[:500]
    mail.claimed_at = None
    if transient and mail.attempts <= len(RETRY_DELAYS):
        mail.status = NotificationStatus.QUEUED
        mail.scheduled_for = now + RETRY_DELAYS[mail.attempts - 1]
    else:
        mail.status = NotificationStatus.FAILED


def _send_group(db: Session, group: list[Notification], now: datetime) -> bool:
    first = group[0]
    first.letter_parts = [(m.type, m.payload or {}) for m in group]  # type: ignore[attr-defined]
    try:
        deliver(first)
    except PermanentMailError as exc:
        for mail in group:
            _fail_or_retry(mail, exc, now, transient=False)
        return False
    except Exception as exc:
        if not isinstance(exc, TransientMailError):
            logger.error("mail delivery crashed: %s", type(exc).__name__, exc_info=True)
        for mail in group:
            _fail_or_retry(mail, exc, now, transient=True)
        return False
    for mail in group:
        mail.status = NotificationStatus.SENT
        mail.sent_at = now
        mail.claimed_at = None
        mail.attempts += 1
        if mail.type == NotificationType.DUE_TOMORROW and mail.item_id and mail.meeting_id:
            _track_reminder(db, mail)
    return True


def _track_reminder(db: Session, mail: Notification) -> None:
    analytics_service.track(
        db,
        "reminder_sent",
        series_id=_series_id(db, mail),
        meeting_id=mail.meeting_id,
        item_id=mail.item_id,
    )


def send_due(db: Session, now: datetime) -> int:
    """Задача Beat.

    Созревшие строки захватываем (`sending`) и коммитим, так что почтовый сервер не вызывается под
    замком строки; каждое письмо коммитится отдельно. Строки одного типа для одного получателя
    уходят одним письмом (BR-16). Четвёртое письмо за неделю по одной серии откладывается, а
    отложенные позже уходят вместе одним (BR-11). Возвращает число строк, помеченных
    отправленными.
    """
    repository.release_stuck(db, now - STUCK_AFTER)
    claimed = repository.claim_due(db, now, CLAIM_BATCH)
    db.commit()
    letters: dict[tuple, list[Notification]] = defaultdict(list)
    for mail in claimed:
        if not _prepare(db, mail, now):
            continue
        series_id = _series_id(db, mail)
        person = mail.recipient_person_id
        if person and series_id and mail.type != NotificationType.HOST_RESPONSE:
            key = (
                ("digest", person, series_id)
                if mail.postponed
                else (mail.type, person, mail.meeting_id)
            )
        else:
            key = (mail.type, mail.recipient_user_id, mail.meeting_id)
        letters[key].append(mail)
    db.commit()
    used: dict[tuple, int] = {}
    sent = 0
    ordered = sorted(letters.items(), key=lambda kv: kv[0][0] != "digest")
    for key, group in ordered:
        mine = repository.still_claimed(db, [m.id for m in group], now)
        group = [m for m in group if m.id in mine]
        if not group:
            continue
        person = group[0].recipient_person_id
        series_id = _series_id(db, group[0])
        limited = bool(person and series_id and group[0].type != NotificationType.HOST_RESPONSE)
        if limited:
            assert person and series_id
            count = used.setdefault(
                (person, series_id),
                repository.sent_count(db, person, series_id, now - WEEK, now),
            )
            if key[0] != "digest" and count >= WEEKLY_LIMIT:
                _postpone(db, series_id, group, now)
                db.commit()
                continue
        if _send_group(db, group, now):
            sent += len(group)
            if limited:
                used[(person, series_id)] += 1
        db.commit()
    return sent
