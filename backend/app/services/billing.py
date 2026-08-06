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

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.core import clock
from app.core.periods import fiscal_year_start, fy_label
from app.models.base import ComplianceStatus, InvoiceStatus, SupplyType
from app.models.client import Client
from app.models.compliance import ComplianceItem, ComplianceType
from app.models.firm import Firm
from app.models.invoice import Invoice, InvoiceLine
from app.services import firms, gst

FILED_STATUSES = (ComplianceStatus.FILED, ComplianceStatus.DELAYED_FILED)
UNPAID_STATUSES = (
    InvoiceStatus.SENT,
    InvoiceStatus.PARTIALLY_PAID,
    InvoiceStatus.OVERDUE,
)
# Neither is a bill the client has been asked to pay: a draft has not been sent,
# and a cancelled one has been withdrawn. Both keep a due date and an unpaid
# balance all the same, so anything deriving lateness from those two fields
# alone has to exclude these first.
NOT_OWED_STATUSES = (InvoiceStatus.DRAFT, InvoiceStatus.CANCELLED)
# Professional services rendered by a chartered accountant.
DEFAULT_SAC_CODE = "998222"


class BillingError(ValueError):
    """Raised when an operation is not valid for the invoice's current state."""


class UnknownComplianceItem(BillingError):
    """A line pointed at a filing this firm and client do not own."""


# --------------------------------------------------------------- numbering --


def number_prefix(issue_date: date) -> str:
    """``INV/FY2026-27/`` — the series an invoice dated ``issue_date`` belongs to."""
    return f"{settings.invoice_number_prefix}/{fy_label(fiscal_year_start(issue_date))}/"


def next_invoice_number(db: Session, firm_id: uuid.UUID, issue_date: date | None = None) -> str:
    """``INV/FY2026-27/0007`` — sequential within the firm's financial year.

    The count-based sequence is only a starting guess; the loop guarantees the
    number is actually free, so a deleted or hand-numbered invoice cannot cause
    a unique-constraint failure on the next insert.

    Reading which numbers are free says nothing about whether they still will
    be by the time a row is inserted. :func:`insert_numbered` is what makes
    that hold; calling this on its own is only a question, not a claim.
    """
    prefix = number_prefix(issue_date or clock.today())

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

    Two practitioners pressing *Create invoice* together both read the same set
    of used numbers and both pick the next one; holding the firm's row is what
    orders them, and the retry is what saves the loser on a backend that does
    not honour the lock.
    """
    firms.lock_firm(db, firm_id)
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


def renumber_for_issue_date(db: Session, invoice: Invoice, issue_date: date) -> bool:
    """Move a draft into the number series its new issue date belongs to.

    An invoice number carries the financial year it was allocated in, and the
    sequence within that year is what a GST return is reconciled against. A
    draft raised on 31 March and then dated 1 April — deferring the revenue
    into the new year, which is an ordinary thing for a firm to do at the start
    of April — kept its ``FY2025-26`` number while being dated in FY2026-27.
    Two invoices dated in the same year then sat in different series, and the
    firm's own books disagreed with its numbering.

    Only a draft: once sent, the number is a document of record and the caller
    is refused the edit long before this. Returns whether the number changed.
    """
    if invoice.invoice_number.startswith(number_prefix(issue_date)):
        return False

    previous = invoice.invoice_number
    firms.lock_firm(db, invoice.firm_id)
    for remaining in reversed(range(NUMBER_ATTEMPTS)):
        try:
            with db.begin_nested():
                invoice.invoice_number = next_invoice_number(db, invoice.firm_id, issue_date)
                db.flush()
        except IntegrityError as exc:
            if not remaining or not number_collision(exc):
                raise
            # The savepoint took the write back out; the attribute is ours to
            # restore, so the next attempt reads the same starting point.
            invoice.invoice_number = previous
            continue
        return True
    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


# ----------------------------------------------------- place of supply --


def apply_place_of_supply(db: Session, invoice: Invoice, client: Client) -> Invoice:
    """Resolve where this supply is placed, and under which heads it is taxed.

    Called when an invoice is raised, and again whenever a *draft* is edited —
    a draft is not yet a document of record, so a client's GSTIN corrected
    before the bill goes out should correct the bill. Once sent, nothing here
    runs again: the tax character of an issued invoice is fixed at issue, and a
    client who later re-registers in another state has not changed a document
    they are already holding a copy of.

    A firm row that cannot be read leaves the supply undetermined, which
    :func:`~app.services.gst.resolve_supply` resolves to intra-state with no
    place of supply recorded — the same answer, and the same visible marker,
    as a firm that has not entered its own GSTIN.
    """
    firm = db.get(Firm, invoice.firm_id)
    if firm is None:  # pragma: no cover - a live invoice always has its firm
        invoice.place_of_supply, invoice.supply_type = None, SupplyType.INTRA_STATE
        return invoice
    invoice.place_of_supply, invoice.supply_type = gst.resolve_supply(firm, client)
    return invoice


# ------------------------------------------------------------------ totals --


def line_amount(line: InvoiceLine) -> int:
    return int(line.quantity) * int(line.unit_price_paise)


def recalculate(invoice: Invoice) -> Invoice:
    """Recompute line amounts, subtotal, GST and total. Call after any edit.

    The tax total is struck first and then divided into its heads, rather than
    each head being computed from its own rate — see
    :func:`~app.services.gst.split_tax` for why that ordering is what keeps the
    printed components summing to the total the client is asked to pay.

    ``supply_type`` falls back to intra-state when it is unset, which is the
    case for an invoice being built in memory before it has ever reached the
    database and taken the column's default. It is the same fallback
    :func:`~app.services.gst.resolve_supply` makes for an undetermined supply,
    so nothing depends on which of the two paths got here.
    """
    subtotal = 0
    for line in invoice.lines:
        line.amount_paise = line_amount(line)
        subtotal += line.amount_paise

    invoice.subtotal_paise = subtotal
    # Round half-up, in paise.
    invoice.tax_paise = (subtotal * invoice.gst_rate_bps + 5_000) // 10_000
    invoice.total_paise = subtotal + invoice.tax_paise
    invoice.cgst_paise, invoice.sgst_paise, invoice.igst_paise = gst.split_tax(
        invoice.tax_paise, invoice.supply_type or SupplyType.INTRA_STATE
    )
    return invoice


def refresh_status(invoice: Invoice, today: date | None = None) -> Invoice:
    """Derive paid / partially-paid / overdue from the amounts and due date.

    Drafts and cancelled invoices are left alone — they are states a human
    chose, not states derived from money movement.
    """
    today = today or clock.today()
    if invoice.status in NOT_OWED_STATUSES:
        return invoice

    # A nil invoice is settled the moment it is issued. Firms raise them to put
    # no-charge work on the record — a courtesy filing, a fee written off, work
    # absorbed under a retainer — and there is nothing to collect. Leaving it in
    # the unpaid states had no way out: `record_payment` refuses every amount as
    # an overpayment against a zero total, so nothing could ever move it, while
    # the due date still dragged it to overdue and onto the chase list.
    if invoice.total_paise <= 0 or invoice.amount_paid_paise >= invoice.total_paise:
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

    issue_date = issue_date or clock.today()

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
        for position, item in enumerate(items):
            invoice.lines.append(
                InvoiceLine(
                    position=position,
                    compliance_item_id=item.id,
                    description=line_description(item),
                    quantity=1,
                    unit_price_paise=item.fee_paise,
                    amount_paise=item.fee_paise,
                    sac_code=DEFAULT_SAC_CODE,
                )
            )
            item.is_billed = True
        # Before the totals, because the split the totals produce depends on it.
        apply_place_of_supply(db, invoice, client)
        return recalculate(invoice)

    return insert_numbered(db, firm_id=firm_id, issue_date=issue_date, build=build)


def generate_invoices_for_firm(
    db: Session,
    firm_id: uuid.UUID,
    *,
    client_id: uuid.UUID | None = None,
    issue_date: date | None = None,
) -> list[Invoice]:
    """One draft invoice per client with outstanding billable work.

    The firm's row is held before the work is read, not merely before each
    invoice is inserted. ``insert_numbered`` takes the same lock, but by the
    time it runs the decision has already been made — from a plain ``SELECT``
    that nothing ordered.

    Two practitioners both pressing *Generate invoices* at the end of a month
    is not a contrived race; it is Tuesday. Both read the same filed-but-
    unbilled filings, then queue up on the lock one after the other, and each
    raises a full set of invoices for them. Marking a filing billed is not a
    claim the second one has to win — ``is_billed`` is already true and setting
    it again succeeds — so the client receives two invoices for the same work,
    which is the revenue-leakage problem this module exists to solve, inverted
    into the more expensive direction.

    Held for the rest of the transaction, so the read, the decision and the
    ``is_billed`` writes are one step as far as any other request is concerned.
    """
    firms.lock_firm(db, firm_id)
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


def load_for_update(db: Session, invoice_id: uuid.UUID) -> Invoice | None:
    """Read an invoice with its row held for the rest of the transaction.

    Recording a payment reads what has been paid so far, decides whether the
    new amount fits inside the balance, and writes the sum back — the same
    read-decide-write the invoice numbering and the plan limits both had to be
    ordered for, except that this one is the money itself.

    A plain read leaves the two halves apart. Two ₹5,000 receipts against a
    ₹10,000 invoice, entered at the same moment by the practitioner who took
    the call and the one reconciling the bank feed, both read ``0`` paid, both
    pass the overpayment check, and both write ``5,000`` — the second over the
    first. The client has paid in full, the firm's books say half, and the
    invoice stays on the chase list with a balance the client has already
    settled. Nothing in the trail says a payment was lost; there is simply one
    fewer than was recorded.

    ``populate_existing`` is the other half of the fix. The session keeps
    loaded rows without expiring them on commit, so the identity map answers a
    second read out of memory with the values from the first — and holding the
    row would then guard a decision taken from a copy older than the lock.
    """
    return db.scalars(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


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
    invoice.payment_date = payment_date or clock.today()
    if reference:
        invoice.payment_reference = reference
    refresh_status(invoice, today)
    return invoice


def release_items(db: Session, invoice: Invoice) -> int:
    """Un-bill the filings an invoice covered, so they can be re-invoiced.

    Only the filings no *other* live invoice still cites. ``is_billed`` is a
    bare flag with no record of which invoice set it, so an invoice releasing
    every filing it happens to name can clear a claim that has since moved on:
    cancel an invoice, re-bill the freed work on a second one, then cancel the
    first again and that filing is back in the billable pile while the second
    invoice is still charging for it — straight to billing the client twice.

    Cancelled invoices are ignored, which is what makes releasing a claim
    possible at all; the invoice being released is excluded so re-sending a
    draft's own lines stays the no-op it looks like.
    """
    item_ids = {line.compliance_item_id for line in invoice.lines if line.compliance_item_id}
    if not item_ids:
        return 0

    claimed_elsewhere = set(
        db.scalars(
            select(InvoiceLine.compliance_item_id)
            .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
            .where(
                InvoiceLine.compliance_item_id.in_(item_ids),
                Invoice.id != invoice.id,
                Invoice.status != InvoiceStatus.CANCELLED,
            )
        ).all()
    )
    releasable = item_ids - claimed_elsewhere
    if not releasable:
        return 0

    items = db.scalars(
        select(ComplianceItem).where(
            ComplianceItem.id.in_(releasable),
            # Scoped for the same reason ``referenced_items`` is: nothing should
            # be able to write ``is_billed`` onto another firm's row.
            ComplianceItem.firm_id == invoice.firm_id,
        )
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

    The firm's row is held for the whole of that, because reading ``is_billed``
    and then writing it is the same read-decide-write the plan limits and the
    invoice numbering both had to be ordered for. Creation was already inside
    the lock ``insert_numbered`` takes; editing a draft was not, and two
    managers each adding the same filed return to their own draft both saw it
    unbilled, both claimed it, and both invoices went out citing it.

    Two lines of *this* invoice citing one filing is the other way to reach the
    same refusal, and it needs its own words. The first line claims the filing,
    so the second one meets ``is_billed`` already true and was told the filing
    "is already on another invoice" — of an invoice that does not exist, on the
    request that was creating this one. A practitioner reading that goes
    looking through the ledger for the bill that supposedly has it, finds
    nothing, and has no way to see that both offending lines are on the form in
    front of them. Named here from the lines actually submitted rather than
    from the flag, so the message says which of the two situations it is.
    """
    firms.lock_firm(db, invoice.firm_id)
    release_items(db, invoice)
    invoice.lines.clear()

    claimed_here: set[uuid.UUID] = set()
    for position, line in enumerate(lines):
        item = items.get(line.compliance_item_id) if line.compliance_item_id else None
        if item is not None:
            if item.id in claimed_here:
                raise BillingError(
                    f"{line_description(item)} is on this invoice twice — "
                    "a filing is billed on one line"
                )
            if item.is_billed:
                raise BillingError(
                    f"{line_description(item)} is already on another invoice"
                )
            item.is_billed = True
            claimed_here.add(item.id)
        invoice.lines.append(
            InvoiceLine(
                position=position,
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


# What separates two clients a firm has given the same name.
#
# A client's name is not unique and nothing pretends it is — only the PAN is,
# and that is optional. Two entities of one family or group under one trading
# name is ordinary ("Kumar Enterprises" at two GSTINs), and so is the same name
# typed twice by mistake, which is the case this most needs to survive.
UNNAMED_CLIENT = "—"


def _client_label(name: str | None) -> str:
    return name if name else UNNAMED_CLIENT


def _disambiguate(rows: list[tuple[uuid.UUID, str | None, str | None, int]]) -> dict[str, int]:
    """Name each client's revenue, telling apart any two that share a name.

    ``by_client`` is keyed by name and was accumulated by name, so two clients
    called the same thing were added together: the breakdown showed one of them
    carrying both their revenues and the other missing entirely. It is a
    revenue report, and the firm reads it to see who it has billed — so the
    reading is that one client owes twice what they do and another has been
    billed nothing all year, which is exactly the conclusion that gets acted
    on.

    Only a repeated name is qualified, so the ordinary breakdown is untouched
    and the qualifier appears precisely where a human would otherwise have to
    guess. The PAN is what a practitioner would reach for to tell two clients
    apart; a client without one falls back to the leading digits of its id,
    which is at least stable and at least distinct.

    Each row is ``(client_id, name, pan, amount)`` — the three columns the
    naming needs rather than the whole client, since the totals now come back
    from a grouped query instead of from rows read into memory.
    """
    seen: dict[str, int] = {}
    for _, name, _pan, _amount in rows:
        label = _client_label(name)
        seen[label] = seen.get(label, 0) + 1

    labelled: dict[str, int] = {}
    for client_id, name, pan, amount in rows:
        label = _client_label(name)
        if seen[label] > 1:
            label = f"{label} ({pan or str(client_id)[:8]})"
        labelled[label] = labelled.get(label, 0) + amount
    return labelled


def revenue_summary(
    db: Session,
    firm_id: uuid.UUID,
    *,
    from_date: date,
    to_date: date,
    today: date | None = None,
) -> RevenueSummary:
    """Invoiced / collected / outstanding over a window, plus unbilled work.

    Counted by the database rather than read whole and reduced here, for the
    reason the dashboard, the calendar and the workload view were each taken
    off the same curve. Every figure on this screen is an aggregate, and what
    used to happen on each visit was that every invoice the firm raised in the
    financial year was read off disk and hydrated into a mapped object — along
    with *every line of every one of them*, eagerly, in a second query, to
    compute a summary that never looks at a line.

    That set only grows: an invoice is a permanent record, a practice raises one
    per client per billing run, and the default window is the whole current
    financial year. The billing screen is what a firm opens to find out what it
    is owed, which means it is opened most in the last week of the month —
    while the same database is running the month-end invoice generation.

    Three grouped queries in place of it: the money, the per-client breakdown,
    and the unbilled work. The arithmetic is unchanged, including which statuses
    contribute to what — a cancelled invoice is not counted at all, a draft
    counts towards ``draft_paise`` and the invoice count and nothing else, and
    lateness is derived from the due date and the balance rather than read off
    the status.
    """
    today = today or clock.today()
    summary = RevenueSummary(from_date=from_date, to_date=to_date)

    window = [
        Invoice.firm_id == firm_id,
        Invoice.issue_date >= from_date,
        Invoice.issue_date <= to_date,
    ]
    # Everything a client has actually been asked to pay. Cancelled is excluded
    # from every figure; a draft is separated inside the query rather than by a
    # second pass over the same rows.
    counted = [*window, Invoice.status != InvoiceStatus.CANCELLED]
    is_draft = Invoice.status == InvoiceStatus.DRAFT
    balance = Invoice.total_paise - Invoice.amount_paid_paise

    def total(expression):
        """A ``SUM(CASE …)`` that answers 0 rather than NULL for an empty firm."""
        return func.coalesce(func.sum(expression), 0)

    def issued(amount):
        return case((is_draft, 0), else_=amount)

    (
        summary.invoice_count,
        summary.draft_paise,
        summary.invoiced_paise,
        summary.collected_paise,
        summary.outstanding_paise,
        summary.overdue_paise,
    ) = db.execute(
        select(
            func.count(Invoice.id),
            total(case((is_draft, Invoice.total_paise), else_=0)),
            total(issued(Invoice.total_paise)),
            total(issued(Invoice.amount_paid_paise)),
            total(issued(balance)),
            # A NULL due date fails the comparison and falls to the ``else_``,
            # which is what the Python did: an invoice with no date on it is
            # not late.
            total(
                case(
                    (
                        (~is_draft) & (Invoice.due_date < today) & (balance > 0),
                        balance,
                    ),
                    else_=0,
                )
            ),
        ).where(*counted)
    ).one()

    # Grouped by client id and named afterwards, because a name does not
    # identify a client; see :func:`_disambiguate`. Outer-joined so a row whose
    # client has somehow gone still carries its revenue, which is what reading
    # the relationship did.
    summary.by_client = _disambiguate(
        [
            (client_id, name, pan, amount)
            for client_id, name, pan, amount in db.execute(
                select(
                    Invoice.client_id,
                    Client.name,
                    Client.pan,
                    func.sum(Invoice.total_paise),
                )
                .outerjoin(Client, Client.id == Invoice.client_id)
                .where(*counted, ~is_draft)
                .group_by(Invoice.client_id, Client.name, Client.pan)
            ).all()
        ]
    )

    # Work that is finished but has not made it onto an invoice yet — the same
    # filings :func:`unbilled_items` selects, summed by category instead of
    # loaded.
    for category, amount in db.execute(
        select(ComplianceType.category, func.sum(ComplianceItem.fee_paise))
        .join(ComplianceType, ComplianceType.id == ComplianceItem.compliance_type_id)
        .where(
            ComplianceItem.firm_id == firm_id,
            ComplianceItem.status.in_(FILED_STATUSES),
            ComplianceItem.is_billed.is_(False),
            ComplianceItem.fee_paise > 0,
        )
        .group_by(ComplianceType.category)
    ).all():
        summary.unbilled_paise += amount
        key = category.value
        summary.by_category[key] = summary.by_category.get(key, 0) + amount

    return summary


def open_invoice_ids(db: Session, firm_id: uuid.UUID | None = None) -> list[uuid.UUID]:
    """The ids of every invoice still in one of the unpaid states, in a fixed order.

    Ids rather than rows, because the one caller — the nightly status refresh —
    re-reads each of them under its own lock before deciding anything from it;
    see :func:`load_for_update`. Handing it loaded rows is what made that
    impossible.

    Unfiltered by balance, unlike :func:`unpaid_invoices`. That function answers
    "who still owes us money", so a settled bill is rightly not on it; this one
    answers "which rows might be labelled wrong", and an invoice sitting in
    ``sent`` with nothing left to collect is exactly such a row.

    Ordered, so two runs walk the queue the same way rather than crossing on
    the locks.
    """
    stmt = select(Invoice.id).where(Invoice.status.in_(UNPAID_STATUSES))
    if firm_id is not None:
        stmt = stmt.where(Invoice.firm_id == firm_id)
    return list(db.scalars(stmt.order_by(Invoice.id)).all())


def unpaid_invoices(
    db: Session, firm_id: uuid.UUID | None = None, *, today: date | None = None
) -> list[Invoice]:
    """Sent invoices with money still outstanding — the payment-chasing list."""
    today = today or clock.today()
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
