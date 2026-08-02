"""Shared FastAPI dependencies: authentication, tenancy and role checks."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import TokenError, decode_token
from app.database import get_db
from app.models.base import PractitionerRole
from app.models.client import Client
from app.models.firm import Firm, Practitioner
from app.services.portal import issued_at_ms, token_is_current

# Two schemes over the same header, so the reference shows which token each
# endpoint wants. auto_error is off because a missing header should produce
# our own error envelope rather than Starlette's bare 403.
bearer_scheme = HTTPBearer(
    scheme_name="PractitionerToken",
    bearerFormat="JWT",
    description=(
        "A practitioner access token from `POST /auth/login` or "
        "`POST /auth/register`. Scoped to the practitioner's firm."
    ),
    auto_error=False,
)

portal_scheme = HTTPBearer(
    scheme_name="PortalMagicLink",
    bearerFormat="JWT",
    description=(
        "A client magic-link token from `POST /clients/{client_id}/portal-link`. "
        "Reaches only `/portal/*`, and only for the one client it was issued for."
    ),
    auto_error=False,
)

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

PORTAL_LINK_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="This portal link is invalid or has expired. Ask your CA for a new one.",
    headers={"WWW-Authenticate": "Bearer"},
)

FIRM_INACTIVE_EXCEPTION = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN, detail="Firm is not active"
)


def _active_firm(db: Session, firm_id: uuid.UUID) -> Firm:
    """The firm, if it is still one this deployment serves.

    ``db.get`` reads the identity map first, so a request whose endpoint also
    declares :data:`CurrentFirm` pays for this once rather than twice.
    """
    firm = db.get(Firm, firm_id)
    if firm is None or not firm.is_active:
        raise FIRM_INACTIVE_EXCEPTION
    return firm


def get_current_practitioner(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> Practitioner:
    if credentials is None:
        raise CREDENTIALS_EXCEPTION
    try:
        payload = decode_token(credentials.credentials)
        practitioner_id = uuid.UUID(payload["sub"])
    except (TokenError, KeyError, ValueError) as exc:
        raise CREDENTIALS_EXCEPTION from exc

    practitioner = db.get(Practitioner, practitioner_id)
    if practitioner is None or not practitioner.is_active:
        raise CREDENTIALS_EXCEPTION
    if str(practitioner.firm_id) != payload.get("firm_id"):
        raise CREDENTIALS_EXCEPTION
    # Checked here rather than only in ``get_current_firm``: an access token
    # lives twelve hours, and most endpoints have no reason to ask for the firm
    # object, so a firm deactivated at nine in the morning went on filing,
    # invoicing and granting portal links until the last token issued before it
    # expired. Sign-in already refuses — this is what makes the refusal apply
    # to credentials that were minted before the decision.
    _active_firm(db, practitioner.firm_id)
    return practitioner


CurrentPractitioner = Annotated[Practitioner, Depends(get_current_practitioner)]
DbSession = Annotated[Session, Depends(get_db)]


def get_current_firm(practitioner: CurrentPractitioner, db: DbSession) -> Firm:
    return _active_firm(db, practitioner.firm_id)


CurrentFirm = Annotated[Firm, Depends(get_current_firm)]


def get_portal_client(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(portal_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> Client:
    """Resolve the client behind a magic-link token.

    Deliberately separate from ``get_current_practitioner``: a portal token
    grants access to exactly one client's own records and nothing else, so it
    must never satisfy a practitioner-scoped dependency.
    """
    if credentials is None:
        raise PORTAL_LINK_EXCEPTION
    try:
        payload = decode_token(credentials.credentials, expected_type="magic_link")
        client_id = uuid.UUID(payload["sub"])
    except (TokenError, KeyError, ValueError) as exc:
        raise PORTAL_LINK_EXCEPTION from exc

    client = db.get(Client, client_id)
    if client is None or not client.is_active or not client.portal_enabled:
        raise PORTAL_LINK_EXCEPTION
    if str(client.firm_id) != payload.get("firm_id"):
        raise PORTAL_LINK_EXCEPTION
    if not token_is_current(client, issued_at_ms(payload)):
        raise PORTAL_LINK_EXCEPTION
    # A firm that is no longer served has no portal either. Reported as a dead
    # link rather than as the firm's status: the client is not the party this
    # decision was about, and "ask your CA" is the right next step regardless.
    firm = db.get(Firm, client.firm_id)
    if firm is None or not firm.is_active:
        raise PORTAL_LINK_EXCEPTION
    return client


PortalClient = Annotated[Client, Depends(get_portal_client)]


def require_roles(*roles: PractitionerRole) -> Callable[[Practitioner], Practitioner]:
    """Dependency factory restricting an endpoint to the given roles."""
    allowed = set(roles)

    def dependency(practitioner: CurrentPractitioner) -> Practitioner:
        if practitioner.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Insufficient permissions — requires one of: "
                    + ", ".join(sorted(r.value for r in allowed))
                ),
            )
        return practitioner

    return dependency


require_firm_admin = require_roles(PractitionerRole.OWNER, PractitionerRole.PARTNER)
require_manager = require_roles(
    PractitionerRole.OWNER, PractitionerRole.PARTNER, PractitionerRole.MANAGER
)

FirmAdmin = Annotated[Practitioner, Depends(require_firm_admin)]
Manager = Annotated[Practitioner, Depends(require_manager)]
