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
from app.models.base import ReminderChannel, ReminderStatus, ReminderType
from app.models.client import Client
from app.models.reminder import Reminder
from app.services import billing, documents, firms
from app.services.ai import DraftingBudget, draft_client_message

# Reminders go out at 09:00 IST on their offset day.
REMINDER_HOUR_IST = 9
IST_OFFSET = timedelta(hours=5, minutes=30)


def ist_morning(day: date) -> datetime:
    """09:00 IST on ``day``, as an aware UTC datetime."""
    naive_ist = datetime.combine(day, time(hour=REMINDER_HOUR_IST))
    return (naive_ist - IST_OFFSET).replace(tzinfo=UTC)


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


def _kind_of(reminder: Reminder) -> str:
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
        _kind_of(r) == kind and (r.extra or {}).get("offset_days") == offset
        for r in existing
    )


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
    """
    run_date = today or date.today()
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
    reason it does above.
    """
    run_date = today or date.today()
    offsets = offsets if offsets is not None else settings.payment_reminder_offsets
    budget = budget if budget is not None else DraftingBudget()
    if not offsets:
        return []

    servable = firms.servable_firm_ids(db, firm_id)
    queued: list[Reminder] = []
    for invoice in billing.unpaid_invoices(db, firm_id, today=run_date):
        # A switched-off firm's debts are still owed; chasing them in its name
        # is not ours to do while it is not being served.
        if invoice.firm_id not in servable:
            continue
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
