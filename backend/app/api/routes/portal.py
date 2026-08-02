"""The client portal, plus the practitioner-side endpoints that grant access.

Two audiences share this module:

* ``/clients/{id}/portal-link`` and friends are practitioner endpoints — they
  mint and revoke magic links.
* ``/portal/*`` is what the client sees. Every one of those routes is scoped by
  ``PortalClient``, so a magic link can only ever read or write records
  belonging to the one client it was issued for.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentPractitioner, DbSession, Manager, PortalClient
from app.api.routes.documents import (
    clean_requirement_or_422,
    compliance_label,
    serialise_checklist,
)
from app.config import settings
from app.models.base import ComplianceStatus, InvoiceStatus
from app.models.client import Client
from app.models.compliance import ComplianceItem
from app.models.document import Document
from app.models.firm import Firm
from app.models.invoice import Invoice
from app.schemas.document import SharedDocumentOut
from app.schemas.portal import (
    MagicLinkOut,
    MagicLinkRequest,
    PortalAccessOut,
    PortalFilingOut,
    PortalInvoiceLineOut,
    PortalInvoiceOut,
    PortalOverview,
    PortalSummary,
)
from app.services import audit, storage
from app.services import documents as document_service
from app.services import portal as portal_service

router = APIRouter(tags=["portal"])

# The client portal shows a year of history and everything still ahead.
PORTAL_HISTORY_DAYS = 365
PORTAL_HORIZON_DAYS = 365


# Which invoices a client may see of their own billing.
#
# Deliberately a whitelist, not "everything except draft": a status added later
# stays hidden until someone decides it should be visible, which is the safe
# direction to be wrong in. A draft is the firm still deciding what to charge,
# and a cancelled invoice is one the client was never meant to pay — showing
# either would be telling them they owe money they do not.
PORTAL_VISIBLE_INVOICE_STATUSES = (
    InvoiceStatus.SENT,
    InvoiceStatus.PARTIALLY_PAID,
    InvoiceStatus.OVERDUE,
    InvoiceStatus.PAID,
)


def _collection_window_days() -> int:
    """How far ahead a filing has to be before its documents are asked for.

    Showing a year of filings and asking for a year of documents are different
    things. Every unfiled filing carries a checklist, so asking for all of them
    put dozens of requests on the landing page — most for periods that have not
    begun — around the few genuinely wanted now. A list that cannot be acted on
    is ignored wholesale, including the rows that mattered.

    The window is the firm's own: ``document_reminder_offsets_days`` is when
    the reminders start going out, so the portal asks for exactly what those
    emails ask for and the two never contradict each other.
    """
    offsets = settings.document_reminder_offsets
    return max(offsets) if offsets else 0


def _is_being_collected(state: str, days_until_due: int | None, window: int) -> bool:
    """Whether the firm is chasing this filing's documents today.

    Overdue is deliberately included: late is when the documents are wanted
    most, however long ago the due date passed.
    """
    if state == "filed":
        return False
    return days_until_due is None or days_until_due <= window


def _get_client_or_404(db: Session, firm_id: uuid.UUID, client_id: uuid.UUID) -> Client:
    client = db.get(Client, client_id)
    if client is None or client.firm_id != firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return client


# ------------------------------------------------- practitioner: access mgmt --


@router.post(
    "/clients/{client_id}/portal-link",
    response_model=MagicLinkOut,
    summary="Mint a magic link for a client",
)
def create_portal_link(
    client_id: uuid.UUID,
    payload: MagicLinkRequest,
    practitioner: Manager,
    db: DbSession,
):
    """Mint a magic link for a client.

    The link is returned to the practitioner rather than emailed from here —
    delivery goes through the reminder pipeline, so it lands in the audit trail
    like every other client contact.
    """
    client = _get_client_or_404(db, practitioner.firm_id, client_id)
    if not client.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot issue a portal link for an inactive client",
        )
    if not client.portal_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Portal access is disabled for this client — enable it first",
        )

    destination = payload.send_to or client.email
    link = portal_service.issue_magic_link(client)

    audit.record(
        db,
        action="portal.link_issued",
        entity_type="client",
        entity_id=client.id,
        actor=practitioner,
        summary=f"Issued a portal link for {client.name}"
        + (f" addressed to {destination}" if destination else ""),
        # Never log the token itself: the audit trail is readable by the firm.
        changes={"expires_at": link.expires_at.isoformat(), "sent_to": destination},
    )
    db.commit()

    return MagicLinkOut(
        client_id=client.id,
        client_name=client.name,
        url=link.url,
        token=link.token,
        expires_at=link.expires_at,
        delivered_to=destination,
    )


@router.get(
    "/clients/{client_id}/portal-access",
    response_model=PortalAccessOut,
    summary="A client's portal access state",
)
def read_portal_access(
    client_id: uuid.UUID, practitioner: CurrentPractitioner, db: DbSession
):
    client = _get_client_or_404(db, practitioner.firm_id, client_id)
    return PortalAccessOut(
        client_id=client.id,
        portal_enabled=client.portal_enabled,
        portal_token_valid_from=client.portal_token_valid_from,
        portal_last_seen_at=client.portal_last_seen_at,
    )


@router.post(
    "/clients/{client_id}/portal-access/revoke",
    response_model=PortalAccessOut,
    summary="Revoke every link issued so far",
)
def revoke_portal_links(client_id: uuid.UUID, practitioner: Manager, db: DbSession):
    """Invalidate every link issued so far. Future links still work."""
    client = _get_client_or_404(db, practitioner.firm_id, client_id)
    cutoff = portal_service.revoke_portal_access(client)
    audit.record(
        db,
        action="portal.links_revoked",
        entity_type="client",
        entity_id=client.id,
        actor=practitioner,
        summary=f"Revoked all portal links issued for {client.name} before {cutoff:%d %b %Y %H:%M}",
    )
    db.commit()
    db.refresh(client)
    return PortalAccessOut(
        client_id=client.id,
        portal_enabled=client.portal_enabled,
        portal_token_valid_from=client.portal_token_valid_from,
        portal_last_seen_at=client.portal_last_seen_at,
    )


@router.post(
    "/clients/{client_id}/portal-access/disable",
    response_model=PortalAccessOut,
    summary="Turn a client's portal off",
)
def disable_portal(client_id: uuid.UUID, practitioner: Manager, db: DbSession):
    client = _get_client_or_404(db, practitioner.firm_id, client_id)
    client.portal_enabled = False
    portal_service.revoke_portal_access(client)
    audit.record(
        db,
        action="portal.disabled",
        entity_type="client",
        entity_id=client.id,
        actor=practitioner,
        summary=f"Disabled portal access for {client.name}",
    )
    db.commit()
    db.refresh(client)
    return PortalAccessOut(
        client_id=client.id,
        portal_enabled=client.portal_enabled,
        portal_token_valid_from=client.portal_token_valid_from,
        portal_last_seen_at=client.portal_last_seen_at,
    )


@router.post(
    "/clients/{client_id}/portal-access/enable",
    response_model=PortalAccessOut,
    summary="Turn a client's portal on",
)
def enable_portal(client_id: uuid.UUID, practitioner: Manager, db: DbSession):
    client = _get_client_or_404(db, practitioner.firm_id, client_id)
    client.portal_enabled = True
    audit.record(
        db,
        action="portal.enabled",
        entity_type="client",
        entity_id=client.id,
        actor=practitioner,
        summary=f"Enabled portal access for {client.name}",
    )
    db.commit()
    db.refresh(client)
    return PortalAccessOut(
        client_id=client.id,
        portal_enabled=client.portal_enabled,
        portal_token_valid_from=client.portal_token_valid_from,
        portal_last_seen_at=client.portal_last_seen_at,
    )


# ---------------------------------------------------------- client: the portal --


def _portal_items(db: Session, client: Client, today: date) -> list[ComplianceItem]:
    return list(
        db.scalars(
            select(ComplianceItem)
            .options(selectinload(ComplianceItem.compliance_type))
            .where(
                ComplianceItem.client_id == client.id,
                ComplianceItem.status != ComplianceStatus.NOT_APPLICABLE,
                ComplianceItem.due_date >= today - timedelta(days=PORTAL_HISTORY_DAYS),
                ComplianceItem.due_date <= today + timedelta(days=PORTAL_HORIZON_DAYS),
            )
            .order_by(ComplianceItem.due_date)
        ).all()
    )


def _portal_invoices(db: Session, client: Client) -> list[Invoice]:
    """The client's issued invoices, newest first.

    Scoped by ``client_id`` as well as status, so a magic link cannot reach
    another client's billing even within the same firm.
    """
    return list(
        db.scalars(
            select(Invoice)
            .options(selectinload(Invoice.lines))
            .where(
                Invoice.client_id == client.id,
                Invoice.firm_id == client.firm_id,
                Invoice.status.in_(PORTAL_VISIBLE_INVOICE_STATUSES),
            )
            .order_by(Invoice.issue_date.desc(), Invoice.invoice_number.desc())
        ).all()
    )


def _portal_invoice(invoice: Invoice, today: date) -> PortalInvoiceOut:
    """Serialise one invoice for the client.

    ``is_overdue`` is derived from the due date and the balance rather than
    read off the status. The status is only as fresh as the last sweep that
    touched it, and a client being told a late bill is on time is the kind of
    wrong that costs the firm money.
    """
    return PortalInvoiceOut(
        id=invoice.id,
        invoice_number=invoice.invoice_number,
        issue_date=invoice.issue_date,
        due_date=invoice.due_date,
        total_paise=invoice.total_paise,
        amount_paid_paise=invoice.amount_paid_paise,
        balance_paise=invoice.balance_paise,
        status=invoice.status.value,
        is_overdue=(
            invoice.due_date is not None
            and invoice.due_date < today
            and invoice.balance_paise > 0
        ),
        lines=[
            PortalInvoiceLineOut(
                description=line.description,
                quantity=line.quantity,
                amount_paise=line.amount_paise,
            )
            for line in invoice.lines
        ],
    )


def _shared_document(document: Document) -> SharedDocumentOut:
    return SharedDocumentOut(
        id=document.id,
        original_filename=document.original_filename,
        category=document.category,
        size_bytes=document.size_bytes,
        uploaded_via_portal=document.uploaded_via_portal,
        created_at=document.created_at,
        compliance_label=compliance_label(document.compliance_item),
    )


@router.get(
    "/portal/me",
    response_model=PortalOverview,
    summary="Everything the client sees on landing",
)
def portal_overview(client: PortalClient, db: DbSession):
    """Everything the client sees on landing: filings, checklists, documents."""
    today = date.today()
    firm = db.get(Firm, client.firm_id)
    items = _portal_items(db, client, today)
    checklists = document_service.checklists_for_items(db, items)

    summary = PortalSummary(
        total=len(items), overdue=0, due_soon=0, upcoming=0, filed=0, documents_outstanding=0
    )

    invoices = [_portal_invoice(invoice, today) for invoice in _portal_invoices(db, client)]
    summary.amount_due_paise = sum(max(0, inv.balance_paise) for inv in invoices)
    summary.invoices_unpaid = sum(1 for inv in invoices if inv.balance_paise > 0)
    filings: list[PortalFilingOut] = []
    outstanding_checklists = []
    window = _collection_window_days()

    for item in items:
        state = item.derive_display_status(today)
        if state in ("overdue", "due_soon", "upcoming", "filed"):
            setattr(summary, state, getattr(summary, state) + 1)

        days_remaining = item.days_until_due(today)
        checklist = checklists[item.id]
        # The filing is still listed either way — only the ask is withheld
        # until the firm would actually be chasing it.
        missing = (
            checklist.missing
            if _is_being_collected(state, days_remaining, window)
            else []
        )
        if missing:
            summary.documents_outstanding += len(missing)
            outstanding_checklists.append(serialise_checklist(item, checklist))

        filings.append(
            PortalFilingOut(
                id=item.id,
                compliance_type_name=item.compliance_type.name,
                form_number=item.compliance_type.form_number,
                period_label=item.period_label,
                due_date=item.due_date,
                display_status=state,
                status=item.status.value,
                filed_on=item.filed_on,
                acknowledgement_number=item.acknowledgement_number,
                days_remaining=days_remaining,
                missing_documents=[
                    document_service.requirement_label(req) for req in missing
                ],
            )
        )

    uploads = list(
        db.scalars(
            select(Document)
            .options(
                selectinload(Document.compliance_item).selectinload(
                    ComplianceItem.compliance_type
                )
            )
            .where(Document.client_id == client.id)
            .order_by(Document.created_at.desc())
        ).all()
    )

    client.portal_last_seen_at = datetime.now(UTC)
    db.commit()

    return PortalOverview(
        client_id=client.id,
        client_name=client.name,
        firm_name=firm.name if firm else "Your CA",
        firm_email=firm.email if firm else "",
        firm_phone=firm.phone if firm else None,
        contact_person=client.contact_person,
        summary=summary,
        filings=filings,
        checklists=outstanding_checklists,
        shared_documents=[
            _shared_document(doc) for doc in uploads if doc.is_shared_with_client
        ],
        my_uploads=[_shared_document(doc) for doc in uploads if doc.uploaded_via_portal],
        invoices=invoices,
    )


@router.post(
    "/portal/documents",
    response_model=SharedDocumentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document as the client",
)
def portal_upload(
    client: PortalClient,
    db: DbSession,
    file: UploadFile = File(...),
    compliance_item_id: uuid.UUID | None = Form(default=None),
    requirement: str | None = Form(default=None),
):
    """Client-side upload against a checklist row."""
    requirement = clean_requirement_or_422(requirement)
    if compliance_item_id is not None:
        item = db.get(ComplianceItem, compliance_item_id)
        if item is None or item.client_id != client.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Filing not found"
            )

    data = file.file.read()
    try:
        document = document_service.ingest_upload(
            db,
            client=client,
            filename=file.filename or "upload",
            data=data,
            content_type=file.content_type,
            compliance_item_id=compliance_item_id,
            requirement=requirement,
            via_portal=True,
        )
    except storage.UploadTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except storage.UnsupportedFileType as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc

    audit.record(
        db,
        action="portal.document_upload",
        entity_type="document",
        entity_id=document.id,
        firm_id=client.firm_id,
        actor_label=f"{client.name} (client portal)",
        summary=f"{client.name} uploaded {document.original_filename} via the portal",
    )
    db.commit()
    db.refresh(document)
    return _shared_document(document)


@router.get("/portal/documents/{document_id}/download", summary="Download a document as the client")
def portal_download(document_id: uuid.UUID, client: PortalClient, db: DbSession):
    """Download a document the firm shared, or one the client uploaded."""
    document = db.get(Document, document_id)
    if document is None or document.client_id != client.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if not (document.is_shared_with_client or document.uploaded_via_portal):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This document has not been shared with you",
        )
    try:
        payload = storage.open_stored(document.storage_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="The stored file is no longer available",
        ) from exc
    return Response(
        content=payload,
        media_type=document.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{storage.safe_filename(document.original_filename)}"'
            )
        },
    )
