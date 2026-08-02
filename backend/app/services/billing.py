"""Invoicing: numbering, totals, generation from filed work, and payments.

All money is integer paise. GST on professional services is added at
``gst_rate_bps`` (18.00% by default) on top of the line subtotal.

The revenue-leakage problem this solves: a filing carries a ``fee_paise`` and
an ``is_billed`` flag, so "everything filed but never invoiced" is a query, not
a memory exercise.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.core.periods import fiscal_year_start, fy_label
from app.models.base import ComplianceStatus, InvoiceStatus
from app.models.client import Client
from app.models.compliance import ComplianceItem
from app.models.firm import Firm
from app.models.invoice import Invoice, InvoiceLine

FILED_STATUSES = (ComplianceStatus.FILED, ComplianceStatus.DELAYED_FILED)
UNPAID_STATUSES = (
    InvoiceStatus.SENT,
    InvoiceStatus.PARTIALLY_PAID,
    InvoiceStatus.OVERDUE,
)
# Professional services rendered by a chartered accountant.
DEFAULT_SAC_CODE = "998222"


class BillingError(ValueError):
    """Raised when an operation is not valid for the invoice's current state."""


class UnknownComplianceItem(BillingError):
    """A line pointed at a filing this firm and client do not own."""


# --------------------------------------------------------------- numbering --


def next_invoice_number(db: Session, firm_id: uuid.UUID, issue_date: date | None = None) -> str:
    """``INV/FY2026-27/0007`` — sequential within the firm's financial year.

    The count-based sequence is only a starting guess; the loop guarantees the
    number is actually free, so a deleted or hand-numbered invoice cannot cause
    a unique-constraint failure on the next insert.

    Reading which numbers are free says nothing about whether they still will
    be by the time a row is inserted. :func:`insert_numbered` is what makes
    that hold; calling this on its own is only a question, not a claim.
    """
    issue_date = issue_date or date.today()
    fy = fy_label(fiscal_year_start(issue_date))
    prefix = f"{settings.invoice_number_prefix}/{fy}/"

    used = set(
        db.scalars(
            select(Invoice.invoice_number).where(
                Invoice.firm_id == firm_id, Invoice.invoice_number.like(f"{prefix}%")
            )
        ).all()
    )
    sequence = len(used) + 1
    while f"{prefix}{sequence:04d}" in used:
        sequence += 1
    return f"{prefix}{sequence:04d}"


# How many numbers to try before giving up and letting the conflict surface.
# With the firm row held this loop should never run twice; the retries are for
# a backend that does not honour the lock, and four is enough that exhausting
# them means something other than contention is wrong.
NUMBER_ATTEMPTS = 4

# What a taken invoice number looks like coming back from each backend:
# PostgreSQL names the constraint, SQLite names the columns. Matched so that a
# genuine foreign-key or other violation is re-raised rather than retried
# NUMBER_ATTEMPTS times and then reported as a numbering problem.
_NUMBER_TAKEN = ("uq_invoice_firm_number", "invoices.invoice_number")


def number_collision(exc: IntegrityError) -> bool:
    """Whether ``exc`` is this firm's invoice number already being taken."""
    message = str(getattr(exc, "orig", exc))
    return any(marker in message for marker in _NUMBER_TAKEN)


def lock_firm_numbering(db: Session, firm_id: uuid.UUID) -> None:
    """Serialise invoice numbering for one firm, for the rest of the transaction.

    Two practitioners in the same firm pressing *Create invoice* together both
    read the same set of used numbers and both pick the next one; one of them
    then loses their whole request to a unique-constraint violation, and a
    batch generate loses every draft in it, not just the one that clashed.
    Holding the firm's own row is the cheapest thing that orders them, and it
    is per firm, so nobody else's billing waits.

    A plain ``SELECT`` is not blocked by this in PostgreSQL, so resolving the
    firm on other requests carries on untouched. SQLite has no row locks and
    ignores the clause — which is why :func:`insert_numbered` retries as well
    as locks.
    """
    db.execute(select(Firm.id).where(Firm.id == firm_id).with_for_update())


def insert_numbered(
    db: Session,
    *,
    firm_id: uuid.UUID,
    issue_date: date | None,
    build: Callable[[str], Invoice],
) -> Invoice:
    """Insert the invoice ``build`` returns, under a number that is really free.

    ``build`` is handed a number and returns the invoice to insert. It is a
    callable rather than a finished object because a losing attempt is undone
    wholesale — the savepoint takes the invoice, its lines and the ``is_billed``
    flags it set back out — so a retry has to construct them again rather than
    re-submit rows the session has already discarded.
    """
    lock_firm_numbering(db, firm_id)
    for remaining in reversed(range(NUMBER_ATTEMPTS)):
        try:
            with db.begin_nested():
                invoice = build(next_invoice_number(db, firm_id, issue_date))
                db.add(invoice)
                db.flush()
        except IntegrityError as exc:
            # Anything that is not the number being taken is the caller's
            # problem and is reported as it happened.
            if not remaining or not number_collision(exc):
                raise
            continue
        return invoice
    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


# ------------------------------------------------------------------ totals --


def line_amount(line: InvoiceLine) -> int:
    return int(line.quantity) * int(line.unit_price_paise)


def recalculate(invoice: Invoice) -> Invoice:
    """Recompute line amounts, subtotal, GST and total. Call after any edit."""
    subtotal = 0
    for line in invoice.lines:
        line.amount_paise = line_amount(line)
        subtotal += line.amount_paise

    invoice.subtotal_paise = subtotal
    # Round half-up, in paise.
    invoice.tax_paise = (subtotal * invoice.gst_rate_bps + 5_000) // 10_000
    invoice.total_paise = subtotal + invoice.tax_paise
    return invoice


def refresh_status(invoice: Invoice, today: date | None = None) -> Invoice:
    """Derive paid / partially-paid / overdue from the amounts and due date.

    Drafts and cancelled invoices are left alone — they are states a human
    chose, not states derived from money movement.
    """
    today = today or date.today()
    if invoice.status in (InvoiceStatus.DRAFT, InvoiceStatus.CANCELLED):
        return invoice

    if invoice.total_paise > 0 and invoice.amount_paid_paise >= invoice.total_paise:
        invoice.status = InvoiceStatus.PAID
    elif invoice.amount_paid_paise > 0:
        invoice.status = InvoiceStatus.PARTIALLY_PAID
    else:
        invoice.status = InvoiceStatus.SENT

    if (
        invoice.status != InvoiceStatus.PAID
        and invoice.due_date is not None
        and invoice.due_date < today
    ):
        invoice.status = InvoiceStatus.OVERDUE
    return invoice


# -------------------------------------------------------------- generation --


@dataclass
class BillableWork:
    """Filed-but-unbilled work for one client."""

    client: Client
    items: list[ComplianceItem] = field(default_factory=list)

    @property
    def total_paise(self) -> int:
        return sum(item.fee_paise for item in self.items)


def unbilled_items(
    db: Session,
    firm_id: uuid.UUID,
    *,
    client_id: uuid.UUID | None = None,
    filed_upto: date | None = None,
) -> list[ComplianceItem]:
    """Filings that are done, carry a fee, and have never been invoiced."""
    filters = [
        ComplianceItem.firm_id == firm_id,
        ComplianceItem.status.in_(FILED_STATUSES),
        ComplianceItem.is_billed.is_(False),
        ComplianceItem.fee_paise > 0,
    ]
    if client_id is not None:
        filters.append(ComplianceItem.client_id == client_id)
    if filed_upto is not None:
        filters.append(ComplianceItem.filed_on <= filed_upto)

    return list(
        db.scalars(
            select(ComplianceItem)
            .options(
                selectinload(ComplianceItem.client),
                selectinload(ComplianceItem.compliance_type),
            )
            .where(*filters)
            .order_by(ComplianceItem.client_id, ComplianceItem.filed_on)
        ).all()
    )


def group_billable(items: list[ComplianceItem]) -> list[BillableWork]:
    grouped: dict[uuid.UUID, BillableWork] = {}
    for item in items:
        work = grouped.get(item.client_id)
        if work is None:
            work = grouped[item.client_id] = BillableWork(client=item.client)
        work.items.append(item)
    return sorted(grouped.values(), key=lambda w: w.client.name)


def line_description(item: ComplianceItem) -> str:
    return f"{item.compliance_type.name} — {item.period_label}"


def build_invoice(
    db: Session,
    *,
    firm_id: uuid.UUID,
    client: Client,
    items: list[ComplianceItem],
    issue_date: date | None = None,
    due_date: date | None = None,
    gst_rate_bps: int | None = None,
    notes: str | None = None,
) -> Invoice:
    """Draft an invoice covering ``items`` and mark that work as billed."""
    if not items:
        raise BillingError("An invoice needs at least one billable filing")

    issue_date = issue_date or date.today()

    def build(invoice_number: str) -> Invoice:
        invoice = Invoice(
            firm_id=firm_id,
            client_id=client.id,
            invoice_number=invoice_number,
            issue_date=issue_date,
            due_date=due_date
            or issue_date + timedelta(days=settings.invoice_payment_terms_days),
            gst_rate_bps=(
                gst_rate_bps if gst_rate_bps is not None else settings.invoice_gst_rate_bps
            ),
            status=InvoiceStatus.DRAFT,
            notes=notes,
        )
        for item in items:
            invoice.lines.append(
                InvoiceLine(
                    compliance_item_id=item.id,
                    description=line_description(item),
                    quantity=1,
                    unit_price_paise=item.fee_paise,
                    amount_paise=item.fee_paise,
                    sac_code=DEFAULT_SAC_CODE,
                )
            )
            item.is_billed = True
        return recalculate(invoice)

    return insert_numbered(db, firm_id=firm_id, issue_date=issue_date, build=build)


def generate_invoices_for_firm(
    db: Session,
    firm_id: uuid.UUID,
    *,
    client_id: uuid.UUID | None = None,
    issue_date: date | None = None,
) -> list[Invoice]:
    """One draft invoice per client with outstanding billable work."""
    work = group_billable(unbilled_items(db, firm_id, client_id=client_id))
    return [
        build_invoice(
            db,
            firm_id=firm_id,
            client=entry.client,
            items=entry.items,
            issue_date=issue_date,
        )
        for entry in work
    ]


# ---------------------------------------------------------------- payments --


def record_payment(
    invoice: Invoice,
    *,
    amount_paise: int,
    payment_date: date | None = None,
    reference: str | None = None,
    today: date | None = None,
) -> Invoice:
    """Apply a payment. Overpayment is refused rather than silently absorbed."""
    if invoice.status == InvoiceStatus.CANCELLED:
        raise BillingError("Cannot record a payment against a cancelled invoice")
    if invoice.status == InvoiceStatus.DRAFT:
        raise BillingError("Send the invoice before recording a payment")
    if amount_paise <= 0:
        raise BillingError("Payment amount must be positive")
    if invoice.amount_paid_paise + amount_paise > invoice.total_paise:
        raise BillingError(
            f"Payment exceeds the outstanding balance of {invoice.balance_paise} paise"
        )

    invoice.amount_paid_paise += amount_paise
    invoice.payment_date = payment_date or date.today()
    if reference:
        invoice.payment_reference = reference
    refresh_status(invoice, today)
    return invoice


def release_items(db: Session, invoice: Invoice) -> int:
    """Un-bill the filings an invoice covered, so they can be re-invoiced."""
    item_ids = [line.compliance_item_id for line in invoice.lines if line.compliance_item_id]
    if not item_ids:
        return 0
    items = db.scalars(
        select(ComplianceItem).where(ComplianceItem.id.in_(item_ids))
    ).all()
    for item in items:
        item.is_billed = False
    return len(items)


# -------------------------------------------------------------- line edits --
# Hand-written lines (an ad-hoc invoice, or an edited draft) may cite a filing
# just as a generated line does. Both paths below exist so that citing one has
# the same consequences either way: the filing leaves the billable pile, and it
# cannot be cited by two invoices at once.


def referenced_items(
    db: Session,
    *,
    firm_id: uuid.UUID,
    client_id: uuid.UUID,
    lines,
) -> dict[uuid.UUID, ComplianceItem]:
    """Load the filings a set of lines cites, refusing anything not ours.

    A compliance item is addressable by id alone, so without this an invoice
    could cite another firm's filing — and cancelling it would then write
    ``is_billed`` onto their row. Being unreachable and not existing are
    reported identically, so an id cannot be probed for existence.
    """
    wanted = {line.compliance_item_id for line in lines if line.compliance_item_id}
    if not wanted:
        return {}

    found = {
        item.id: item
        for item in db.scalars(
            select(ComplianceItem)
            .options(selectinload(ComplianceItem.compliance_type))
            .where(
                ComplianceItem.id.in_(wanted),
                ComplianceItem.firm_id == firm_id,
                ComplianceItem.client_id == client_id,
            )
        ).all()
    }
    missing = wanted - found.keys()
    if missing:
        raise UnknownComplianceItem(
            f"No filing for this client matches {sorted(str(i) for i in missing)[0]}"
        )
    return found


def set_lines(db: Session, invoice: Invoice, lines, items: dict[uuid.UUID, ComplianceItem]):
    """Replace an invoice's lines, keeping ``is_billed`` in step.

    The invoice's existing claim is released *first*, so re-sending the same
    lines back — which is exactly what an editor round-trip does — is a no-op
    rather than a release. Anything still marked billed after that release is
    billed by some *other* invoice, and citing it again would bill the client
    twice for one filing.
    """
    release_items(db, invoice)
    invoice.lines.clear()

    for line in lines:
        item = items.get(line.compliance_item_id) if line.compliance_item_id else None
        if item is not None:
            if item.is_billed:
                raise BillingError(
                    f"{line_description(item)} is already on another invoice"
                )
            item.is_billed = True
        invoice.lines.append(
            InvoiceLine(
                compliance_item_id=line.compliance_item_id,
                description=line.description,
                quantity=line.quantity,
                unit_price_paise=line.unit_price_paise,
                amount_paise=line.quantity * line.unit_price_paise,
                sac_code=line.sac_code or DEFAULT_SAC_CODE,
            )
        )

    return recalculate(invoice)


# ----------------------------------------------------------------- revenue --


@dataclass
class RevenueSummary:
    from_date: date
    to_date: date
    invoiced_paise: int = 0
    collected_paise: int = 0
    outstanding_paise: int = 0
    overdue_paise: int = 0
    draft_paise: int = 0
    invoice_count: int = 0
    unbilled_paise: int = 0
    by_client: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)


def revenue_summary(
    db: Session,
    firm_id: uuid.UUID,
    *,
    from_date: date,
    to_date: date,
    today: date | None = None,
) -> RevenueSummary:
    """Invoiced / collected / outstanding over a window, plus unbilled work."""
    today = today or date.today()
    summary = RevenueSummary(from_date=from_date, to_date=to_date)

    invoices = list(
        db.scalars(
            select(Invoice)
            .options(selectinload(Invoice.client), selectinload(Invoice.lines))
            .where(
                Invoice.firm_id == firm_id,
                Invoice.issue_date >= from_date,
                Invoice.issue_date <= to_date,
            )
        ).all()
    )

    for invoice in invoices:
        if invoice.status == InvoiceStatus.CANCELLED:
            continue
        summary.invoice_count += 1
        if invoice.status == InvoiceStatus.DRAFT:
            summary.draft_paise += invoice.total_paise
            continue

        summary.invoiced_paise += invoice.total_paise
        summary.collected_paise += invoice.amount_paid_paise
        summary.outstanding_paise += invoice.balance_paise
        if invoice.due_date and invoice.due_date < today and invoice.balance_paise > 0:
            summary.overdue_paise += invoice.balance_paise

        name = invoice.client.name if invoice.client else "—"
        summary.by_client[name] = summary.by_client.get(name, 0) + invoice.total_paise

    # Work that is finished but has not made it onto an invoice yet.
    for item in unbilled_items(db, firm_id):
        summary.unbilled_paise += item.fee_paise
        category = item.compliance_type.category.value
        summary.by_category[category] = summary.by_category.get(category, 0) + item.fee_paise

    return summary


def unpaid_invoices(
    db: Session, firm_id: uuid.UUID | None = None, *, today: date | None = None
) -> list[Invoice]:
    """Sent invoices with money still outstanding — the payment-chasing list."""
    today = today or date.today()
    filters = [Invoice.status.in_(UNPAID_STATUSES)]
    if firm_id is not None:
        filters.append(Invoice.firm_id == firm_id)
    invoices = list(
        db.scalars(
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(*filters)
            .order_by(Invoice.due_date)
        ).all()
    )
    return [inv for inv in invoices if inv.balance_paise > 0]
