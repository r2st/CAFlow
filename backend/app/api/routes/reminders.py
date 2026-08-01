"""Client communication: AI-drafted reminders, the queue, and manual sends."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentPractitioner, DbSession, Manager
from app.models.base import ReminderChannel, ReminderStatus, ReminderType
from app.models.client import Client
from app.models.compliance import ComplianceItem
from app.models.invoice import Invoice
from app.models.reminder import Reminder
from app.schemas.common import Page
from app.schemas.reminder import (
    ReminderCancelResponse,
    ReminderCreate,
    ReminderDraftOut,
    ReminderDraftRequest,
    ReminderOut,
    ReminderQueueRequest,
    ReminderQueueResponse,
)
from app.services import audit
from app.services import documents as document_service
from app.services import reminders as reminder_service
from app.services.ai import draft_client_message

router = APIRouter(prefix="/reminders", tags=["reminders"])


def _get_client_or_404(db: Session, firm_id: uuid.UUID, client_id: uuid.UUID) -> Client:
    client = db.get(Client, client_id)
    if client is None or client.firm_id != firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return client


def serialise(reminder: Reminder) -> ReminderOut:
    out = ReminderOut.model_validate(reminder)
    out.client_name = reminder.client.name if reminder.client else None
    return out


# ------------------------------------------------------------------ drafting --


@router.post(
    "/draft",
    response_model=ReminderDraftOut,
    summary="Draft reminder copy without sending it",
)
def draft_reminder(
    payload: ReminderDraftRequest, practitioner: CurrentPractitioner, db: DbSession
):
    """Ask the AI for a message body. Nothing is queued until you create it.

    Falls back to a deterministic template when no OpenRouter key is set, so
    the endpoint always returns something usable.
    """
    client = _get_client_or_404(db, practitioner.firm_id, payload.client_id)
    context: dict = dict(payload.extra_context)
    subject = "A message from your CA"

    if payload.compliance_item_id is not None:
        item = db.get(ComplianceItem, payload.compliance_item_id)
        if item is None or item.firm_id != practitioner.firm_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Compliance item not found"
            )
        checklist = document_service.checklist_for_item(db, item)
        missing = [document_service.requirement_label(r) for r in checklist.missing]
        context.update(
            {
                "compliance": item.compliance_type.name,
                "period": item.period_label,
                "due_date": item.due_date.isoformat(),
                "documents": missing,
                "acknowledgement_number": item.acknowledgement_number,
            }
        )
        subject = f"{item.compliance_type.name} — {item.period_label}"

    if payload.invoice_id is not None:
        invoice = db.get(Invoice, payload.invoice_id)
        if invoice is None or invoice.firm_id != practitioner.firm_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found"
            )
        context.update(
            {
                "invoice_number": invoice.invoice_number,
                "amount_inr": f"{invoice.balance_paise / 100:,.2f}",
                "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
            }
        )
        subject = f"Invoice {invoice.invoice_number}"

    channel = reminder_service.preferred_channel(client)
    body = draft_client_message(
        purpose=payload.purpose,
        client_name=client.name,
        context=context,
        channel=channel.value,
    )
    return ReminderDraftOut(
        subject=subject,
        body=body,
        channel=channel,
        recipient=reminder_service.recipient_for(client, channel),
    )


@router.post(
    "/queue",
    response_model=ReminderQueueResponse,
    summary="Queue the reminders due today",
)
def queue_automated_reminders(
    payload: ReminderQueueRequest, practitioner: Manager, db: DbSession
):
    """Run a reminder sweep now instead of waiting for the nightly beat."""
    if payload.kind == "document":
        queued = reminder_service.queue_document_reminders(db, firm_id=practitioner.firm_id)
    else:
        queued = reminder_service.queue_payment_reminders(db, firm_id=practitioner.firm_id)

    audit.record(
        db,
        action="reminder.queue",
        entity_type="reminder",
        actor=practitioner,
        summary=f"Queued {len(queued)} {payload.kind} reminder(s) on demand",
    )
    db.commit()
    for reminder in queued:
        db.refresh(reminder)
    return ReminderQueueResponse(
        kind=payload.kind,
        queued=len(queued),
        reminders=[serialise(reminder) for reminder in queued],
    )


# --------------------------------------------------------------------- CRUD --


@router.post(
    "",
    response_model=ReminderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Schedule a reminder",
)
def create_reminder(
    payload: ReminderCreate, practitioner: CurrentPractitioner, db: DbSession
):
    """Queue a one-off reminder composed by a practitioner."""
    client = _get_client_or_404(db, practitioner.firm_id, payload.client_id)
    reminder = reminder_service.build_manual_reminder(
        db,
        client=client,
        reminder_type=payload.reminder_type,
        subject=payload.subject,
        body=payload.body,
        channel=payload.channel,
        scheduled_for=payload.scheduled_for,
        compliance_item_id=payload.compliance_item_id,
        invoice_id=payload.invoice_id,
    )
    if not reminder.recipient:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{client.name} has no {reminder.channel.value} address on file",
        )

    audit.record(
        db,
        action="reminder.create",
        entity_type="reminder",
        entity_id=reminder.id,
        actor=practitioner,
        summary=f"Queued a {reminder.channel.value} reminder to {client.name}",
    )
    db.commit()
    db.refresh(reminder)
    return serialise(reminder)


@router.get("", response_model=Page[ReminderOut], summary="List reminders")
def list_reminders(
    practitioner: CurrentPractitioner,
    db: DbSession,
    client_id: uuid.UUID | None = Query(default=None),
    reminder_type: ReminderType | None = Query(default=None),
    reminder_status: ReminderStatus | None = Query(default=None),
    channel: ReminderChannel | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    filters = [Reminder.firm_id == practitioner.firm_id]
    if client_id is not None:
        filters.append(Reminder.client_id == client_id)
    if reminder_type is not None:
        filters.append(Reminder.reminder_type == reminder_type)
    if reminder_status is not None:
        filters.append(Reminder.status == reminder_status)
    if channel is not None:
        filters.append(Reminder.channel == channel)

    total = db.scalar(select(func.count(Reminder.id)).where(*filters)) or 0
    rows = db.scalars(
        select(Reminder)
        .options(selectinload(Reminder.client))
        .where(*filters)
        .order_by(Reminder.scheduled_for.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[ReminderOut](
        items=[serialise(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.post(
    "/{reminder_id}/cancel",
    response_model=ReminderOut,
    summary="Cancel a scheduled reminder",
)
def cancel_reminder(
    reminder_id: uuid.UUID, practitioner: CurrentPractitioner, db: DbSession
):
    reminder = db.get(Reminder, reminder_id)
    if reminder is None or reminder.firm_id != practitioner.firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reminder not found")
    if reminder.status != ReminderStatus.SCHEDULED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This reminder is already {reminder.status.value}",
        )

    reminder.status = ReminderStatus.CANCELLED
    audit.record(
        db,
        action="reminder.cancel",
        entity_type="reminder",
        entity_id=reminder.id,
        actor=practitioner,
        summary="Cancelled a scheduled reminder",
    )
    db.commit()
    db.refresh(reminder)
    return serialise(reminder)


@router.post(
    "/cancel-scheduled",
    response_model=ReminderCancelResponse,
    summary="Cancel every scheduled reminder for a client",
)
def cancel_scheduled_for_client(
    practitioner: Manager,
    db: DbSession,
    client_id: uuid.UUID = Query(...),
):
    """Stop chasing a client — cancels everything still queued for them."""
    client = _get_client_or_404(db, practitioner.firm_id, client_id)
    pending = list(
        db.scalars(
            select(Reminder).where(
                Reminder.firm_id == practitioner.firm_id,
                Reminder.client_id == client.id,
                Reminder.status == ReminderStatus.SCHEDULED,
            )
        ).all()
    )
    for reminder in pending:
        reminder.status = ReminderStatus.CANCELLED

    audit.record(
        db,
        action="reminder.cancel_scheduled",
        entity_type="client",
        entity_id=client.id,
        actor=practitioner,
        summary=f"Cancelled {len(pending)} scheduled reminder(s) for {client.name}",
    )
    db.commit()
    return ReminderCancelResponse(cancelled=len(pending))


@router.get("/pending-count", summary="How many reminders are waiting to go out")
def pending_count(practitioner: CurrentPractitioner, db: DbSession) -> dict[str, int]:
    """Badge counts for the reminders nav item."""
    now = datetime.now(UTC)
    scheduled = (
        db.scalar(
            select(func.count(Reminder.id)).where(
                Reminder.firm_id == practitioner.firm_id,
                Reminder.status == ReminderStatus.SCHEDULED,
            )
        )
        or 0
    )
    due_now = (
        db.scalar(
            select(func.count(Reminder.id)).where(
                Reminder.firm_id == practitioner.firm_id,
                Reminder.status == ReminderStatus.SCHEDULED,
                Reminder.scheduled_for <= now,
            )
        )
        or 0
    )
    return {"scheduled": scheduled, "due_now": due_now}
