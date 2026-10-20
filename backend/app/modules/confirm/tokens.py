import base64
import hashlib
import secrets

TOKEN_BYTES = 32


def new_token() -> str:
    """32 случайных байта в base64url без заполнения, всего 43 символа."""
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode()


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()
