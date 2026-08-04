"""Reminder schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.models.base import ReminderChannel, ReminderStatus, ReminderType
from app.schemas.common import (
    DraftContext,
    MessageBody,
    ORMModel,
    SanitizedModel,
)


class ReminderOut(ORMModel):
    id: uuid.UUID
    firm_id: uuid.UUID
    client_id: uuid.UUID
    compliance_item_id: uuid.UUID | None
    invoice_id: uuid.UUID | None
    reminder_type: ReminderType
    channel: ReminderChannel
    status: ReminderStatus
    subject: str | None
    body: str | None
    recipient: str | None
    scheduled_for: datetime
    sent_at: datetime | None
    attempt_count: int
    error_message: str | None
    extra: dict
    created_at: datetime

    client_name: str | None = None


class ReminderCreate(SanitizedModel):
    client_id: uuid.UUID
    reminder_type: ReminderType = ReminderType.CUSTOM
    channel: ReminderChannel | None = None
    subject: str | None = Field(default=None, max_length=512)
    body: MessageBody | None = None
    scheduled_for: datetime | None = None
    compliance_item_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None


class ReminderDraftRequest(SanitizedModel):
    """Ask the AI to draft a message; nothing is queued until it is created."""

    client_id: uuid.UUID
    purpose: str = Field(default="document_request", max_length=64)
    compliance_item_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None
    # Bounded in size and shape: every entry is rendered into the prompt that
    # goes to OpenRouter. See :data:`~app.schemas.common.DraftContext`.
    extra_context: DraftContext = Field(default_factory=dict)


class ReminderDraftOut(SanitizedModel):
    subject: str
    body: str
    channel: ReminderChannel
    recipient: str | None


class ReminderQueueRequest(SanitizedModel):
    """Run the automated sweeps on demand rather than waiting for the beat."""

    kind: str = Field(default="document", pattern="^(document|payment)$")


class ReminderQueueResponse(SanitizedModel):
    kind: str
    queued: int
    reminders: list[ReminderOut]


class ReminderCancelResponse(SanitizedModel):
    cancelled: int
