"""Building and queueing client reminders.

Three kinds of chase are automated:

* **filing** — "your GSTR-3B is due on the 20th" (driven by the compliance
  type's own ``reminder_offsets_days``);
* **document** — "we still need your bank statement to file it" (driven by
  ``settings.document_reminder_offsets``, and only sent while something is
  actually outstanding);
* **payment** — "invoice INV/FY2026-27/0004 is now due".

Reminders are queued, never sent inline: a row is created with
``scheduled_for`` and the dispatcher picks it up. That keeps the audit trail
intact and makes "did we chase them?" answerable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core import clock
from app.models.base import (
    ComplianceStatus,
    ReminderChannel,
    ReminderStatus,
    ReminderType,
)
from app.models.client import Client
from app.models.compliance import ComplianceItem
from app.models.invoice import Invoice
from app.models.reminder import Reminder
from app.services import billing, documents, firms
from app.services.ai import DraftingBudget, draft_client_message

# Reminders go out at 09:00 IST on their offset day.
REMINDER_HOUR_IST = 9
IST_OFFSET = clock.IST_OFFSET


def ist_morning(day: date) -> datetime:
    """09:00 IST on ``day``, as an aware UTC datetime."""
    return datetime.combine(day, time(hour=REMINDER_HOUR_IST), tzinfo=clock.IST).astimezone(UTC)


def preferred_channel(client: Client) -> ReminderChannel:
    """WhatsApp is how Indian CAs actually reach clients; email is the fallback."""
    if client.whatsapp:
        return ReminderChannel.WHATSAPP
    if client.email:
        return ReminderChannel.EMAIL
    if client.phone:
        return ReminderChannel.SMS
    return ReminderChannel.EMAIL


def recipient_for(client: Client, channel: ReminderChannel) -> str | None:
    match channel:
        case ReminderChannel.WHATSAPP:
            return client.whatsapp or client.phone
        case ReminderChannel.SMS:
            return client.phone
        case _:
            return client.email


def kind_of(reminder: Reminder) -> str:
    """Which chase a queued reminder was, defaulting to the one that predates the key.

    Filing reminders were the first kind and wrote only ``offset_days``, so a
    row with no ``kind`` is one of those. Reading it that way is what lets the
    check below distinguish kinds without a backfill: without the default,
    every filing reminder already queued would look like a different kind and
    be chased a second time on the next run.
    """
    return (reminder.extra or {}).get("kind", "filing")


def already_queued(existing: list[Reminder], kind: str, offset: int) -> bool:
    """One reminder per (target, kind, offset), checked in Python.

    Per *kind*, not per offset alone: a filing falling due in ten days can be
    both the subject of a document chase and of the deadline reminder itself,
    and those say different things. Matching on the offset alone meant whichever
    job ran first silenced the other — the client was asked for a bank statement
    and never told when the return was due.

    A JSON containment predicate would behave differently on SQLite and
    PostgreSQL; the candidate set here is small enough that it does not matter.
    """
    return any(
        kind_of(r) == kind and (r.extra or {}).get("offset_days") == offset
        for r in existing
    )


# ---------------------------------------------------- the chase still standing --

# The statuses in which a filing is still something to chase. Anything else —
# filed, delayed-filed, or ruled not applicable — is a chase that has been
# answered or withdrawn.
OPEN_ITEM_STATUSES = (ComplianceStatus.PENDING, ComplianceStatus.IN_PROGRESS)


def withdrawn_reason(db: Session, reminder: Reminder) -> str | None:
    """Why a queued chase should no longer go out, or ``None`` if it should.

    A reminder is queued because something was outstanding *then*, and sent
    later — the sweeps queue at 07:00 IST for 09:00 IST, and a message held for
    a greylisting relay waits longer still. Nothing re-read the reason in
    between, so the queue went out on a fact two hours to several days stale:

    * the client sent the bank statement and the return was filed at half past
      eight, and at nine they were emailed to ask for it;
    * they paid the invoice, and were then chased for the balance — quoting an
      amount they no longer owe;
    * they surrendered the GST registration behind the filing, which closes the
      item and cancels the task raised for it, and were asked for the paperwork
      anyway.

    Only the automated chases. ``kind`` distinguishes them from a message a
    practitioner composed, which may perfectly well be *about* a filed return —
    a filing confirmation is exactly that — and is never second-guessed here.
    Read in Python rather than as a JSON predicate, for the reason
    :func:`already_queued` is.

    A missing item or invoice is not a reason: the foreign keys cascade, so
    there is no such row to find, and being unable to check is not grounds for
    withholding a message the firm asked for.
    """
    kind = kind_of(reminder)
    if kind in ("filing", "document") and reminder.compliance_item_id is not None:
        item = db.get(ComplianceItem, reminder.compliance_item_id)
        if item is None or item.status in OPEN_ITEM_STATUSES:
            return None
        if item.status == ComplianceStatus.NOT_APPLICABLE:
            return "the filing is no longer one this client owes"
        return f"the return was {item.status.value} before this went out"

    if kind == "payment" and reminder.invoice_id is not None:
        invoice = db.get(Invoice, reminder.invoice_id)
        if invoice is None:
            return None
        if invoice.status in billing.NOT_OWED_STATUSES:
            # Neither is a bill the client has been asked to pay.
            return f"the invoice is {invoice.status.value}"
        if invoice.balance_paise <= 0:
            return "the invoice has been settled in full"

    return None


# ------------------------------------------------------- one sweep at a time --


def _hold_while_queueing(db: Session, firm_id: uuid.UUID) -> None:
    """Hold this firm's row for the rest of the transaction, before its queue is read.

    Queueing a chase is the same read-decide-write the invoice numbering and
    the plan limits both had to be ordered for: read what is already queued for
    a filing, decide there is nothing, add one. Neither sweep commits until it
    has been through every firm it was given, and a run spends up to
    ``ai_draft_budget_seconds`` on the wording — so the gap between the read
    and the commit is a minute or two wide, not an instant.

    Two runs inside that gap both read an empty queue and both add. The nightly
    beat is one; *Queue reminders now* on the reminders screen is the other,
    and it is the same code reachable by any manager at any moment — including
    twice, from one double-clicked button, landing on two workers. What comes
    out is a client emailed the identical document chase or fee reminder twice
    on the same morning, over the firm's own name.

    Held before the read rather than around the insert: a lock taken after the
    decision orders the writes and nothing else, which is the state this
    replaces. Per firm and in a deterministic order, so two sweeps queue behind
    each other rather than crossing.
    """
    firms.lock_firm(db, firm_id)


# ----------------------------------------------------- document collection --


def queue_document_reminders(
    db: Session,
    *,
    firm_id: uuid.UUID | None = None,
    today: date | None = None,
    offsets: list[int] | None = None,
    budget: DraftingBudget | None = None,
) -> list[Reminder]:
    """Chase clients for the documents their upcoming filings still need.

    Unlike a filing reminder, this only fires when something is genuinely
    outstanding — a client who has already uploaded everything is left alone.

    ``budget`` caps how long the whole sweep may spend on model-drafted
    wording; see :class:`~app.services.ai.DraftingBudget`. One is made here
    when the caller supplies none, so that reaching this directly cannot leave
    the drafting unbounded by accident.

    Each firm's row is held while its own queue is read and added to; see
    :func:`_hold_while_queueing`.
    """
    run_date = today or clock.today()
    offsets = offsets if offsets is not None else settings.document_reminder_offsets
    budget = budget if budget is not None else DraftingBudget()
    if not offsets:
        return []

    horizon = run_date + timedelta(days=max(offsets))
    # Only firms this deployment still acts for. A firm switched off keeps its
    # compliance items, and sweeping the items rather than the firms is what
    # let it go on chasing its clients after it had stopped being served.
    firm_ids = sorted(firms.servable_firm_ids(db, firm_id))

    queued: list[Reminder] = []
    for current_firm_id in firm_ids:
        _hold_while_queueing(db, current_firm_id)
        firm_name = firms.name_of(db, current_firm_id)
        outstanding = documents.items_awaiting_documents(
            db, current_firm_id, from_date=run_date, to_date=horizon
        )
        for item, checklist in outstanding:
            days_left = (item.due_date - run_date).days
            if days_left not in offsets:
                continue
            client = item.client
            if client is None or not client.is_active:
                continue

            existing = list(
                db.scalars(
                    select(Reminder).where(Reminder.compliance_item_id == item.id)
                ).all()
            )
            if already_queued(existing, "document", days_left):
                continue

            missing_labels = [
                documents.requirement_label(req) for req in checklist.missing
            ]
            channel = preferred_channel(client)
            body = draft_client_message(
                purpose="document_request",
                client_name=client.name,
                context={
                    "compliance": item.compliance_type.name,
                    "period": item.period_label,
                    "due_date": item.due_date.isoformat(),
                    "days_remaining": days_left,
                    "documents": missing_labels,
                },
                channel=channel.value,
                firm_name=firm_name,
                budget=budget,
            )
            reminder = Reminder(
                firm_id=item.firm_id,
                client_id=client.id,
                compliance_item_id=item.id,
                reminder_type=ReminderType.DOCUMENT,
                channel=channel,
                status=ReminderStatus.SCHEDULED,
                subject=(
                    f"{len(missing_labels)} document(s) still needed for "
                    f"{item.compliance_type.name} ({item.period_label})"
                ),
                body=body,
                recipient=recipient_for(client, channel),
                scheduled_for=ist_morning(run_date),
                extra={
                    "kind": "document",
                    "offset_days": days_left,
                    "missing": checklist.missing,
                },
            )
            db.add(reminder)
            queued.append(reminder)

    if queued:
        db.flush()
    return queued


# ------------------------------------------------------------ fee chasing --


def queue_payment_reminders(
    db: Session,
    *,
    firm_id: uuid.UUID | None = None,
    today: date | None = None,
    offsets: list[int] | None = None,
    budget: DraftingBudget | None = None,
) -> list[Reminder]:
    """Chase unpaid invoices at 0/7/15/30 days past the due date.

    ``budget`` bounds the model-drafted wording across the sweep, for the same
    reason it does above, and each firm's row is held while its own receivables
    are read for the reason :func:`_hold_while_queueing` gives.

    Walked firm by firm rather than straight down one cross-tenant list of
    receivables. A switched-off firm's debts are still owed, but chasing them
    in its name is not ours to do while it is not being served — which
    ``servable_firm_ids`` already decided — and taking each firm's lock before
    reading only that firm's invoices is what keeps the hold to the firm being
    worked on. Sorted, so two sweeps queue behind each other in the same order
    rather than crossing.
    """
    run_date = today or clock.today()
    offsets = offsets if offsets is not None else settings.payment_reminder_offsets
    budget = budget if budget is not None else DraftingBudget()
    if not offsets:
        return []

    queued: list[Reminder] = []
    for current_firm_id in sorted(firms.servable_firm_ids(db, firm_id)):
        _hold_while_queueing(db, current_firm_id)
        # Every message is signed by the firm that raised the invoice.
        firm_name = firms.name_of(db, current_firm_id)
        for invoice in billing.unpaid_invoices(db, current_firm_id, today=run_date):
            if invoice.due_date is None:
                continue
            days_overdue = (run_date - invoice.due_date).days
            if days_overdue not in offsets:
                continue
            client = invoice.client
            if client is None or not client.is_active:
                continue

            existing = list(
                db.scalars(select(Reminder).where(Reminder.invoice_id == invoice.id)).all()
            )
            if already_queued(existing, "payment", days_overdue):
                continue

            channel = preferred_channel(client)
            body = draft_client_message(
                purpose="fee_reminder",
                client_name=client.name,
                context={
                    "invoice_number": invoice.invoice_number,
                    "amount_inr": f"{invoice.balance_paise / 100:,.2f}",
                    "due_date": invoice.due_date.isoformat(),
                    "days_overdue": days_overdue,
                },
                channel=channel.value,
                firm_name=firm_name,
                budget=budget,
            )
            reminder = Reminder(
                firm_id=invoice.firm_id,
                client_id=client.id,
                invoice_id=invoice.id,
                reminder_type=ReminderType.PAYMENT,
                channel=channel,
                status=ReminderStatus.SCHEDULED,
                subject=(
                    f"Invoice {invoice.invoice_number} — "
                    f"₹{invoice.balance_paise / 100:,.2f} outstanding"
                ),
                body=body,
                recipient=recipient_for(client, channel),
                scheduled_for=ist_morning(run_date),
                extra={"kind": "payment", "offset_days": days_overdue},
            )
            db.add(reminder)
            queued.append(reminder)

    if queued:
        db.flush()
    return queued


# ------------------------------------------------------------------ manual --


def build_manual_reminder(
    db: Session,
    *,
    client: Client,
    reminder_type: ReminderType,
    subject: str | None,
    body: str | None,
    channel: ReminderChannel | None = None,
    scheduled_for: datetime | None = None,
    compliance_item_id: uuid.UUID | None = None,
    invoice_id: uuid.UUID | None = None,
) -> Reminder:
    """A one-off reminder a practitioner composed (or asked the AI to draft)."""
    channel = channel or preferred_channel(client)
    reminder = Reminder(
        firm_id=client.firm_id,
        client_id=client.id,
        compliance_item_id=compliance_item_id,
        invoice_id=invoice_id,
        reminder_type=reminder_type,
        channel=channel,
        status=ReminderStatus.SCHEDULED,
        subject=subject,
        body=body,
        recipient=recipient_for(client, channel),
        scheduled_for=scheduled_for or datetime.now(UTC),
        extra={"kind": "manual"},
    )
    db.add(reminder)
    db.flush()
    return reminder
