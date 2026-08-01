"""Billing: draft invoices from filed work, send them, and track payment."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentPractitioner, DbSession, Manager
from app.config import settings
from app.core.periods import fiscal_year_start
from app.models.base import InvoiceStatus
from app.models.client import Client
from app.models.invoice import Invoice, InvoiceLine
from app.schemas.common import Page
from app.schemas.invoice import (
    BillableClientOut,
    BillableItemOut,
    BillableWorkResponse,
    InvoiceCreate,
    InvoiceDetailOut,
    InvoiceGenerateRequest,
    InvoiceGenerateResponse,
    InvoiceOut,
    InvoiceUpdate,
    PaymentCreate,
    RevenueSummaryOut,
)
from app.services import audit, billing

router = APIRouter(prefix="/invoices", tags=["billing"])


def _get_invoice_or_404(db: Session, firm_id: uuid.UUID, invoice_id: uuid.UUID) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.firm_id != firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return invoice


def serialise(invoice: Invoice, today: date | None = None) -> InvoiceOut:
    today = today or date.today()
    out = InvoiceOut.model_validate(invoice)
    out.client_name = invoice.client.name if invoice.client else None
    if invoice.due_date is not None and invoice.balance_paise > 0:
        overdue = (today - invoice.due_date).days
        out.days_overdue = overdue if overdue > 0 else None
    return out


def serialise_detail(invoice: Invoice, today: date | None = None) -> InvoiceDetailOut:
    return InvoiceDetailOut(
        **serialise(invoice, today).model_dump(),
        lines=list(invoice.lines),
    )


def _replace_lines(invoice: Invoice, lines) -> None:
    invoice.lines.clear()
    for line in lines:
        invoice.lines.append(
            InvoiceLine(
                compliance_item_id=line.compliance_item_id,
                description=line.description,
                quantity=line.quantity,
                unit_price_paise=line.unit_price_paise,
                amount_paise=line.quantity * line.unit_price_paise,
                sac_code=line.sac_code or billing.DEFAULT_SAC_CODE,
            )
        )


# ---------------------------------------------------------- billable / stats --
# Literal paths first so they are not captured by /{invoice_id}.


@router.get(
    "/billable",
    response_model=BillableWorkResponse,
    summary="Filed work that has not been billed",
)
def billable_work(
    practitioner: CurrentPractitioner,
    db: DbSession,
    client_id: uuid.UUID | None = Query(default=None),
):
    """Filed work that carries a fee and has never been invoiced."""
    items = billing.unbilled_items(db, practitioner.firm_id, client_id=client_id)
    groups = billing.group_billable(items)
    return BillableWorkResponse(
        clients=[
            BillableClientOut(
                client_id=group.client.id,
                client_name=group.client.name,
                item_count=len(group.items),
                total_paise=group.total_paise,
                items=[
                    BillableItemOut(
                        compliance_item_id=item.id,
                        description=billing.line_description(item),
                        period_label=item.period_label,
                        filed_on=item.filed_on,
                        fee_paise=item.fee_paise,
                    )
                    for item in group.items
                ],
            )
            for group in groups
        ],
        total_paise=sum(group.total_paise for group in groups),
        total_items=len(items),
    )


@router.get("/revenue", response_model=RevenueSummaryOut, summary="Revenue and receivables summary")
def revenue(
    practitioner: CurrentPractitioner,
    db: DbSession,
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
):
    """Invoiced / collected / outstanding, defaulting to the current FY."""
    today = date.today()
    fy_start_year = fiscal_year_start(today)
    start = from_date or date(fy_start_year, 4, 1)
    end = to_date or date(fy_start_year + 1, 3, 31)
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to_date must not be before from_date",
        )

    summary = billing.revenue_summary(
        db, practitioner.firm_id, from_date=start, to_date=end, today=today
    )
    return RevenueSummaryOut(**vars(summary))


@router.post(
    "/generate",
    response_model=InvoiceGenerateResponse,
    summary="Draft invoices from unbilled work",
)
def generate_invoices(
    payload: InvoiceGenerateRequest, practitioner: Manager, db: DbSession
):
    """One draft invoice per client with outstanding billable work."""
    if payload.client_id is not None:
        client = db.get(Client, payload.client_id)
        if client is None or client.firm_id != practitioner.firm_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Client not found"
            )

    invoices = billing.generate_invoices_for_firm(
        db,
        practitioner.firm_id,
        client_id=payload.client_id,
        issue_date=payload.issue_date,
    )
    audit.record(
        db,
        action="invoice.generate",
        entity_type="invoice",
        actor=practitioner,
        summary=f"Drafted {len(invoices)} invoice(s) from unbilled filings",
        changes={"invoice_numbers": [inv.invoice_number for inv in invoices]},
    )
    db.commit()
    for invoice in invoices:
        db.refresh(invoice)

    return InvoiceGenerateResponse(
        created=len(invoices),
        total_paise=sum(inv.total_paise for inv in invoices),
        invoices=[serialise(inv) for inv in invoices],
    )


# --------------------------------------------------------------------- CRUD --


@router.post(
    "",
    response_model=InvoiceDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an invoice",
)
def create_invoice(payload: InvoiceCreate, practitioner: Manager, db: DbSession):
    client = db.get(Client, payload.client_id)
    if client is None or client.firm_id != practitioner.firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")

    issue_date = payload.issue_date or date.today()
    invoice = Invoice(
        firm_id=practitioner.firm_id,
        client_id=client.id,
        invoice_number=billing.next_invoice_number(db, practitioner.firm_id, issue_date),
        issue_date=issue_date,
        due_date=payload.due_date,
        gst_rate_bps=(
            payload.gst_rate_bps
            if payload.gst_rate_bps is not None
            else settings.invoice_gst_rate_bps
        ),
        status=InvoiceStatus.DRAFT,
        notes=payload.notes,
    )
    _replace_lines(invoice, payload.lines)
    billing.recalculate(invoice)
    db.add(invoice)
    db.flush()

    audit.record(
        db,
        action="invoice.create",
        entity_type="invoice",
        entity_id=invoice.id,
        actor=practitioner,
        summary=f"Created invoice {invoice.invoice_number} for {client.name}",
    )
    db.commit()
    db.refresh(invoice)
    return serialise_detail(invoice)


@router.get("", response_model=Page[InvoiceOut], summary="List invoices")
def list_invoices(
    practitioner: CurrentPractitioner,
    db: DbSession,
    client_id: uuid.UUID | None = Query(default=None),
    invoice_status: InvoiceStatus | None = Query(default=None),
    unpaid_only: bool = Query(default=False),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    filters = [Invoice.firm_id == practitioner.firm_id]
    if client_id is not None:
        filters.append(Invoice.client_id == client_id)
    if invoice_status is not None:
        filters.append(Invoice.status == invoice_status)
    if unpaid_only:
        filters.append(Invoice.status.in_(billing.UNPAID_STATUSES))
    if from_date is not None:
        filters.append(Invoice.issue_date >= from_date)
    if to_date is not None:
        filters.append(Invoice.issue_date <= to_date)

    total = db.scalar(select(func.count(Invoice.id)).where(*filters)) or 0
    rows = db.scalars(
        select(Invoice)
        .options(selectinload(Invoice.client))
        .where(*filters)
        .order_by(Invoice.issue_date.desc(), Invoice.invoice_number.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[InvoiceOut](
        items=[serialise(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{invoice_id}", response_model=InvoiceDetailOut, summary="An invoice with its lines")
def get_invoice(invoice_id: uuid.UUID, practitioner: CurrentPractitioner, db: DbSession):
    return serialise_detail(_get_invoice_or_404(db, practitioner.firm_id, invoice_id))


@router.patch("/{invoice_id}", response_model=InvoiceDetailOut, summary="Update a draft invoice")
def update_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceUpdate,
    practitioner: Manager,
    db: DbSession,
):
    """Edit a draft. Once sent, an invoice is a document of record."""
    invoice = _get_invoice_or_404(db, practitioner.firm_id, invoice_id)
    if invoice.status != InvoiceStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only draft invoices can be edited (this one is {invoice.status.value})",
        )

    updates = payload.model_dump(exclude_unset=True)
    for key in ("issue_date", "due_date", "gst_rate_bps", "notes"):
        if key in updates and updates[key] is not None:
            setattr(invoice, key, updates[key])
    if payload.lines is not None:
        billing.release_items(db, invoice)
        _replace_lines(invoice, payload.lines)
    billing.recalculate(invoice)

    audit.record(
        db,
        action="invoice.update",
        entity_type="invoice",
        entity_id=invoice.id,
        actor=practitioner,
        summary=f"Updated draft invoice {invoice.invoice_number}",
    )
    db.commit()
    db.refresh(invoice)
    return serialise_detail(invoice)


@router.post(
    "/{invoice_id}/send",
    response_model=InvoiceDetailOut,
    summary="Issue an invoice to the client",
)
def send_invoice(invoice_id: uuid.UUID, practitioner: Manager, db: DbSession):
    """Move a draft to sent, which is what starts the payment clock."""
    invoice = _get_invoice_or_404(db, practitioner.firm_id, invoice_id)
    if invoice.status != InvoiceStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invoice {invoice.invoice_number} has already been sent",
        )
    if not invoice.lines:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot send an invoice with no lines",
        )

    invoice.status = InvoiceStatus.SENT
    if invoice.due_date is None:
        invoice.due_date = invoice.issue_date + timedelta(
            days=settings.invoice_payment_terms_days
        )
    billing.refresh_status(invoice)

    audit.record(
        db,
        action="invoice.send",
        entity_type="invoice",
        entity_id=invoice.id,
        actor=practitioner,
        summary=(
            f"Sent invoice {invoice.invoice_number} — "
            f"₹{invoice.total_paise / 100:,.2f} due {invoice.due_date:%d %b %Y}"
        ),
    )
    db.commit()
    db.refresh(invoice)
    return serialise_detail(invoice)


@router.post("/{invoice_id}/payments", response_model=InvoiceDetailOut, summary="Record a payment")
def record_payment(
    invoice_id: uuid.UUID,
    payload: PaymentCreate,
    practitioner: Manager,
    db: DbSession,
):
    invoice = _get_invoice_or_404(db, practitioner.firm_id, invoice_id)
    try:
        billing.record_payment(
            invoice,
            amount_paise=payload.amount_paise,
            payment_date=payload.payment_date,
            reference=payload.reference,
        )
    except billing.BillingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    audit.record(
        db,
        action="invoice.payment",
        entity_type="invoice",
        entity_id=invoice.id,
        actor=practitioner,
        summary=(
            f"Recorded ₹{payload.amount_paise / 100:,.2f} against "
            f"{invoice.invoice_number} → {invoice.status.value}"
        ),
        changes={"amount_paise": payload.amount_paise, "reference": payload.reference},
    )
    db.commit()
    db.refresh(invoice)
    return serialise_detail(invoice)


@router.post("/{invoice_id}/cancel", response_model=InvoiceDetailOut, summary="Cancel an invoice")
def cancel_invoice(invoice_id: uuid.UUID, practitioner: Manager, db: DbSession):
    """Cancel an invoice and release its filings back to the billable pool."""
    invoice = _get_invoice_or_404(db, practitioner.firm_id, invoice_id)
    if invoice.status == InvoiceStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A paid invoice cannot be cancelled"
        )
    if invoice.amount_paid_paise > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This invoice has payments recorded against it",
        )

    invoice.status = InvoiceStatus.CANCELLED
    released = billing.release_items(db, invoice)
    audit.record(
        db,
        action="invoice.cancel",
        entity_type="invoice",
        entity_id=invoice.id,
        actor=practitioner,
        summary=(
            f"Cancelled invoice {invoice.invoice_number}; "
            f"{released} filing(s) returned to unbilled"
        ),
    )
    db.commit()
    db.refresh(invoice)
    return serialise_detail(invoice)
