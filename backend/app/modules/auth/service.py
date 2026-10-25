import hashlib
import math
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.errors import Conflict, Gone, RateLimited, Unauthorized, ValidationFailed
from app.core.security import (
    SESSION_TTL_SECONDS,
    create_access_token,
    hash_password,
    verify_password,
)
from app.modules.auth import repository
from app.modules.auth.models import PasswordResetToken, User
from app.modules.auth.schemas import (
    LoginRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    RegisterRequest,
    TokenRead,
    UserRead,
)
from app.modules.notifications import service as notifications_service
from app.modules.series import service as series_service

# Время входа одинаково для неизвестных e-mail: так не видно, какие учётные записи существуют.
_DUMMY_HASH = hash_password("timing-equalizer")

MAX_FAILED_LOGINS = 10
LOCKOUT_WINDOW = timedelta(minutes=15)
RESET_TOKEN_TTL = timedelta(hours=1)
LOGIN_ATTEMPT_TTL = timedelta(minutes=30)
PASSWORD_RESET_PER_HOUR = 3


def login(db: Session, body: LoginRequest) -> TokenRead:
    now = datetime.now(UTC)
    repository.lock_email(db, body.email)
    _check_not_locked(db, body.email, now)
    user = repository.get_by_email(db, body.email)
    if not verify_password(body.password, user.password_hash if user else _DUMMY_HASH) or not user:
        repository.add_failed_login(db, body.email, now)
        # keep_changes: get_db сохраняет запись о неудаче, хотя запрос завершается 401 (FR-02)
        raise Unauthorized("Неверный e-mail или пароль", keep_changes=True)
    repository.clear_failed_logins(db, body.email)
    return TokenRead(
        access_token=create_access_token(user.id),
        expires_in=SESSION_TTL_SECONDS,
        user=UserRead.model_validate(user),
    )


def _lock_end(failures: list[datetime]) -> datetime | None:
    """Десятая неудача подряд внутри LOCKOUT_WINDOW блокирует вход; после окончания блокировки счёт
    идёт заново.
    """
    lock_end: datetime | None = None
    window: list[datetime] = []
    for at in failures:
        if lock_end is not None and at < lock_end:
            continue
        window = [w for w in window if w > at - LOCKOUT_WINDOW]
        window.append(at)
        if len(window) >= MAX_FAILED_LOGINS:
            lock_end = at + LOCKOUT_WINDOW
            window = []
    return lock_end


def _check_not_locked(db: Session, email: str, now: datetime) -> None:
    # Попытки старше двух окон не влияют на блокировку, которая ещё действует.
    failures = repository.failed_logins_since(db, email, now - 2 * LOCKOUT_WINDOW)
    unlock_at = _lock_end(failures)
    if unlock_at is None or unlock_at <= now:
        return
    raise RateLimited(
        "Слишком много неудачных попыток входа, попробуйте позже",
        retry_after=max(1, math.ceil((unlock_at - now).total_seconds())),
    )


def purge_login_attempts(db: Session, now: datetime) -> int:
    return repository.delete_failed_logins_before(db, now - LOGIN_ATTEMPT_TTL)


def register(db: Session, body: RegisterRequest) -> User:
    if not body.pd_consent:
        raise ValidationFailed("Нужно согласие на обработку персональных данных")
    if repository.get_by_email(db, body.email):
        raise Conflict("E-mail уже занят", code="EMAIL_TAKEN")
    user = repository.add(
        db,
        User(
            email=body.email,
            password_hash=hash_password(body.password),
            full_name=body.full_name,
            timezone=body.timezone,
            pd_consent_at=datetime.now(UTC),
        ),
    )
    series_service.create_own_organization(db, user)  # своя сторона ведущего (OQ-07)
    return user


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def request_password_reset(db: Session, body: PasswordResetRequest) -> None:
    user = repository.get_by_email(db, body.email)
    if user is None:
        return
    now = datetime.now(UTC)
    recent = notifications_service.count_recent_password_resets(
        db, user.id, now - timedelta(hours=1)
    )
    if recent >= PASSWORD_RESET_PER_HOUR:
        return
    token = secrets.token_urlsafe(32)
    reset = repository.add_reset_token(
        db,
        PasswordResetToken(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=_hash_token(token),
            expires_at=now + RESET_TOKEN_TTL,
        ),
    )
    notifications_service.queue_password_reset(db, user.id, reset.id, token, now, reset.expires_at)


def confirm_password_reset(db: Session, body: PasswordResetConfirm) -> None:
    now = datetime.now(UTC)
    token_hash = _hash_token(body.token)
    reset = repository.use_reset_token(db, token_hash, now)
    if reset is None:
        raise Gone("Ссылка недействительна или уже использована")
    user = db.get(User, reset.user_id)
    user.password_hash = hash_password(body.new_password)
    repository.expire_reset_tokens(db, user.id, now)
    repository.clear_failed_logins(db, user.email)
