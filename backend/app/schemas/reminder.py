"""Reminder schemas."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, Field

from app.core import clock
from app.models.base import ReminderChannel, ReminderStatus, ReminderType
from app.schemas.common import (
    DraftContext,
    MessageBody,
    ORMModel,
    SanitizedModel,
)


def as_instant(value: datetime | None) -> datetime | None:
    """Pin a caller's timestamp to an instant, reading a bare one as IST.

    ``scheduled_for`` is the only datetime this API takes *in*, and nothing
    required it to carry an offset. A timestamp without one names a wall clock
    and not a moment, so what it meant was decided by whoever read it next —
    and none of the readers agreed:

    * PostgreSQL casts a naive value into ``timestamptz`` using the session's
      ``TimeZone``, which nothing here sets. The stored instant therefore
      depended on how the database server happened to be configured;
    * SQLAlchemy's SQLite dialect drops ``tzinfo`` when it writes, and
      everything that reads a stored timestamp back — ``clock.to_ist`` says so
      plainly — treats a naive one as UTC.

    So the value a practitioner typed as 09:00 became 09:00 UTC, which is 14:30
    in the office that typed it. A reminder set for first thing in the morning
    went out in the middle of the afternoon, and one set for the evening of a
    deadline went out after it had passed.

    Read as IST, because that is the clock this product runs on and the one the
    person filling in the field is looking at — the same reading
    ``reminders.ist_morning``, ``audit._day_bounds`` and
    ``tasks._month_start_instant`` already take when an Indian wall-clock time
    has to become an instant. An offset the caller *did* send is believed and
    simply re-expressed, so both backends store the same moment either way.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=clock.IST)
    return value.astimezone(UTC)


Instant = Annotated[datetime, AfterValidator(as_instant)]


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
    # Pinned to an instant on the way in; a bare timestamp is Indian office
    # time. See :func:`as_instant`.
    scheduled_for: Instant | None = None
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
