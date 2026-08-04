"""Background jobs: compliance top-up, reminder scheduling and dispatch."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.core import clock
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
from app.services import firms as firm_service  # `firms` is a local name below
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
    """Extend every active firm's compliance items over the rolling window.

    Ordered by id, because ``generate_compliance_items`` now holds each firm's
    row and this sweep holds all of them at once — the transaction spans every
    firm. Two runs walking an unordered list can each hold what the other wants
    next; walking the same order means one queues behind the other instead.
    It is the same ordering ``generate_tasks_task`` takes, for the same reason.
    """
    with SessionLocal() as db:
        total = 0
        firms = db.scalars(
            select(Firm).where(Firm.is_active.is_(True)).order_by(Firm.id)
        ).all()
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

    Walked firm by firm, each firm's row held before its own queue is read.
    "Once" was a read-decide-write nothing ordered — read what is already
    queued for this filing, decide there is nothing, add one — and this run
    does not commit until it has been through every tenant, spending up to
    ``ai_draft_budget_seconds`` on the wording along the way. Two runs inside
    that gap both read an empty queue and both add, and beat firing while a
    previous run is still drafting is the ordinary way to get two: Celery
    acknowledges this task only once it finishes, so a redelivery overlaps it
    too. What came out was a client told twice, on the same morning and over
    their own CA's name, that the same return is due on the 20th.

    The other two sweeps were ordered for exactly this in
    :func:`~app.services.reminders.hold_while_queueing`; the filing reminder is
    the one that lives here, and it was left behind.
    """
    run_date = date.fromisoformat(today) if today else clock.today()
    queued = 0
    # One blocking model call per reminder, and this task has ten minutes. See
    # DraftingBudget: past the allowance the wording comes from the template so
    # that the run finishes and the reminders exist.
    budget = DraftingBudget()

    with SessionLocal() as db:
        # A firm switched off keeps its filings — they are its records, not
        # ours to delete — so sweeping the items alone chased the clients of a
        # firm this deployment had stopped serving. Sorted, so two runs queue
        # behind each other on the locks rather than crossing.
        for firm_id in sorted(firm_service.servable_firm_ids(db)):
            reminder_service.hold_while_queueing(db, firm_id)
            # Each message is signed by the firm whose client is being written
            # to; resolved once per firm now that the walk is per firm.
            firm_name = firm_service.name_of(db, firm_id)
            items = db.scalars(
                select(ComplianceItem)
                .options(
                    selectinload(ComplianceItem.client),
                    selectinload(ComplianceItem.compliance_type),
                )
                .where(
                    ComplianceItem.firm_id == firm_id,
                    ComplianceItem.status.in_(OPEN_STATUSES),
                    ComplianceItem.due_date >= run_date,
                    ComplianceItem.due_date <= run_date + timedelta(days=60),
                )
            ).all()
            # One query for the firm's whole due list rather than one per
            # filing; see ``reminders._group_queued``.
            queued_already = reminder_service.queued_for_items(
                db, [item.id for item in items]
            )

            for item in items:
                offsets = item.compliance_type.reminder_offsets_days or []
                days_left = (item.due_date - run_date).days
                if days_left not in offsets:
                    continue
                client = item.client
                if client is None or not client.is_active:
                    continue

                # One *filing* reminder per (item, offset) — a document chase
                # for the same filing on the same day is a different message
                # and must not silence this one. Checked in Python so the JSON
                # predicate behaves identically on PostgreSQL and SQLite.
                if reminder_service.already_queued(
                    queued_already[item.id], "filing", days_left
                ):
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
                    firm_name=firm_name,
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
    sent = failed = retrying = withdrawn = 0
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
            elif outcome == "withdrawn":
                withdrawn += 1
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
        "Dispatched %s reminder(s), %s failed, %s awaiting retry, %s withdrawn",
        sent,
        failed,
        retrying,
        withdrawn,
    )
    return {
        "sent": sent,
        "failed": failed,
        "retrying": retrying,
        "withdrawn": withdrawn,
    }


def _attempt_delivery(
    db: Session, reminder: Reminder, firms: dict[uuid.UUID, Firm | None]
) -> str:
    """Try to send one claimed reminder.

    Returns "sent", "failed", "retrying" or "withdrawn".
    """
    # Before the attempt is counted, because no attempt is made: the reason the
    # chase was queued has been answered or has gone away since, and sending it
    # would tell the client something the firm's own records contradict. See
    # ``reminders.withdrawn_reason``. Cancelled rather than left scheduled —
    # this is the same conclusion a practitioner reaches by hand, and it is not
    # a state the reminder comes back from the way a firm on hold does.
    withdrawn = reminder_service.withdrawn_reason(db, reminder)
    if withdrawn is not None:
        reminder.status = ReminderStatus.CANCELLED
        # Reassigned rather than mutated: ``extra`` is a JSON column, and an
        # in-place update is not a change the session would notice.
        reminder.extra = {**(reminder.extra or {}), "withdrawn_because": withdrawn}
        logger.info("Withdrew reminder %s before sending: %s", reminder.id, withdrawn)
        return "withdrawn"

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
    today = clock.today()
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
    run_date = date.fromisoformat(today) if today else clock.today()
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
    run_date = date.fromisoformat(today) if today else clock.today()
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
    """Materialise tasks for filings coming due inside the planning horizon.

    Ordered by id, because ``create_tasks_for_due_items`` now holds each firm's
    row and this sweep holds all of them at once — the transaction spans every
    firm. Two runs walking an unordered list can each hold what the other wants
    next; walking the same order means one queues behind the other instead.
    """
    run_date = date.fromisoformat(today) if today else clock.today()
    with SessionLocal() as db:
        firms = db.scalars(
            select(Firm).where(Firm.is_active.is_(True)).order_by(Firm.id)
        ).all()
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
    """Flip sent invoices to overdue once their due date passes.

    Each invoice is claimed and decided one at a time, for the reason the four
    endpoints that change one already are — see ``billing.load_for_update``.
    This was the fifth writer of ``Invoice.status`` and the only one left
    deciding from a copy: it read every open invoice on the deployment up
    front, derived a status for each from what that copy said, and wrote them
    all back at a single commit at the end of the walk.

    The walk is the window. ``refresh_status`` is derived entirely from
    ``total_paise``, ``amount_paid_paise`` and the due date, so a copy taken
    before a concurrent request committed is a copy that derives the wrong
    answer — and it is the *later* write, so it lands on top:

    * a client pays in full at 02:31 and the sweep, holding a copy that says
      nothing was paid, writes ``overdue`` back over the ``paid`` the receipt
      had just recorded;
    * an invoice cancelled inside the window is worse, because cancelling
      releases the filings it covered back into the billable pool.
      ``refresh_status`` leaves a cancelled invoice alone — but only when it
      can see that it is cancelled, and a copy taken beforehand says ``sent``.
      What lands is a withdrawn invoice back in an owed state, chased by the
      payment sweep for money the firm decided not to ask for, while the work
      it cites is also sitting on the billable list waiting to be invoiced
      again.

    Neither is a race that needs a busy deployment: 02:30 is when this runs and
    the whole of it is one transaction, so the window is the length of the
    walk rather than an instant.

    The ids are read without a lock, because they are only a list of what to
    look at; each row is then re-read under ``FOR UPDATE`` and committed on its
    own, which keeps the hold to one invoice for the length of one derivation.
    A row that has gone since the ids were read is simply skipped — the
    foreign keys cascade, and there is nothing to relabel.
    """
    run_date = date.fromisoformat(today) if today else clock.today()
    changed = 0
    with SessionLocal() as db:
        invoice_ids = billing.open_invoice_ids(db)
        for invoice_id in invoice_ids:
            invoice = billing.load_for_update(db, invoice_id)
            if invoice is not None:
                before = invoice.status
                billing.refresh_status(invoice, run_date)
                if invoice.status != before:
                    changed += 1
            # Ends the transaction either way, so the lock is released and the
            # next claim starts from what the database says now.
            db.commit()
    logger.info("Refreshed %s invoice status(es) of %s open", changed, len(invoice_ids))
    return {"updated": changed, "open": len(invoice_ids)}
