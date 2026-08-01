"""Password hashing and JWT issuing/verification."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.config import settings

# bcrypt silently truncates at 72 bytes; reject longer input instead.
MAX_PASSWORD_BYTES = 72


class TokenError(Exception):
    """Raised when a token is malformed, expired or has the wrong purpose."""


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    *,
    practitioner_id: uuid.UUID | str,
    firm_id: uuid.UUID | str,
    role: str,
    expires_minutes: int | None = None,
) -> str:
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=expires_minutes or settings.access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": str(practitioner_id),
        "firm_id": str(firm_id),
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_magic_link_token(*, client_id: uuid.UUID | str, firm_id: uuid.UUID | str) -> str:
    """Passwordless token for the client portal (no login required).

    Carries ``iat_ms`` alongside the standard ``iat``: revocation compares the
    issue time against a cut-off instant, and one-second resolution would let a
    link minted in the same second as the revocation survive it.
    """
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=settings.magic_link_expire_minutes)
    payload = {
        "sub": str(client_id),
        "firm_id": str(firm_id),
        "type": "magic_link",
        "iat": int(now.timestamp()),
        "iat_ms": int(now.timestamp() * 1000),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str, *, expected_type: str = "access") -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("Could not validate token") from exc

    if payload.get("type") != expected_type:
        raise TokenError("Unexpected token type")
    return payload
