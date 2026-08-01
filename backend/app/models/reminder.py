"""Scheduled / sent client reminders."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import (
    EnumString,
    JSONType,
    ReminderChannel,
    ReminderStatus,
    ReminderType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    UuidType,
)
from app.models.client import Client


class Reminder(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "reminders"
    __table_args__ = (
        Index("ix_reminder_status_scheduled", "status", "scheduled_for"),
        Index("ix_reminder_firm_client", "firm_id", "client_id"),
    )

    firm_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("firms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    compliance_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("compliance_items.id", ondelete="CASCADE"), index=True
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("invoices.id", ondelete="CASCADE"), index=True
    )

    reminder_type: Mapped[ReminderType] = mapped_column(
        EnumString(ReminderType, 32), nullable=False
    )
    channel: Mapped[ReminderChannel] = mapped_column(
        EnumString(ReminderChannel, 32), default=ReminderChannel.EMAIL, nullable=False
    )
    status: Mapped[ReminderStatus] = mapped_column(
        EnumString(ReminderStatus, 32), default=ReminderStatus.SCHEDULED, nullable=False
    )

    subject: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str | None] = mapped_column(Text)
    recipient: Mapped[str | None] = mapped_column(String(255))

    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    client: Mapped[Client] = relationship()
