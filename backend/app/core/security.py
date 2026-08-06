"""Password hashing and JWT issuing/verification."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
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


@lru_cache(maxsize=1)
def _absent_account_hash() -> str:
    """A real hash, of a secret nobody holds, to check against when there is no account.

    Built once on first use and at the same cost factor as a real password, so
    checking against it costs what checking a real one costs.
    """
    return bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Check a password. ``None`` means no such account — and costs the same.

    bcrypt is slow on purpose, about a quarter of a second here, and skipping
    it when the email matched nobody makes the two failures tell themselves
    apart by the clock: a wrong password takes 250ms, an address with no
    account behind it comes back in a handful. That gap answers "is this firm a
    customer" for anyone holding a list of emails and a stopwatch, and it holds
    however carefully the status code and response body are kept identical.

    So an absent account is checked too, against a hash of a random secret that
    no password will ever match. The clock then says nothing either way.

    This lives here rather than in the sign-in route because a caller should
    not be able to skip it by accident: the only way to ask the question is to
    spend the same time on the answer.
    """
    if password_hash is None:
        password_hash = _absent_account_hash()
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
    """A practitioner's bearer token.

    Carries ``iat_ms`` alongside the standard ``iat`` for the reason
    :func:`create_magic_link_token` does, and now for a second party: changing a
    password ends every session opened before it, and that comparison is against
    a cut-off instant. One-second resolution would let a token minted in the
    same second as the change outlive it — which is the one second that matters,
    because a self-service change mints its replacement immediately after
    stamping the cut-off.
    """
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=expires_minutes or settings.access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": str(practitioner_id),
        "firm_id": str(firm_id),
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "iat_ms": int(now.timestamp() * 1000),
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


# ------------------------------------------------ revoking without a blacklist --
# Two credentials are killed the same way: a cut-off instant on the row the
# token speaks for, and a token accepted only if it was minted at or after it.
# ``Client.portal_token_valid_from`` does it for magic links and
# ``Practitioner.credentials_valid_from`` for sign-in sessions. The comparison
# is the same both times, so it is written once.


def issued_at_ms(payload: dict[str, Any]) -> int | None:
    """A token's issue time in milliseconds, tolerating one minted before ``iat_ms``."""
    if (precise := payload.get("iat_ms")) is not None:
        return int(precise)
    if (seconds := payload.get("iat")) is not None:
        # Whole-second tokens fail closed inside the revocation second.
        return int(seconds) * 1000
    return None


def issued_after(cutoff: datetime | None, minted_at_ms: int | None) -> bool:
    """Was a token minted at ``minted_at_ms`` issued at or after ``cutoff``?

    No cut-off means nothing has been revoked, so every token passes. A cut-off
    with no issue time to compare fails closed: a token that cannot say when it
    was minted cannot be shown to postdate the revocation.
    """
    if cutoff is None:
        return True
    if minted_at_ms is None:
        return False
    if cutoff.tzinfo is None:  # SQLite hands back naive datetimes
        cutoff = cutoff.replace(tzinfo=UTC)
    return minted_at_ms >= int(cutoff.timestamp() * 1000)
