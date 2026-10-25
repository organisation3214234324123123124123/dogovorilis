import uuid
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.modules.auth.models import LoginAttempt, PasswordResetToken, User


def get_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email))


def add(db: Session, user: User) -> User:
    db.add(user)
    db.flush()
    return user


def add_failed_login(db: Session, email: str, at: datetime) -> None:
    db.add(LoginAttempt(email=email, attempted_at=at))
    db.flush()


def lock_email(db: Session, email: str) -> None:
    """Замок на e-mail до конца транзакции: параллельные входы не обгонят счётчик."""
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(email.lower(), 0))))


def failed_logins_since(db: Session, email: str, since: datetime) -> list[datetime]:
    return list(
        db.scalars(
            select(LoginAttempt.attempted_at)
            .where(LoginAttempt.email == email, LoginAttempt.attempted_at > since)
            .order_by(LoginAttempt.attempted_at)
        )
    )


def delete_failed_logins_before(db: Session, before: datetime) -> int:
    result = db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < before))
    return result.rowcount


def clear_failed_logins(db: Session, email: str) -> None:
    db.execute(delete(LoginAttempt).where(LoginAttempt.email == email))


def add_reset_token(db: Session, token: PasswordResetToken) -> PasswordResetToken:
    db.add(token)
    db.flush()
    return token


def use_reset_token(db: Session, token_hash: str, now: datetime) -> PasswordResetToken | None:
    """Один UPDATE ... RETURNING: из двух одновременных запросов токен получит только первый."""
    return db.scalar(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(PasswordResetToken)
    )


def expire_reset_tokens(db: Session, user_id: uuid.UUID, now: datetime) -> None:
    db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    )
