"""Magic-link issuing and revocation for the client portal.

Clients never get a password. The firm issues a signed, time-limited link; the
portal exchanges it for read access to that client's filings plus the ability
to upload the documents their filings are waiting on.

Revocation without a token blacklist: ``Client.portal_token_valid_from`` is a
cut-off instant. Setting it to "now" invalidates every link already issued,
because a token is only accepted when it was issued at or after the cut-off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from app.config import settings
from app.core.security import create_magic_link_token
from app.models.client import Client


@dataclass(frozen=True)
class MagicLink:
    token: str
    url: str
    expires_at: datetime


def issue_magic_link(client: Client) -> MagicLink:
    token = create_magic_link_token(client_id=client.id, firm_id=client.firm_id)
    separator = "&" if "?" in settings.portal_base_url else "?"
    return MagicLink(
        token=token,
        url=f"{settings.portal_base_url}{separator}{urlencode({'token': token})}",
        expires_at=datetime.now(UTC)
        + timedelta(minutes=settings.magic_link_expire_minutes),
    )


def revoke_portal_access(client: Client, *, at: datetime | None = None) -> datetime:
    """Kill every link issued so far. New links keep working."""
    cutoff = at or datetime.now(UTC)
    client.portal_token_valid_from = cutoff
    return cutoff


def token_is_current(client: Client, issued_at_ms: int | None) -> bool:
    """Was a token issued at ``issued_at_ms`` minted after the revocation cut-off?

    Millisecond resolution matters: with whole seconds, a link issued in the
    same second as a revocation would slip through.
    """
    if client.portal_token_valid_from is None:
        return True
    if issued_at_ms is None:
        return False
    cutoff = client.portal_token_valid_from
    if cutoff.tzinfo is None:  # SQLite hands back naive datetimes
        cutoff = cutoff.replace(tzinfo=UTC)
    return issued_at_ms >= int(cutoff.timestamp() * 1000)


def issued_at_ms(payload: dict) -> int | None:
    """Issue time in milliseconds, tolerating a token that predates ``iat_ms``."""
    if (precise := payload.get("iat_ms")) is not None:
        return int(precise)
    if (seconds := payload.get("iat")) is not None:
        # Whole-second tokens fail closed inside the revocation second.
        return int(seconds) * 1000
    return None
