"""Billing and invoicing schemas. Every amount is integer paise."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field

from app.models.base import InvoiceStatus
from app.schemas.common import ORMModel, SanitizedModel


class InvoiceLineIn(SanitizedModel):
    description: str = Field(min_length=1, max_length=512)
    quantity: int = Field(default=1, ge=1, le=10_000)
    unit_price_paise: int = Field(ge=0)
    sac_code: str | None = Field(default=None, max_length=16)
    compliance_item_id: uuid.UUID | None = None


class InvoiceLineOut(ORMModel):
    id: uuid.UUID
    compliance_item_id: uuid.UUID | None
    description: str
    quantity: int
    unit_price_paise: int
    amount_paise: int
    sac_code: str | None


class InvoiceCreate(SanitizedModel):
    client_id: uuid.UUID
    lines: list[InvoiceLineIn] = Field(min_length=1, max_length=200)
    issue_date: date | None = None
    due_date: date | None = None
    gst_rate_bps: int | None = Field(default=None, ge=0, le=10_000)
    notes: str | None = None


class InvoiceUpdate(SanitizedModel):
    """Only a draft can have its lines or dates changed."""

    lines: list[InvoiceLineIn] | None = Field(default=None, min_length=1, max_length=200)
    issue_date: date | None = None
    due_date: date | None = None
    gst_rate_bps: int | None = Field(default=None, ge=0, le=10_000)
    notes: str | None = None


class InvoiceOut(ORMModel):
    id: uuid.UUID
    firm_id: uuid.UUID
    client_id: uuid.UUID
    invoice_number: str
    issue_date: date
    due_date: date | None
    subtotal_paise: int
    tax_paise: int
    total_paise: int
    amount_paid_paise: int
    balance_paise: int
    gst_rate_bps: int
    status: InvoiceStatus
    payment_date: date | None
    payment_reference: str | None
    notes: str | None
    created_at: datetime

    client_name: str | None = None
    days_overdue: int | None = None


class InvoiceDetailOut(InvoiceOut):
    lines: list[InvoiceLineOut] = []


class PaymentCreate(SanitizedModel):
    amount_paise: int = Field(gt=0)
    payment_date: date | None = None
    reference: str | None = Field(default=None, max_length=128)


class InvoiceGenerateRequest(SanitizedModel):
    """Draft invoices for every client with filed-but-unbilled work."""

    client_id: uuid.UUID | None = None
    issue_date: date | None = None


class InvoiceGenerateResponse(SanitizedModel):
    created: int
    total_paise: int
    invoices: list[InvoiceOut]


class BillableItemOut(SanitizedModel):
    compliance_item_id: uuid.UUID
    description: str
    period_label: str
    filed_on: date | None
    fee_paise: int


class BillableClientOut(SanitizedModel):
    client_id: uuid.UUID
    client_name: str
    item_count: int
    total_paise: int
    items: list[BillableItemOut]


class BillableWorkResponse(SanitizedModel):
    clients: list[BillableClientOut]
    total_paise: int
    total_items: int


class RevenueSummaryOut(SanitizedModel):
    from_date: date
    to_date: date
    invoiced_paise: int
    collected_paise: int
    outstanding_paise: int
    overdue_paise: int
    draft_paise: int
    invoice_count: int
    unbilled_paise: int
    by_client: dict[str, int]
    by_category: dict[str, int]
