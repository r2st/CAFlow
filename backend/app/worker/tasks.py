"""Background jobs: compliance top-up, reminder scheduling and dispatch."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

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
from app.services.ai import draft_client_message
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

    with SessionLocal() as db:
        items = db.scalars(
            select(ComplianceItem)
            .options(
                selectinload(ComplianceItem.client),
                selectinload(ComplianceItem.compliance_type),
            )
            .where(
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

            # One reminder per (item, offset) — checked in Python so the JSON
            # predicate behaves identically on PostgreSQL and SQLite.
            existing = db.scalars(
                select(Reminder).where(Reminder.compliance_item_id == item.id)
            ).all()
            if any((r.extra or {}).get("offset_days") == days_left for r in existing):
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
                    extra={"offset_days": days_left},
                )
            )
            queued += 1
        db.commit()

    logger.info("Queued %s reminder(s) for %s", queued, run_date)
    return {"queued": queued}


@celery_app.task(name="caflow.dispatch_due_reminders")
def dispatch_due_reminders_task(limit: int = 200) -> dict[str, int]:
    """Send reminders whose scheduled time has arrived.

    Email goes out over SMTP (see :mod:`app.services.mailer`); reminders queued
    for WhatsApp or SMS fall back to email until those adapters exist.

    A transient failure leaves the reminder ``SCHEDULED`` so the next run picks
    it up again, up to ``settings.reminder_max_attempts``. Permanent failures —
    a malformed address, a refused recipient — fail immediately rather than
    burning retries on something that cannot succeed.
    """
    now = datetime.now(UTC)
    sent = failed = retrying = 0

    with SessionLocal() as db:
        due = db.scalars(
            select(Reminder)
            .options(selectinload(Reminder.client))
            .where(
                Reminder.status == ReminderStatus.SCHEDULED,
                Reminder.scheduled_for <= now,
            )
            .limit(limit)
        ).all()

        firms: dict[uuid.UUID, Firm | None] = {}
        for reminder in due:
            reminder.attempt_count += 1
            if not reminder.recipient:
                reminder.status = ReminderStatus.FAILED
                reminder.error_message = "No recipient address on file for this client"
                failed += 1
                continue

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
                    failed += 1
                else:
                    # Stays SCHEDULED; the next dispatcher run tries again.
                    retrying += 1
                continue

            reminder.status = ReminderStatus.SENT
            reminder.sent_at = now
            reminder.error_message = None
            logger.debug("Reminder %s: %s", reminder.id, outcome.detail)
            sent += 1
        db.commit()

    logger.info(
        "Dispatched %s reminder(s), %s failed, %s awaiting retry", sent, failed, retrying
    )
    return {"sent": sent, "failed": failed, "retrying": retrying}


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
    with SessionLocal() as db:
        queued = reminder_service.queue_document_reminders(db, today=run_date)
        count = len(queued)
        db.commit()
    logger.info("Queued %s document-collection reminder(s) for %s", count, run_date)
    return {"queued": count}


@celery_app.task(name="caflow.queue_payment_reminders")
def queue_payment_reminders_task(today: str | None = None) -> dict[str, int]:
    """Chase unpaid invoices at each configured day past the due date."""
    run_date = date.fromisoformat(today) if today else date.today()
    with SessionLocal() as db:
        queued = reminder_service.queue_payment_reminders(db, today=run_date)
        count = len(queued)
        db.commit()
    logger.info("Queued %s payment reminder(s) for %s", count, run_date)
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
