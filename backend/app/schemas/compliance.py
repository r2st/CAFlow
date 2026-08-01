"""Compliance type / item schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field

from app.models.base import ComplianceCategory, ComplianceStatus, Frequency
from app.schemas.common import ORMModel, SanitizedModel


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
    fee_paise: int | None = Field(default=None, ge=0)
    notes: str | None = None


class BulkStatusUpdate(SanitizedModel):
    item_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    status: ComplianceStatus
    filed_on: date | None = None
    acknowledgement_number: str | None = Field(default=None, max_length=128)


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
