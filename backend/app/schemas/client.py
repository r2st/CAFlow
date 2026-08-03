"""Client schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import EmailStr, Field, field_validator

from app.models.base import EntityType, GSTFilingFrequency
from app.schemas.common import (
    ORMModel,
    SanitizedModel,
    ServiceFees,
    validate_gstin,
    validate_pan,
    validate_tan,
)


class ClientBase(SanitizedModel):
    name: str = Field(min_length=2, max_length=255)
    entity_type: EntityType = EntityType.INDIVIDUAL
    pan: str | None = None
    gstin: str | None = None
    tan: str | None = None
    cin: str | None = Field(default=None, max_length=21)

    contact_person: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=20)
    whatsapp: str | None = Field(default=None, max_length=20)
    address: str | None = None
    state: str | None = Field(default=None, max_length=100)

    gst_registered: bool = False
    gst_filing_frequency: GSTFilingFrequency = GSTFilingFrequency.MONTHLY
    tds_applicable: bool = False
    income_tax_applicable: bool = True
    tax_audit_applicable: bool = False
    roc_applicable: bool = False
    payroll_applicable: bool = False

    assigned_practitioner_id: uuid.UUID | None = None
    notes: str | None = None
    service_fees: ServiceFees = Field(default_factory=dict)

    _validate_pan = field_validator("pan")(validate_pan)
    _validate_gstin = field_validator("gstin")(validate_gstin)
    _validate_tan = field_validator("tan")(validate_tan)


class ClientCreate(ClientBase):
    onboarded_on: date | None = None
    # Set false to create the client without materialising compliance items.
    generate_compliance_items: bool = True


class ClientUpdate(SanitizedModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    entity_type: EntityType | None = None
    pan: str | None = None
    gstin: str | None = None
    tan: str | None = None
    cin: str | None = Field(default=None, max_length=21)

    contact_person: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=20)
    whatsapp: str | None = Field(default=None, max_length=20)
    address: str | None = None
    state: str | None = Field(default=None, max_length=100)

    gst_registered: bool | None = None
    gst_filing_frequency: GSTFilingFrequency | None = None
    tds_applicable: bool | None = None
    income_tax_applicable: bool | None = None
    tax_audit_applicable: bool | None = None
    roc_applicable: bool | None = None
    payroll_applicable: bool | None = None

    assigned_practitioner_id: uuid.UUID | None = None
    is_active: bool | None = None
    notes: str | None = None
    service_fees: ServiceFees | None = None

    _validate_pan = field_validator("pan")(validate_pan)
    _validate_gstin = field_validator("gstin")(validate_gstin)
    _validate_tan = field_validator("tan")(validate_tan)


class ClientOut(ORMModel):
    id: uuid.UUID
    firm_id: uuid.UUID
    assigned_practitioner_id: uuid.UUID | None
    name: str
    entity_type: EntityType
    pan: str | None
    gstin: str | None
    tan: str | None
    cin: str | None
    contact_person: str | None
    email: EmailStr | None
    phone: str | None
    whatsapp: str | None
    address: str | None
    state: str | None
    gst_registered: bool
    gst_filing_frequency: GSTFilingFrequency
    tds_applicable: bool
    income_tax_applicable: bool
    tax_audit_applicable: bool
    roc_applicable: bool
    payroll_applicable: bool
    onboarded_on: date
    is_active: bool
    notes: str | None
    service_fees: dict
    created_at: datetime
    updated_at: datetime


class ClientCreateResponse(SanitizedModel):
    client: ClientOut
    compliance_items_created: int


class ClientComplianceSummary(SanitizedModel):
    total: int
    pending: int
    overdue: int
    due_soon: int
    filed: int


class ClientDetailOut(ClientOut):
    assigned_practitioner_name: str | None = None
    compliance_summary: ClientComplianceSummary
