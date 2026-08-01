"""Background jobs: compliance top-up, reminder scheduling and dispatch."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

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
from app.services.ai import draft_client_message
from app.services.compliance_generator import regenerate_for_firm
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

# Reminders are queued for 09:00 IST on the offset day.
REMINDER_HOUR_IST = 9
IST_OFFSET = timedelta(hours=5, minutes=30)

OPEN_STATUSES = (ComplianceStatus.PENDING, ComplianceStatus.IN_PROGRESS)


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

    Delivery adapters (SMTP, MSG91, WhatsApp Business API) are not wired up yet;
    until they are, this marks reminders as sent so the audit trail is intact and
    the same reminder is not re-queued.
    """
    now = datetime.now(UTC)
    sent = failed = 0

    with SessionLocal() as db:
        due = db.scalars(
            select(Reminder)
            .where(
                Reminder.status == ReminderStatus.SCHEDULED,
                Reminder.scheduled_for <= now,
            )
            .limit(limit)
        ).all()

        for reminder in due:
            reminder.attempt_count += 1
            if not reminder.recipient:
                reminder.status = ReminderStatus.FAILED
                reminder.error_message = "No recipient address on file for this client"
                failed += 1
                continue
            # TODO: plug in the real delivery adapter per reminder.channel.
            reminder.status = ReminderStatus.SENT
            reminder.sent_at = now
            sent += 1
        db.commit()

    logger.info("Dispatched %s reminder(s), %s failed", sent, failed)
    return {"sent": sent, "failed": failed}


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


def _ist_morning(day: date) -> datetime:
    """09:00 IST on ``day``, as an aware UTC datetime."""
    naive_ist = datetime.combine(day, time(hour=REMINDER_HOUR_IST))
    return (naive_ist - IST_OFFSET).replace(tzinfo=UTC)
