"""Client schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import EmailStr, Field, field_validator

from app.config import settings
from app.core import clock
from app.core.periods import add_months
from app.models.base import EntityType, GSTFilingFrequency
from app.schemas.common import (
    ORMModel,
    SanitizedModel,
    ServiceFees,
    not_clearable,
    pan_matches_gstin,
    validate_gstin,
    validate_pan,
    validate_tan,
)


def validate_onboarded_on(value: date | None) -> date | None:
    """Refuse an onboarding date the calendar generator cannot reach.

    ``onboarded_on`` is not merely a note about when the engagement started —
    it is the *start* of the window compliance items are generated over.
    :func:`~app.services.compliance_generator.default_window` runs from it to
    ``compliance_generation_months`` past today, so a date beyond that horizon
    produces a window that runs backwards, and a backwards window materialises
    nothing.

    That was reported as success. Creating the client answered ``201`` with
    ``compliance_items_created: 0``, which reads exactly like a client who
    genuinely owes nothing — and nothing afterwards ever notices: the monthly
    top-up recomputes the same empty window, so the client sits on the books
    occupying a plan slot with no calendar at all. Not one filing is raised,
    no task, no reminder, and no fee. The firm finds out when the client asks
    why their return was not filed.

    A mistyped year is what produces it, which is the digit that gets mistyped
    in a date field, and it is the one typo whose result looks like a healthy
    client record.

    Dating an engagement a little ahead is real, so this is a horizon rather
    than a ban: a client taken on with effect from next month, or from the
    start of the new financial year, is inside it and generates the part of
    their calendar that falls in the window. The bound is the generator's own
    reach, because past it there is by definition nothing to generate.

    Back-dating stays open with no bound at all — picking up a client whose
    returns began years ago is ordinary, and the window simply starts at the
    generator's own three-month look-back instead.

    Today is today in India; see :mod:`app.core.clock`.
    """
    if value is None:
        return value
    horizon = add_months(clock.today(), settings.compliance_generation_months)
    if value > horizon:
        raise ValueError(
            f"onboarded_on cannot be later than {horizon:%d %b %Y} — the compliance "
            f"calendar is generated {settings.compliance_generation_months} months "
            "ahead, so a client onboarded after that would be created with no "
            "filings at all"
        )
    return value


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
    # Both are on the form, and a GSTIN carries a PAN inside it. See
    # :func:`~app.schemas.common.check_pan_gstin_agreement`.
    _pan_matches_gstin = pan_matches_gstin()


class ClientCreate(ClientBase):
    onboarded_on: date | None = None
    # Set false to create the client without materialising compliance items.
    generate_compliance_items: bool = True

    _validate_onboarded_on = field_validator("onboarded_on")(validate_onboarded_on)


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
    # Only catches a patch carrying both. One that moves a single half is
    # checked against the stored other half in ``update_client``, which is the
    # only place the pair is whole.
    _pan_matches_gstin = pan_matches_gstin()
    # The identity and registration fields back ``NOT NULL`` columns, so a null
    # here is not "clear it" — there is nothing to clear it to. The contact
    # fields above are genuinely optional and stay clearable.
    _no_nulls = not_clearable(
        "name",
        "entity_type",
        "gst_registered",
        "gst_filing_frequency",
        "tds_applicable",
        "income_tax_applicable",
        "tax_audit_applicable",
        "roc_applicable",
        "payroll_applicable",
        "is_active",
        "service_fees",
    )


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
