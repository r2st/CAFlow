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
from collections.abc import Collection
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


def _group_queued(
    db: Session, column, ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[Reminder]]:
    """Everything already queued against each of ``ids``, in one query.

    ``already_queued`` reads the reminders a filing or an invoice already
    carries, and every sweep asked for them one target at a time — inside a
    loop over the firm's whole due list, and inside the transaction that holds
    the firm's row for the length of the run.

    That is one round-trip per filing, on a run whose size is the firm's client
    list rather than a handful: a firm's deadlines cluster on the same offset
    day, because every GST client is due on the 20th. Twenty-five clients of
    the seeded calendar are thirteen hundred filings, so a sweep that queues
    thirteen hundred reminders spent thirteen hundred separate SELECTs finding
    out that none of them had one — and the *quiet* run, the one where
    everything is already queued and nothing is added, costs exactly the same.

    None of it buys anything a single query does not. The candidate set is the
    firm's own due list, and the reminders hanging off it are few.
    """
    grouped: dict[uuid.UUID, list[Reminder]] = {target_id: [] for target_id in ids}
    if not grouped:
        return grouped
    for reminder in db.scalars(select(Reminder).where(column.in_(grouped))).all():
        grouped[getattr(reminder, column.key)].append(reminder)
    return grouped


def queued_for_items(
    db: Session, item_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[Reminder]]:
    """Reminders already queued against each filing. See :func:`_group_queued`."""
    return _group_queued(db, Reminder.compliance_item_id, item_ids)


def queued_for_invoices(
    db: Session, invoice_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[Reminder]]:
    """Reminders already queued against each invoice. See :func:`_group_queued`."""
    return _group_queued(db, Reminder.invoice_id, invoice_ids)


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
        if item is None:
            return None
        if item.status not in OPEN_ITEM_STATUSES:
            if item.status == ComplianceStatus.NOT_APPLICABLE:
                return "the filing is no longer one this client owes"
            return f"the return was {item.status.value} before this went out"
        # The filing is still open, which settles a *filing* reminder: the
        # deadline is what that one is about and it has not moved. A document
        # chase is about a list, and the list is the part that goes stale.
        #
        # The client uploading the last statement is the ordinary case, and it
        # is the case the queue cannot see. The sweep asks at 07:00 for 09:00,
        # the portal is open in between, and the filing itself is not touched
        # until the practitioner sits down to it that afternoon — so the
        # already-filed test above catches none of this. What went out was the
        # firm asking its own client, by name, for paperwork the firm was
        # already holding and could see in the portal.
        #
        # Recomputed rather than read from ``extra["missing"]``: that list is
        # what was outstanding when the row was written, and the question here
        # is what is outstanding now.
        if kind == "document" and documents.checklist_for_item(db, item).is_complete:
            return "every document it asked for has since arrived"
        return None

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


def hold_while_queueing(db: Session, firm_id: uuid.UUID) -> None:
    """Hold this firm's row for the rest of the transaction, before its queue is read.

    Queueing a chase is the same read-decide-write the invoice numbering and
    the plan limits both had to be ordered for: read what is already queued for
    a filing, decide there is nothing, add one. No sweep commits until it has
    been through every firm it was given, and a run spends up to
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

    Public because all three sweeps take it, and the third does not live here:
    the filing reminder is queued by
    :func:`~app.worker.tasks.schedule_compliance_reminders_task`, which is the
    one a beat schedule fires on its own and the one whose duplicate says "your
    GSTR-3B is due on the 20th" twice.
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
    :func:`hold_while_queueing`.
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
        hold_while_queueing(db, current_firm_id)
        firm_name = firms.name_of(db, current_firm_id)
        outstanding = documents.items_awaiting_documents(
            db, current_firm_id, from_date=run_date, to_date=horizon
        )
        # One query for the whole firm's due list rather than one per filing;
        # see :func:`_group_queued`.
        queued_already = queued_for_items(db, [item.id for item, _ in outstanding])
        for item, checklist in outstanding:
            days_left = (item.due_date - run_date).days
            if days_left not in offsets:
                continue
            client = item.client
            if client is None or not client.is_active:
                continue

            if already_queued(queued_already[item.id], "document", days_left):
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
    are read for the reason :func:`hold_while_queueing` gives.

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
        hold_while_queueing(db, current_firm_id)
        # Every message is signed by the firm that raised the invoice.
        firm_name = firms.name_of(db, current_firm_id)
        receivables = billing.unpaid_invoices(db, current_firm_id, today=run_date)
        # One query for the firm's whole receivables list; see :func:`_group_queued`.
        queued_already = queued_for_invoices(db, [inv.id for inv in receivables])
        for invoice in receivables:
            if invoice.due_date is None:
                continue
            days_overdue = (run_date - invoice.due_date).days
            if days_overdue not in offsets:
                continue
            client = invoice.client
            if client is None or not client.is_active:
                continue

            if already_queued(queued_already[invoice.id], "payment", days_overdue):
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


# ------------------------------------------------------- cancelling a chase --


def load_for_update(db: Session, reminder_id: uuid.UUID) -> Reminder | None:
    """Read a reminder with its row held for the rest of the transaction.

    Cancelling one is a read-decide-write over the same row the dispatcher
    claims: read the status, decide it is still ``SCHEDULED``, write
    ``CANCELLED``. The dispatcher takes that row with ``FOR UPDATE SKIP
    LOCKED``, sends the message, stamps ``SENT`` and commits — and a plain
    ``SELECT`` is not blocked by that lock, so the cancel read a status taken
    before the message went out and the ``UPDATE`` simply queued behind the
    send.

    What lands is a row saying ``cancelled`` with ``sent_at`` set beside it: the
    firm's own record of who it has contacted says this client was not written
    to, on a morning they were. Nothing in the trail contradicts it, because the
    cancel is the later write and the audit line says a practitioner stopped the
    chase.

    Held before the status is read, so the decision and the write are one step.
    ``populate_existing`` is the other half, for the reason
    :func:`~app.services.billing.load_for_update` gives: the session keeps
    loaded rows without expiring them on commit, so a second read would
    otherwise be answered out of the identity map with values older than the
    lock.
    """
    return db.scalars(
        select(Reminder)
        .where(Reminder.id == reminder_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def scheduled_for_client_for_update(
    db: Session, *, firm_id: uuid.UUID, client_id: uuid.UUID
) -> list[Reminder]:
    """A client's still-queued reminders, each row held. See :func:`load_for_update`.

    "Stop chasing this client" is the same race in bulk, and it is the one a
    practitioner reaches for when a client rings in — which is to say, in the
    morning, while the dispatcher is working through exactly these rows.

    Waited for rather than skipped: a row the dispatcher holds is one being sent
    right now, and the honest answer is to let the send finish and then find the
    reminder already ``SENT``. The caller re-checks the status per row, so what
    is reported cancelled is what was actually still waiting.
    """
    return list(
        db.scalars(
            select(Reminder)
            .where(
                Reminder.firm_id == firm_id,
                Reminder.client_id == client_id,
                Reminder.status == ReminderStatus.SCHEDULED,
            )
            # Ordered, so two callers queue behind each other on the rows in the
            # same order rather than crossing.
            .order_by(Reminder.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )


def cancel_scheduled(db: Session, *, firm_id: uuid.UUID, client_id: uuid.UUID) -> int:
    """Cancel everything still queued for a client. Returns how many were stopped.

    The rows are held while their status is read, for the reason
    :func:`scheduled_for_client_for_update` gives, and each is re-checked once
    the lock is ours so one the dispatcher has just sent is left as sent rather
    than recorded cancelled.

    Two callers: "stop chasing this client", which a practitioner reaches for
    when a client rings in, and off-boarding, which is the same instruction
    said once and for all.
    """
    pending = [
        reminder
        for reminder in scheduled_for_client_for_update(
            db, firm_id=firm_id, client_id=client_id
        )
        if reminder.status == ReminderStatus.SCHEDULED
    ]
    for reminder in pending:
        reminder.status = ReminderStatus.CANCELLED
    return len(pending)


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
