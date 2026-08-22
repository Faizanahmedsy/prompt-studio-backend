import hashlib
import hmac
import re
import secrets
import uuid
from datetime import timedelta
from typing import Any

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher

from app.core.config import settings
from app.core.constants import TokenType
from app.core.exceptions import ValidationError
from app.core.messages import ErrorMessage
from app.core.time import now

_password_hash = PasswordHash((BcryptHasher(rounds=settings.BCRYPT_ROUNDS),))

# bcrypt silently truncates at 72 BYTES. Left unchecked, two different long
# passwords that share a 72-byte prefix verify against each other — so the limit
# is enforced up front rather than discovered as an authentication bug.
BCRYPT_MAX_BYTES = 72
MIN_PASSWORD_LENGTH = 8

_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT = re.compile(r"\d")


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _password_hash.verify(plain, hashed)
    except ValueError:
        # A malformed stored hash must read as "wrong password", never a 500.
        return False


def assert_password_strong(password: str) -> None:
    """Reject passwords the product will not accept. Raises `ValidationError`.

    Checked in the service layer rather than as a pydantic constraint because
    four different routes accept a new password (register, change, reset, admin
    create) and a rule that lives on one schema is a rule the other three forgot.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(ErrorMessage.PASSWORD_TOO_WEAK)
    if len(password.encode()) > BCRYPT_MAX_BYTES:
        raise ValidationError(
            f"Password must be at most {BCRYPT_MAX_BYTES} bytes (about 72 characters)"
        )
    if not _HAS_LETTER.search(password) or not _HAS_DIGIT.search(password):
        raise ValidationError(ErrorMessage.PASSWORD_TOO_WEAK)


def _create_token(claims: dict[str, Any], expires_delta: timedelta, token_type: TokenType) -> str:
    issued = now()
    payload: dict[str, Any] = {
        **claims,
        "type": token_type.value,
        "jti": uuid.uuid4().hex,  # unique token id, used for revocation
        "iat": issued,
        "exp": issued + expires_delta,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_access_token(claims: dict[str, Any]) -> str:
    """Short-lived token carrying identity claims (user_id, name, role)."""
    return _create_token(
        claims, timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES), TokenType.ACCESS
    )


def create_refresh_token(user_id: str) -> str:
    return _create_token(
        {"user_id": user_id}, timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS), TokenType.REFRESH
    )


def create_reset_token(user_id: str) -> str:
    return _create_token(
        {"user_id": user_id},
        timedelta(minutes=settings.RESET_TOKEN_EXPIRE_MINUTES),
        TokenType.RESET,
    )


def create_verify_token(user_id: str, email: str) -> str:
    """Email-confirmation token. Carries the address so changing it invalidates."""
    return _create_token({"user_id": user_id, "email": email}, timedelta(days=3), TokenType.VERIFY)


def create_invite_token(project_id: str, email: str) -> str:
    return _create_token(
        {"project_id": project_id, "email": email},
        timedelta(days=settings.INVITE_TOKEN_EXPIRE_DAYS),
        TokenType.INVITE,
    )


def decode_token(token: str) -> dict[str, Any]:
    payload: dict[str, Any] = jwt.decode(
        token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    return payload


def new_opaque_token() -> str:
    """A refresh-session secret that is never a JWT — 43 random URL-safe chars."""
    return secrets.token_urlsafe(32)


def fingerprint(value: str) -> str:
    """Stable, non-reversible id for a token.

    Used to store refresh tokens: the database keeps the fingerprint, so a dump
    of the `sessions` table hands an attacker nothing they can present. Plain
    SHA-256 rather than bcrypt because the input is already 256 bits of entropy
    — there is no dictionary to slow down, and a lookup has to be an index hit.
    """
    return hashlib.sha256(value.encode()).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
