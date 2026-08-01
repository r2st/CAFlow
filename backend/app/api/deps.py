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
    return practitioner


CurrentPractitioner = Annotated[Practitioner, Depends(get_current_practitioner)]
DbSession = Annotated[Session, Depends(get_db)]


def get_current_firm(practitioner: CurrentPractitioner, db: DbSession) -> Firm:
    firm = db.get(Firm, practitioner.firm_id)
    if firm is None or not firm.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Firm is not active")
    return firm


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
