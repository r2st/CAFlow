"""Client-portal schemas.

The portal is client-facing, so these payloads deliberately expose less than
the practitioner API: no fee amounts, no internal notes, no other clients.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.document import ChecklistOut, SharedDocumentOut


class MagicLinkRequest(BaseModel):
    # Optional override — otherwise the link is addressed to the client's
    # email on file. The link itself is returned to the practitioner either
    # way; delivery is a separate, explicit step.
    send_to: EmailStr | None = None
    note: str | None = Field(default=None, max_length=500)


class MagicLinkOut(BaseModel):
    client_id: uuid.UUID
    client_name: str
    url: str
    token: str
    expires_at: datetime
    delivered_to: str | None = None


class PortalAccessOut(BaseModel):
    client_id: uuid.UUID
    portal_enabled: bool
    portal_token_valid_from: datetime | None
    portal_last_seen_at: datetime | None


class PortalFilingOut(BaseModel):
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


class PortalSummary(BaseModel):
    total: int
    overdue: int
    due_soon: int
    upcoming: int
    filed: int
    documents_outstanding: int


class PortalOverview(BaseModel):
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
