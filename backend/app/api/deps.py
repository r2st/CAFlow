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
from app.models.firm import Firm, Practitioner

bearer_scheme = HTTPBearer(auto_error=False)

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
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
