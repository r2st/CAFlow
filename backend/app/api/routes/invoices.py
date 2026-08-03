"""Billing: draft invoices from filed work, send them, and track payment."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentPractitioner, DbSession, Manager
from app.config import settings
from app.core import clock
from app.core.periods import fiscal_year_start
from app.models.base import InvoiceStatus
from app.models.client import Client
from app.models.invoice import Invoice
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


def _get_invoice_or_404(
    db: Session, firm_id: uuid.UUID, invoice_id: uuid.UUID, *, for_update: bool = False
) -> Invoice:
    """The invoice, or a 404 that does not say whose it was.

    ``for_update`` holds the row while the caller decides something from what
    it reads — see :func:`billing.load_for_update`. The tenancy check is the
    same either way, and comes after the lock: a row this firm cannot reach is
    one it was never told about, lock or no lock.
    """
    invoice = (
        billing.load_for_update(db, invoice_id)
        if for_update
        else db.get(Invoice, invoice_id)
    )
    if invoice is None or invoice.firm_id != firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return invoice


def serialise(invoice: Invoice, today: date | None = None) -> InvoiceOut:
    """Serialise an invoice, dating its lateness.

    Only for an invoice that is actually owed. A draft and a cancelled invoice
    both keep a due date and an unpaid balance, so lateness read off those two
    fields alone reported a bill the client was never asked to pay — and the
    billing table renders that as "75 days late" in red beside a *Draft* or
    *Cancelled* pill. It is the same pair ``refresh_status`` refuses to derive
    a status for, for the same reason: neither state follows from money.
    """
    today = today or clock.today()
    out = InvoiceOut.model_validate(invoice)
    out.client_name = invoice.client.name if invoice.client else None
    if (
        invoice.status not in billing.NOT_OWED_STATUSES
        and invoice.due_date is not None
        and invoice.balance_paise > 0
    ):
        overdue = (today - invoice.due_date).days
        out.days_overdue = overdue if overdue > 0 else None
    return out


def serialise_detail(invoice: Invoice, today: date | None = None) -> InvoiceDetailOut:
    return InvoiceDetailOut(
        **serialise(invoice, today).model_dump(),
        lines=list(invoice.lines),
    )


def _reject_due_before_issue(issue_date: date, due_date: date | None) -> None:
    """A payment term cannot run backwards.

    The due date is when the client was asked to pay by, counted from the day
    the bill was raised. Nothing checked that the caller's two dates were in
    that order, and the consequences do not wait for anyone to notice: sending
    such an invoice runs it through ``refresh_status``, which reads a due date
    already past and marks it *overdue* the same second it is issued. It lands
    on the receivables list, it is counted in ``overdue_paise``, the billing
    table shows the client in red, and the payment sweep chases them at
    whichever offset the gap happens to match — a demand for a late payment on
    a bill they have not had a single day to settle.

    ``update_invoice`` reached it from the other side, by moving a draft's
    issue date forward past a due date already on the record; a fat-fingered
    year did it silently.

    Only when the caller gave both. A missing due date is filled in from the
    issue date and the firm's payment terms when the invoice is sent, which
    cannot produce this.
    """
    if due_date is not None and due_date < issue_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"due_date {due_date:%d %b %Y} falls before the issue date "
                f"{issue_date:%d %b %Y} — an invoice cannot be due before it is raised"
            ),
        )


# Fields of a draft a caller may empty, and fields where an explicit null can
# only be "I did not send this".
#
# ``exclude_unset`` already separates a field the caller named from one they
# left out, so ``key in updates`` is the whole of that question. Requiring the
# value to be non-null on top of it threw away the other half: a caller who
# named a nullable field *in order to clear it* was answered 200, with the old
# value still on the record and nothing saying so.
#
# The due date is the half that costs something. It is what starts the payment
# clock on send, what turns the invoice overdue, what the payment sweep counts
# its offsets from, and what the client is shown in the portal — and clearing
# it is the one way back to the firm's standard terms, since ``send_invoice``
# fills in a missing one from ``invoice_payment_terms_days``. A practitioner
# who mistyped a date onto a draft could replace it with another wrong date but
# could not take it off, so the invoice went out demanding payment by whatever
# they had typed. The notes are the same silence in a smaller place.
#
# The other two are columns that cannot be null, so a null there is not an
# instruction and is ignored rather than written.
CLEARABLE_FIELDS = ("due_date", "notes")
REQUIRED_FIELDS = ("issue_date", "gst_rate_bps")


def _apply_lines(db: Session, invoice: Invoice, lines, firm_id: uuid.UUID) -> None:
    """Set an invoice's lines, translating billing refusals into HTTP.

    A line citing a filing the caller cannot reach is a 404 — the same answer
    an unknown id gets — while citing one that is already invoiced is a 409,
    because that is a conflict with the firm's own records.
    """
    try:
        items = billing.referenced_items(
            db, firm_id=firm_id, client_id=invoice.client_id, lines=lines
        )
        billing.set_lines(db, invoice, lines, items)
    except billing.UnknownComplianceItem as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except billing.BillingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


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
    today = clock.today()
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

    issue_date = payload.issue_date or clock.today()
    _reject_due_before_issue(issue_date, payload.due_date)

    def build(invoice_number: str) -> Invoice:
        invoice = Invoice(
            firm_id=practitioner.firm_id,
            client_id=client.id,
            invoice_number=invoice_number,
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
        _apply_lines(db, invoice, payload.lines, practitioner.firm_id)
        return invoice

    invoice = billing.insert_numbered(
        db, firm_id=practitioner.firm_id, issue_date=issue_date, build=build
    )

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
    # Checked against the pair this patch leaves behind, not against what the
    # caller happened to name: moving either date alone can put the two out of
    # order, and only one of them is ever in the request. A due date being
    # cleared leaves no pair to check, which ``_reject_due_before_issue``
    # already reads as nothing to say.
    _reject_due_before_issue(
        updates.get("issue_date") or invoice.issue_date,
        updates["due_date"] if "due_date" in updates else invoice.due_date,
    )
    for key in CLEARABLE_FIELDS:
        if key in updates:
            setattr(invoice, key, updates[key])
    for key in REQUIRED_FIELDS:
        if updates.get(key) is not None:
            setattr(invoice, key, updates[key])
    if payload.lines is not None:
        _apply_lines(db, invoice, payload.lines, practitioner.firm_id)
    billing.recalculate(invoice)

    # After the lines, so a re-numbering attempt is not undone by a savepoint
    # rollback belonging to the line edit.
    was = invoice.invoice_number
    renumbered = billing.renumber_for_issue_date(db, invoice, invoice.issue_date)

    audit.record(
        db,
        action="invoice.update",
        entity_type="invoice",
        entity_id=invoice.id,
        actor=practitioner,
        summary=(
            f"Updated draft invoice {invoice.invoice_number}"
            + (f", renumbered from {was}" if renumbered else "")
        ),
        changes={"invoice_number": [was, invoice.invoice_number]} if renumbered else None,
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
    # Held before the balance is read, not merely before it is written: the
    # overpayment check and the sum it writes back have to be one step, or two
    # receipts entered at once each overwrite the other's. See
    # ``billing.load_for_update``.
    invoice = _get_invoice_or_404(db, practitioner.firm_id, invoice_id, for_update=True)
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
    if invoice.status == InvoiceStatus.CANCELLED:
        # Refused rather than waved through: a second cancel has no work left to
        # do, and the filings it named may since have been re-billed on another
        # invoice. Told plainly, the way an already-sent invoice is.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invoice {invoice.invoice_number} has already been cancelled",
        )
    # Money having changed hands is what closes the door, not the label on the
    # status. A nil invoice is settled the moment it is issued without anything
    # being collected, and withdrawing one raised in error has to stay open —
    # otherwise the filings it cites keep `is_billed` for good and that work can
    # never be re-invoiced, which is the leakage this system exists to catch.
    if invoice.amount_paid_paise > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A paid invoice cannot be cancelled"
                if invoice.status == InvoiceStatus.PAID
                else "This invoice has payments recorded against it"
            ),
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
