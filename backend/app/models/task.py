"""Internal work items, usually derived from a compliance deadline."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import (
    EnumString,
    TaskPriority,
    TaskStatus,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    UuidType,
)
from app.models.compliance import ComplianceItem
from app.models.firm import Practitioner


class Task(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_task_firm_status", "firm_id", "status"),
        Index("ix_task_assignee_due", "assignee_id", "due_date"),
    )

    firm_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("firms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    compliance_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("compliance_items.id", ondelete="CASCADE"), index=True
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("practitioners.id", ondelete="SET NULL"), index=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("practitioners.id", ondelete="SET NULL")
    )

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        EnumString(TaskStatus, 32), default=TaskStatus.TODO, nullable=False
    )
    priority: Mapped[TaskPriority] = mapped_column(
        EnumString(TaskPriority, 16), default=TaskPriority.NORMAL, nullable=False
    )
    due_date: Mapped[date | None] = mapped_column(Date)
    estimated_minutes: Mapped[int | None] = mapped_column(Integer)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    compliance_item: Mapped[ComplianceItem | None] = relationship()
    assignee: Mapped[Practitioner | None] = relationship(foreign_keys=[assignee_id])
