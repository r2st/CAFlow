"""Background jobs: compliance top-up, reminder scheduling and dispatch."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import SessionLocal
from app.models.base import (
    ComplianceStatus,
    ReminderChannel,
    ReminderStatus,
    ReminderType,
)
from app.models.client import Client
from app.models.compliance import ComplianceItem
from app.models.firm import Firm
from app.models.reminder import Reminder
from app.services import billing, delivery, mailer
from app.services import reminders as reminder_service
from app.services import tasks as task_service
from app.services.ai import DraftingBudget, draft_client_message
from app.services.compliance_generator import regenerate_for_firm
from app.services.reminders import IST_OFFSET, REMINDER_HOUR_IST
from app.services.reminders import ist_morning as _ist_morning
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

OPEN_STATUSES = (ComplianceStatus.PENDING, ComplianceStatus.IN_PROGRESS)

__all__ = [
    "REMINDER_HOUR_IST",
    "IST_OFFSET",
    "dispatch_due_reminders_task",
    "flag_overdue_task",
    "generate_compliance_items_task",
    "generate_tasks_task",
    "queue_document_reminders_task",
    "queue_payment_reminders_task",
    "refresh_invoice_statuses_task",
    "schedule_compliance_reminders_task",
]


@celery_app.task(name="caflow.generate_compliance_items")
def generate_compliance_items_task() -> dict[str, int]:
    """Extend every active firm's compliance items over the rolling window."""
    with SessionLocal() as db:
        total = 0
        firms = db.scalars(select(Firm).where(Firm.is_active.is_(True))).all()
        for firm in firms:
            total += regenerate_for_firm(db, firm.id)
        db.commit()
    logger.info("Generated %s compliance item(s) across %s firm(s)", total, len(firms))
    return {"created": total, "firms": len(firms)}


@celery_app.task(name="caflow.schedule_compliance_reminders")
def schedule_compliance_reminders_task(today: str | None = None) -> dict[str, int]:
    """Queue escalating reminders for filings whose deadline is approaching.

    Each compliance type declares ``reminder_offsets_days`` (e.g. [10, 5, 2, 1]);
    an item due in exactly that many days gets one reminder queued, once.
    """
    run_date = date.fromisoformat(today) if today else date.today()
    queued = 0
    # One blocking model call per reminder, and this task has ten minutes. See
    # DraftingBudget: past the allowance the wording comes from the template so
    # that the run finishes and the reminders exist.
    budget = DraftingBudget()

    with SessionLocal() as db:
        items = db.scalars(
            select(ComplianceItem)
            .join(Firm, Firm.id == ComplianceItem.firm_id)
            .options(
                selectinload(ComplianceItem.client),
                selectinload(ComplianceItem.compliance_type),
            )
            .where(
                # A firm switched off keeps its filings — they are its records,
                # not ours to delete — so sweeping the items alone chased the
                # clients of a firm this deployment had stopped serving.
                Firm.is_active.is_(True),
                ComplianceItem.status.in_(OPEN_STATUSES),
                ComplianceItem.due_date >= run_date,
                ComplianceItem.due_date <= run_date + timedelta(days=60),
            )
        ).all()

        for item in items:
            offsets = item.compliance_type.reminder_offsets_days or []
            days_left = (item.due_date - run_date).days
            if days_left not in offsets:
                continue
            client = item.client
            if client is None or not client.is_active:
                continue

            # One *filing* reminder per (item, offset) — a document chase for
            # the same filing on the same day is a different message and must
            # not silence this one. Checked in Python so the JSON predicate
            # behaves identically on PostgreSQL and SQLite.
            existing = list(
                db.scalars(select(Reminder).where(Reminder.compliance_item_id == item.id)).all()
            )
            if reminder_service.already_queued(existing, "filing", days_left):
                continue

            body = draft_client_message(
                purpose="document_request",
                client_name=client.name,
                context={
                    "compliance": item.compliance_type.name,
                    "period": item.period_label,
                    "due_date": item.due_date.isoformat(),
                    "documents": item.compliance_type.required_documents,
                },
                budget=budget,
            )
            db.add(
                Reminder(
                    firm_id=item.firm_id,
                    client_id=client.id,
                    compliance_item_id=item.id,
                    reminder_type=ReminderType.FILING,
                    channel=ReminderChannel.EMAIL,
                    status=ReminderStatus.SCHEDULED,
                    subject=(
                        f"{item.compliance_type.name} for {item.period_label} "
                        f"is due on {item.due_date:%d %b %Y}"
                    ),
                    body=body,
                    recipient=client.email,
                    scheduled_for=_ist_morning(run_date),
                    extra={"kind": "filing", "offset_days": days_left},
                )
            )
            queued += 1
        db.commit()

    logger.info("Queued %s reminder(s) for %s — %s", queued, run_date, budget.summary())
    return {"queued": queued}


def _deliverable(stmt, *, cutoff: datetime, exclude: set[uuid.UUID]):
    """Narrow ``stmt`` to reminders this deployment is still willing to send.

    Due, still scheduled, and — the part the queue cannot know when the row is
    written — belonging to a firm and a client that are both still active
    *now*. A document chase is queued up to fifteen days ahead and a filing
    reminder up to forty-five, so the state that was checked at queue time is
    old news by the time the message goes out: a firm switched off this morning
    had a fortnight of mail already sitting in the queue, addressed to its
    clients and signed with its name.

    Held rather than failed. Being suspended is a state a firm comes back from,
    and marking the queue ``FAILED`` would mean nothing resumes when it does.
    """
    return (
        stmt.join(Firm, Firm.id == Reminder.firm_id)
        .join(Client, Client.id == Reminder.client_id)
        .where(
            Reminder.status == ReminderStatus.SCHEDULED,
            Reminder.scheduled_for <= cutoff,
            Firm.is_active.is_(True),
            Client.is_active.is_(True),
            *([Reminder.id.not_in(exclude)] if exclude else []),
        )
    )


def _claim_next_due(
    db: Session, *, cutoff: datetime, exclude: set[uuid.UUID]
) -> Reminder | None:
    """Take exclusive hold of the oldest reminder that is due, or return None.

    ``FOR UPDATE SKIP LOCKED`` is what makes the claim exclusive: a row another
    dispatcher is already sending is passed over rather than waited for, so two
    runs divide the queue instead of both working through it. SQLite renders no
    locking clause at all, which is correct for a single-process test suite.

    ``OF reminders`` keeps the lock off the two tables joined only to be read.
    Without it PostgreSQL locks the matching ``firms`` row too, for as long as
    one message takes to send — and that is the same row a plan-limit check or
    an invoice number waits on, so every dispatch would stall the firm's own
    practitioners behind an SMTP handshake.

    ``exclude`` holds the reminders this run has already tried and left
    ``SCHEDULED`` for a later run; without it the very next iteration would
    select the same row again and retry it in a tight loop.
    """
    stmt = _deliverable(select(Reminder), cutoff=cutoff, exclude=exclude)
    return db.scalars(
        stmt.order_by(Reminder.scheduled_for, Reminder.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=Reminder)
    ).first()


def _undispatched_count(db: Session, *, cutoff: datetime, exclude: set[uuid.UUID]) -> int:
    """How many due reminders this run never got to. No lock — it only counts.

    Counts what the claim would have taken, so a firm on hold does not read as
    a backlog the next run is expected to clear.
    """
    stmt = _deliverable(
        select(func.count()).select_from(Reminder), cutoff=cutoff, exclude=exclude
    )
    return db.scalar(stmt) or 0


@celery_app.task(name="caflow.dispatch_due_reminders")
def dispatch_due_reminders_task(limit: int = 200) -> dict[str, int]:
    """Send reminders whose scheduled time has arrived.

    Email goes out over SMTP (see :mod:`app.services.mailer`); reminders queued
    for WhatsApp or SMS fall back to email until those adapters exist.

    A transient failure leaves the reminder ``SCHEDULED`` so the next run picks
    it up again, up to ``settings.reminder_max_attempts``. Permanent failures —
    a malformed address, a refused recipient — fail immediately rather than
    burning retries on something that cannot succeed.

    One reminder is claimed, sent and committed at a time, rather than the
    whole batch being read up front and written back at the end. Both halves of
    that matter, because sending mail is the one thing here that a rollback
    cannot take back:

    * **The claim** is a locked row, so a second dispatcher cannot pick it up.
      Beat fires this every fifteen minutes and the worker runs two processes,
      so a batch that outlives its interval — two hundred messages through a
      greylisting relay will — overlaps with the next run. Reading the same
      ``SCHEDULED`` rows twice meant mailing every client in the batch twice.
    * **The commit** is per reminder, so a crash costs at most the one message
      in flight. A single commit at the end meant a worker killed at message
      one hundred and fifty had sent a hundred and forty-nine emails and
      recorded none of them — and with ``task_acks_late`` the task is then
      redelivered, so all hundred and forty-nine go out again.

    What is left is an at-least-once window of exactly one message: a process
    killed between the SMTP handshake and the commit re-sends that one. That is
    the honest floor without a provider-side idempotency key, and it is three
    orders of magnitude better than the batch it replaces.
    """
    cutoff = datetime.now(UTC)
    sent = failed = retrying = 0
    # Tried this run and deliberately left SCHEDULED for the next one.
    deferred: set[uuid.UUID] = set()
    firms: dict[uuid.UUID, Firm | None] = {}
    claimed = 0

    with SessionLocal() as db:
        for _ in range(limit):
            reminder = _claim_next_due(db, cutoff=cutoff, exclude=deferred)
            if reminder is None:
                break
            claimed += 1
            try:
                outcome = _attempt_delivery(db, reminder, firms)
            except Exception:
                # The claim is released and the reminder stays exactly as it
                # was found, which is the only safe reading of "we do not know
                # whether that went out".
                db.rollback()
                raise
            if outcome == "sent":
                sent += 1
            elif outcome == "failed":
                failed += 1
            else:
                retrying += 1
                deferred.add(reminder.id)
            # Ends the transaction, so the outcome is durable and the row lock
            # is held only for the length of one delivery.
            db.commit()

        # Only asked when the loop ran out of iterations rather than out of
        # work, and only counts what was never looked at — a reminder deferred
        # for a transient failure waits for the next run by design, and
        # counting it here would make every greylisted message look like a
        # backlog.
        overflow = (
            _undispatched_count(db, cutoff=cutoff, exclude=deferred)
            if claimed == limit
            else 0
        )

    if overflow:
        logger.warning(
            "Dispatch stopped at its batch limit of %s with %s reminder(s) still due; "
            "they wait for the next run",
            limit,
            overflow,
        )
    logger.info(
        "Dispatched %s reminder(s), %s failed, %s awaiting retry", sent, failed, retrying
    )
    return {"sent": sent, "failed": failed, "retrying": retrying}


def _attempt_delivery(
    db: Session, reminder: Reminder, firms: dict[uuid.UUID, Firm | None]
) -> str:
    """Try to send one claimed reminder. Returns "sent", "failed" or "retrying"."""
    reminder.attempt_count += 1
    if not reminder.recipient:
        reminder.status = ReminderStatus.FAILED
        reminder.error_message = "No recipient address on file for this client"
        return "failed"

    if reminder.firm_id not in firms:
        firms[reminder.firm_id] = db.get(Firm, reminder.firm_id)

    try:
        outcome = delivery.deliver(reminder, firms[reminder.firm_id])
    except (mailer.DeliveryError, delivery.NoTransport) as exc:
        permanent = getattr(exc, "permanent", True) or (
            reminder.attempt_count >= settings.reminder_max_attempts
        )
        reminder.error_message = str(exc)
        if permanent:
            reminder.status = ReminderStatus.FAILED
            return "failed"
        # Stays SCHEDULED; the next dispatcher run tries again.
        return "retrying"

    reminder.status = ReminderStatus.SENT
    reminder.sent_at = datetime.now(UTC)
    reminder.error_message = None
    logger.debug("Reminder %s: %s", reminder.id, outcome.detail)
    return "sent"


@celery_app.task(name="caflow.mark_overdue_clients")
def flag_overdue_task() -> dict[str, int]:
    """Count clients with at least one overdue filing (used by the dashboard)."""
    today = date.today()
    with SessionLocal() as db:
        overdue_client_ids = set(
            db.scalars(
                select(ComplianceItem.client_id).where(
                    ComplianceItem.status.in_(OPEN_STATUSES),
                    ComplianceItem.due_date < today,
                )
            ).all()
        )
        active = db.scalar(select(Client.id).where(Client.is_active.is_(True)))
    return {"clients_with_overdue": len(overdue_client_ids), "has_active_clients": bool(active)}


@celery_app.task(name="caflow.queue_document_reminders")
def queue_document_reminders_task(today: str | None = None) -> dict[str, int]:
    """Chase clients for documents their upcoming filings are still missing.

    Only fires while something is genuinely outstanding, so a client who has
    already uploaded everything hears nothing.
    """
    run_date = date.fromisoformat(today) if today else date.today()
    budget = DraftingBudget()
    with SessionLocal() as db:
        queued = reminder_service.queue_document_reminders(db, today=run_date, budget=budget)
        count = len(queued)
        db.commit()
    logger.info(
        "Queued %s document-collection reminder(s) for %s — %s",
        count,
        run_date,
        budget.summary(),
    )
    return {"queued": count}


@celery_app.task(name="caflow.queue_payment_reminders")
def queue_payment_reminders_task(today: str | None = None) -> dict[str, int]:
    """Chase unpaid invoices at each configured day past the due date."""
    run_date = date.fromisoformat(today) if today else date.today()
    budget = DraftingBudget()
    with SessionLocal() as db:
        queued = reminder_service.queue_payment_reminders(db, today=run_date, budget=budget)
        count = len(queued)
        db.commit()
    logger.info(
        "Queued %s payment reminder(s) for %s — %s", count, run_date, budget.summary()
    )
    return {"queued": count}


@celery_app.task(name="caflow.generate_tasks")
def generate_tasks_task(today: str | None = None, horizon_days: int = 21) -> dict[str, int]:
    """Materialise tasks for filings coming due inside the planning horizon."""
    run_date = date.fromisoformat(today) if today else date.today()
    with SessionLocal() as db:
        firms = db.scalars(select(Firm).where(Firm.is_active.is_(True))).all()
        total = 0
        for firm in firms:
            total += len(
                task_service.create_tasks_for_due_items(
                    db, firm.id, today=run_date, horizon_days=horizon_days
                )
            )
        db.commit()
    logger.info("Created %s task(s) across %s firm(s)", total, len(firms))
    return {"created": total, "firms": len(firms)}


@celery_app.task(name="caflow.refresh_invoice_statuses")
def refresh_invoice_statuses_task(today: str | None = None) -> dict[str, int]:
    """Flip sent invoices to overdue once their due date passes."""
    run_date = date.fromisoformat(today) if today else date.today()
    with SessionLocal() as db:
        invoices = billing.unpaid_invoices(db, today=run_date)
        changed = 0
        for invoice in invoices:
            before = invoice.status
            billing.refresh_status(invoice, run_date)
            if invoice.status != before:
                changed += 1
        db.commit()
    logger.info("Refreshed %s invoice status(es) of %s open", changed, len(invoices))
    return {"updated": changed, "open": len(invoices)}
