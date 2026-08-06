"""Client-portal schemas.

The portal is client-facing, so these payloads deliberately expose less than
the practitioner API: no internal notes, no other clients, and no fee on a
filing — a fee sitting on a filing is the firm's own working number, not
something the client has been asked to pay.

An invoice is the exception, and only once the firm has issued it. Sending an
invoice is the act of telling the client what they owe, so a sent invoice
belongs to them as much as to the firm. A draft or a cancelled one does not,
and never appears here.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import EmailStr, Field

from app.schemas.common import SanitizedModel
from app.schemas.document import ChecklistOut, SharedDocumentOut


class MagicLinkRequest(SanitizedModel):
    # Optional override — otherwise the link is addressed to the client's
    # email on file. The link itself is returned to the practitioner either
    # way; delivery is a separate, explicit step.
    send_to: EmailStr | None = None
    note: str | None = Field(default=None, max_length=500)


class MagicLinkOut(SanitizedModel):
    client_id: uuid.UUID
    client_name: str
    url: str
    token: str
    expires_at: datetime
    delivered_to: str | None = None


class PortalAccessOut(SanitizedModel):
    client_id: uuid.UUID
    portal_enabled: bool
    portal_token_valid_from: datetime | None
    portal_last_seen_at: datetime | None


class PortalFilingOut(SanitizedModel):
    """One filing as the client sees it — status, not fees."""

    id: uuid.UUID
    compliance_type_name: str
    form_number: str | None
    period_label: str
    due_date: date
    display_status: str
    status: str
    filed_on: date | None
    acknowledgement_number: str | None
    days_remaining: int
    missing_documents: list[str]


class PortalInvoiceLineOut(SanitizedModel):
    """One billed line. The client sees what the work was and what it cost."""

    description: str
    quantity: int
    amount_paise: int


class PortalInvoiceOut(SanitizedModel):
    """An invoice the firm has issued, as the client sees it.

    ``balance_paise`` is sent rather than left to the client to subtract: it is
    the number they actually act on, and the server is the authority on it.
    """

    id: uuid.UUID
    invoice_number: str
    issue_date: date
    due_date: date | None
    # The tax breakup, which the client's copy of an invoice exists to carry as
    # much as the total does: input tax credit is claimed against the heads
    # separately, and CGST credit cannot offset IGST. A client shown only a
    # total has no way to claim what they have paid.
    subtotal_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    tax_paise: int
    gst_rate_bps: int
    # ``"27-Maharashtra"``, or null on an invoice raised before the place of
    # supply was determined at all.
    place_of_supply: str | None
    total_paise: int
    amount_paid_paise: int
    balance_paise: int
    status: str
    is_overdue: bool
    lines: list[PortalInvoiceLineOut]


class PortalSummary(SanitizedModel):
    total: int
    overdue: int
    due_soon: int
    upcoming: int
    filed: int
    documents_outstanding: int
    # Across every issued invoice still carrying a balance.
    amount_due_paise: int = 0
    invoices_unpaid: int = 0


class PortalOverview(SanitizedModel):
    client_id: uuid.UUID
    client_name: str
    firm_name: str
    firm_email: str
    firm_phone: str | None
    contact_person: str | None
    summary: PortalSummary
    filings: list[PortalFilingOut]
    checklists: list[ChecklistOut]
    shared_documents: list[SharedDocumentOut]
    my_uploads: list[SharedDocumentOut]
    invoices: list[PortalInvoiceOut] = []
