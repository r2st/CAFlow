"""Auth, firm and practitioner schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.models.base import FirmPlan, PractitionerRole
from app.schemas.common import ORMModel, SanitizedModel, validate_gstin, validate_pan


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
    owner_password: str = Field(min_length=8, max_length=72)
    owner_membership_number: str | None = Field(default=None, max_length=32)

    _validate_pan = field_validator("pan")(validate_pan)
    _validate_gstin = field_validator("gstin")(validate_gstin)


class LoginRequest(SanitizedModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class PractitionerCreate(SanitizedModel):
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
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


class TokenResponse(SanitizedModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    practitioner: PractitionerOut
    firm: FirmOut


class RegisterResponse(TokenResponse):
    pass
