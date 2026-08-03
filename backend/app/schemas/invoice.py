"""Billing and invoicing schemas. Every amount is integer paise."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from pydantic import Field, field_validator

from app.core import clock
from app.models.base import InvoiceStatus
from app.schemas.common import MAX_AMOUNT_PAISE, ORMModel, SanitizedModel

# How far ahead an invoice may be dated. A month covers every forward-dating a
# practice actually does — see :func:`validate_issue_date` — and a mistyped
# year lands eleven months past it at the very closest, so no typo of the kind
# this guards against can fit inside the window.
MAX_ISSUE_DATE_LEAD_DAYS = 31


def validate_issue_date(value: date | None) -> date | None:
    """Refuse an invoice dated further ahead than a practice ever bills.

    The issue date is the one date on an invoice a practitioner types rather
    than derives, and it decides far more than when the bill was written:

    * it picks the number series, so ``INV/FY2096-97/0001`` is a live number
      allocated seventy years early — and the sequence a GST return is
      reconciled against now has a gap in it that nothing will ever fill;
    * the due date is counted from it, so the invoice is never overdue, never
      reaches the receivables list and is never chased. It is simply not
      collected;
    * the revenue window is the current financial year, so it is invisible on
      the billing screen the firm reads to find what is outstanding.

    Nothing recovers from that on its own. The invoice exists, the client has
    been billed, and the only record of it sits outside every view the firm
    looks at. A mistyped year is what produces it — the digit that gets
    mistyped in a date field — and it produces the version of this that nobody
    notices, because the row does not appear anywhere to be noticed in.

    So it is bounded rather than closed, because dating a bill a little ahead
    is real work and refusing it outright would break the two places a practice
    does it:

    * raising an invoice on 31 March dated 1 April, deferring the revenue into
      the new financial year — the ordinary thing to do in the last week of
      March, and the whole reason a draft's date can move at all;
    * a monthly retainer prepared on the 28th and dated the 1st, which is how
      an advance is billed.

    Both are days ahead. A mistyped year is a *year* ahead, so a month of lead
    separates them completely: everything a firm means to do fits inside it and
    nothing that produces an invisible invoice does.

    Back-dating stays open with no bound at all — entering last month's
    invoices while catching up on the books is ordinary, and a date in the past
    still lands in a series the firm can see and a due date that still comes.

    Today is today in India — see :mod:`app.core.clock`. A practitioner raising
    an invoice at 01:00 IST on the 1st means the 1st, and bounding this by the
    server's own date would put them a day out.
    """
    if value is None:
        return value
    horizon = clock.today() + timedelta(days=MAX_ISSUE_DATE_LEAD_DAYS)
    if value > horizon:
        raise ValueError(
            f"issue_date cannot be later than {horizon:%d %b %Y} — an invoice may be dated "
            f"up to {MAX_ISSUE_DATE_LEAD_DAYS} days ahead, and no further"
        )
    return value


class InvoiceLineIn(SanitizedModel):
    description: str = Field(min_length=1, max_length=512)
    quantity: int = Field(default=1, ge=1, le=10_000)
    unit_price_paise: int = Field(ge=0, le=MAX_AMOUNT_PAISE)
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

    _validate_issue_date = field_validator("issue_date")(validate_issue_date)


class InvoiceUpdate(SanitizedModel):
    """Only a draft can have its lines or dates changed."""

    lines: list[InvoiceLineIn] | None = Field(default=None, min_length=1, max_length=200)
    issue_date: date | None = None
    due_date: date | None = None
    gst_rate_bps: int | None = Field(default=None, ge=0, le=10_000)
    notes: str | None = None

    # The edit is the other door to the same place, and the more dangerous of
    # the two: moving a draft's issue date forward re-numbers it into the
    # series that date belongs to, so a mistyped year both hides the invoice
    # and burns a number in a financial year decades out.
    _validate_issue_date = field_validator("issue_date")(validate_issue_date)


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
    amount_paise: int = Field(gt=0, le=MAX_AMOUNT_PAISE)
    payment_date: date | None = None
    reference: str | None = Field(default=None, max_length=128)


class InvoiceGenerateRequest(SanitizedModel):
    """Draft invoices for every client with filed-but-unbilled work."""

    client_id: uuid.UUID | None = None
    issue_date: date | None = None

    # The third door, and the widest: one mistyped date here raises a
    # future-dated invoice for *every* client with unbilled work at once.
    _validate_issue_date = field_validator("issue_date")(validate_issue_date)


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
