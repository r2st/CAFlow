"""Firm registration, login and practitioner management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select

from app.api.deps import CurrentFirm, CurrentPractitioner, DbSession, FirmAdmin
from app.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.models.base import PractitionerRole
from app.models.firm import Firm, Practitioner
from app.schemas.auth import (
    FirmOut,
    FirmRegisterRequest,
    LoginRequest,
    PractitionerCreate,
    PractitionerOut,
    PractitionerUpdate,
    RegisterResponse,
    TokenResponse,
)
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(practitioner: Practitioner, firm: Firm) -> TokenResponse:
    token = create_access_token(
        practitioner_id=practitioner.id, firm_id=firm.id, role=practitioner.role
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
        practitioner=PractitionerOut.model_validate(practitioner),
        firm=FirmOut.model_validate(firm),
    )


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
def register_firm(payload: FirmRegisterRequest, request: Request, db: DbSession):
    """Create a firm together with its owner practitioner."""
    existing = db.scalar(
        select(Practitioner).where(func.lower(Practitioner.email) == payload.owner_email.lower())
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A practitioner with this email already exists",
        )

    firm = Firm(
        name=payload.firm_name,
        icai_registration_number=payload.icai_registration_number,
        email=payload.firm_email.lower(),
        phone=payload.firm_phone,
        pan=payload.pan,
        gstin=payload.gstin,
        city=payload.city,
        state=payload.state,
        plan=payload.plan,
    )
    db.add(firm)
    db.flush()

    owner = Practitioner(
        firm_id=firm.id,
        full_name=payload.owner_full_name,
        email=payload.owner_email.lower(),
        password_hash=hash_password(payload.owner_password),
        role=PractitionerRole.OWNER,
        membership_number=payload.owner_membership_number,
    )
    db.add(owner)
    db.flush()

    audit.record(
        db,
        action="firm.register",
        entity_type="firm",
        entity_id=firm.id,
        actor=owner,
        summary=f"Firm {firm.name} registered on the {firm.plan.value} plan",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    db.refresh(firm)
    db.refresh(owner)
    return _token_response(owner, firm)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: DbSession):
    practitioner = db.scalar(
        select(Practitioner).where(func.lower(Practitioner.email) == payload.email.lower())
    )
    # Constant-ish work either way: never reveal whether the email exists.
    if practitioner is None or not verify_password(payload.password, practitioner.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    if not practitioner.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account has been deactivated"
        )

    firm = db.get(Firm, practitioner.firm_id)
    if firm is None or not firm.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Firm is not active")

    practitioner.last_login_at = datetime.now(UTC)
    audit.record(
        db,
        action="auth.login",
        entity_type="practitioner",
        entity_id=practitioner.id,
        actor=practitioner,
        summary=f"{practitioner.email} signed in",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    db.refresh(practitioner)
    return _token_response(practitioner, firm)


@router.get("/me", response_model=PractitionerOut)
def read_me(practitioner: CurrentPractitioner):
    return practitioner


@router.get("/firm", response_model=FirmOut)
def read_firm(firm: CurrentFirm):
    return firm


@router.get("/practitioners", response_model=list[PractitionerOut])
def list_practitioners(practitioner: CurrentPractitioner, db: DbSession):
    stmt = (
        select(Practitioner)
        .where(Practitioner.firm_id == practitioner.firm_id)
        .order_by(Practitioner.created_at)
    )
    return list(db.scalars(stmt).all())


@router.post(
    "/practitioners", response_model=PractitionerOut, status_code=status.HTTP_201_CREATED
)
def add_practitioner(
    payload: PractitionerCreate, admin: FirmAdmin, firm: CurrentFirm, db: DbSession
):
    """Add a team member. Restricted to owners and partners."""
    if payload.role == PractitionerRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="A firm can only have one owner"
        )

    limit = firm.user_limit
    if limit is not None:
        current = db.scalar(
            select(func.count(Practitioner.id)).where(
                Practitioner.firm_id == firm.id, Practitioner.is_active.is_(True)
            )
        )
        if current >= limit:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"The {firm.plan.value} plan allows {limit} user(s). Upgrade to add more.",
            )

    duplicate = db.scalar(
        select(Practitioner).where(
            Practitioner.firm_id == firm.id,
            func.lower(Practitioner.email) == payload.email.lower(),
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A practitioner with this email already exists in the firm",
        )

    practitioner = Practitioner(
        firm_id=firm.id,
        full_name=payload.full_name,
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        role=payload.role,
        phone=payload.phone,
        membership_number=payload.membership_number,
    )
    db.add(practitioner)
    db.flush()

    audit.record(
        db,
        action="practitioner.create",
        entity_type="practitioner",
        entity_id=practitioner.id,
        actor=admin,
        summary=f"Added {practitioner.email} as {practitioner.role.value}",
    )
    db.commit()
    db.refresh(practitioner)
    return practitioner


@router.patch("/practitioners/{practitioner_id}", response_model=PractitionerOut)
def update_practitioner(
    practitioner_id: uuid.UUID, payload: PractitionerUpdate, admin: FirmAdmin, db: DbSession
):
    target = db.get(Practitioner, practitioner_id)
    if target is None or target.firm_id != admin.firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Practitioner not found")
    if target.role == PractitionerRole.OWNER and target.id != admin.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="The firm owner cannot be modified"
        )

    updates = payload.model_dump(exclude_unset=True)
    if updates.get("role") == PractitionerRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="A firm can only have one owner"
        )

    before = {key: getattr(target, key) for key in updates}
    for key, value in updates.items():
        setattr(target, key, value)

    audit.record(
        db,
        action="practitioner.update",
        entity_type="practitioner",
        entity_id=target.id,
        actor=admin,
        summary=f"Updated {target.email}",
        changes=audit.diff(before, updates),
    )
    db.commit()
    db.refresh(target)
    return target
