"""Compliance type / item schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field, field_validator

from app.core import clock
from app.models.base import ComplianceCategory, ComplianceStatus, Frequency
from app.schemas.common import (
    MAX_AMOUNT_PAISE,
    ORMModel,
    SanitizedModel,
    not_clearable,
)


def validate_filed_on(value: date | None) -> date | None:
    """Refuse a filing date that has not happened yet.

    A return cannot have been lodged on a day that has not arrived, so a date
    in the future is always a typo — and the year is the digit that gets
    mistyped, which puts it twelve months out rather than one day.

    Nothing downstream treats it as one. The item is marked filed, so it drops
    off the chase list and out of the reminder sweeps; ``_normalise_filing``
    compares the date with the due date and records the return as
    ``delayed_filed``; and the work becomes billable that moment, so the client
    is invoiced for a filing nobody has made. The record the firm would show an
    assessing officer then says a return was lodged on a date still in the
    future.

    Today is today in India — see :mod:`app.core.clock`. A practitioner filing
    at 01:00 IST on the 20th means the 20th, and bounding this by the server's
    own date would refuse it.
    """
    if value is not None and value > clock.today():
        raise ValueError(
            "filed_on cannot be in the future — a return is filed on or before today"
        )
    return value


class ComplianceTypeOut(ORMModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    category: ComplianceCategory
    frequency: Frequency
    form_number: str | None
    statutory_reference: str | None
    due_day: int
    due_month_offset: int
    applicability_rule: str
    default_fee_paise: int
    required_documents: list
    is_system: bool
    is_active: bool


class ComplianceItemOut(ORMModel):
    id: uuid.UUID
    firm_id: uuid.UUID
    client_id: uuid.UUID
    compliance_type_id: uuid.UUID
    assigned_practitioner_id: uuid.UUID | None
    period_label: str
    period_start: date
    period_end: date
    due_date: date
    status: ComplianceStatus
    filed_on: date | None
    acknowledgement_number: str | None
    fee_paise: int
    is_billed: bool
    notes: str | None
    created_at: datetime

    # Denormalised for the calendar UI.
    client_name: str | None = None
    compliance_type_code: str | None = None
    compliance_type_name: str | None = None
    category: ComplianceCategory | None = None
    form_number: str | None = None
    display_status: str | None = None
    days_remaining: int | None = None


class ComplianceItemUpdate(SanitizedModel):
    status: ComplianceStatus | None = None
    filed_on: date | None = None
    acknowledgement_number: str | None = Field(default=None, max_length=128)
    assigned_practitioner_id: uuid.UUID | None = None
    due_date: date | None = None
    fee_paise: int | None = Field(default=None, ge=0, le=MAX_AMOUNT_PAISE)
    notes: str | None = None

    _validate_filed_on = field_validator("filed_on")(validate_filed_on)
    # A filing always has a status, a deadline and a fee. Clearing the deadline
    # in particular is what the filed/delayed comparison then reads, so a null
    # there used to come back a 500 rather than a refusal naming the field.
    _no_nulls = not_clearable("status", "due_date", "fee_paise")


class BulkStatusUpdate(SanitizedModel):
    item_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    status: ComplianceStatus
    filed_on: date | None = None
    acknowledgement_number: str | None = Field(default=None, max_length=128)

    _validate_filed_on = field_validator("filed_on")(validate_filed_on)


class BulkStatusUpdateResult(SanitizedModel):
    updated: int
    skipped: int


class ComplianceCalendarBucket(SanitizedModel):
    """Counts for one period column of the calendar."""

    period_label: str
    total: int
    upcoming: int
    due_soon: int
    overdue: int
    filed: int


class ComplianceCalendarResponse(SanitizedModel):
    from_date: date
    to_date: date
    total: int
    limit: int
    offset: int
    summary: ComplianceCalendarBucket
    buckets: list[ComplianceCalendarBucket]
    items: list[ComplianceItemOut]


class ComplianceGenerateRequest(SanitizedModel):
    window_start: date | None = None
    window_end: date | None = None


class ComplianceGenerateResponse(SanitizedModel):
    created: int
    skipped_existing: int
    window_start: date
    window_end: date


class DashboardStats(SanitizedModel):
    total_clients: int
    active_clients: int
    total_items: int
    overdue: int
    due_soon: int
    upcoming: int
    filed_this_month: int
    unbilled_fee_paise: int
    by_category: dict[str, int]
