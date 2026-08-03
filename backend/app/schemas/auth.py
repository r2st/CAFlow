"""Auth, firm and practitioner schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.models.base import FirmPlan, PractitionerRole
from app.schemas.common import (
    ORMModel,
    Password,
    SanitizedModel,
    validate_gstin,
    validate_pan,
)


class FirmRegisterRequest(SanitizedModel):
    firm_name: str = Field(min_length=2, max_length=255)
    icai_registration_number: str | None = Field(default=None, max_length=64)
    firm_email: EmailStr
    firm_phone: str | None = Field(default=None, max_length=20)
    pan: str | None = None
    gstin: str | None = None
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    plan: FirmPlan = FirmPlan.SOLO

    # The first practitioner — becomes the firm owner.
    owner_full_name: str = Field(min_length=2, max_length=255)
    owner_email: EmailStr
    owner_password: Password
    owner_membership_number: str | None = Field(default=None, max_length=32)

    _validate_pan = field_validator("pan")(validate_pan)
    _validate_gstin = field_validator("gstin")(validate_gstin)


class LoginRequest(SanitizedModel):
    email: EmailStr
    # Not ``Password``: this one is being checked, not set. A password too long
    # for bcrypt cannot be the one on any account, and ``verify_password``
    # already turns it into the same 401 as any other wrong password — while
    # still spending the same time on it. A 422 here would answer faster than
    # a real attempt and say so.
    password: str = Field(min_length=1, max_length=72)


class PractitionerCreate(SanitizedModel):
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    password: Password
    role: PractitionerRole = PractitionerRole.JUNIOR
    phone: str | None = Field(default=None, max_length=20)
    membership_number: str | None = Field(default=None, max_length=32)


class PractitionerUpdate(SanitizedModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=255)
    role: PractitionerRole | None = None
    phone: str | None = Field(default=None, max_length=20)
    membership_number: str | None = Field(default=None, max_length=32)
    is_active: bool | None = None


class PractitionerOut(ORMModel):
    id: uuid.UUID
    firm_id: uuid.UUID
    full_name: str
    email: EmailStr
    phone: str | None
    membership_number: str | None
    role: PractitionerRole
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class FirmOut(ORMModel):
    id: uuid.UUID
    name: str
    icai_registration_number: str | None
    email: EmailStr
    phone: str | None
    pan: str | None
    gstin: str | None
    city: str | None
    state: str | None
    plan: FirmPlan
    is_active: bool
    created_at: datetime
    # Derived from the plan. Sent so the team screen can show what is left
    # before it is spent, rather than letting the firm discover the ceiling
    # by being refused at the point of adding someone. ``None`` is unlimited.
    user_limit: int | None
    client_limit: int | None


class TokenResponse(SanitizedModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    practitioner: PractitionerOut
    firm: FirmOut


class RegisterResponse(TokenResponse):
    pass
