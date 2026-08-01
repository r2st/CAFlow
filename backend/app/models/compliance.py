"""Compliance types (the statutory calendar) and per-client compliance items."""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import (
    ComplianceCategory,
    ComplianceStatus,
    EnumString,
    Frequency,
    JSONType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    UuidType,
)

if TYPE_CHECKING:
    from app.models.client import Client
    from app.models.firm import Practitioner


class ComplianceType(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A recurring statutory obligation, e.g. "GSTR-3B — monthly, due 20th".

    Rows with ``firm_id IS NULL`` are the system-seeded Indian compliance
    calendar and are visible to every firm. A firm may add its own types.
    """

    __tablename__ = "compliance_types"
    __table_args__ = (UniqueConstraint("firm_id", "code", name="uq_compliance_type_firm_code"),)

    firm_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("firms.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[ComplianceCategory] = mapped_column(
        EnumString(ComplianceCategory, 32), nullable=False, index=True
    )
    frequency: Mapped[Frequency] = mapped_column(EnumString(Frequency, 16), nullable=False)
    form_number: Mapped[str | None] = mapped_column(String(32))
    statutory_reference: Mapped[str | None] = mapped_column(String(255))

    # --- Due date rule: due_day of the month `due_month_offset` months after period end ---
    due_day: Mapped[int] = mapped_column(Integer, nullable=False)
    due_month_offset: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Per-period overrides keyed by period suffix, e.g. {"Q4": {"month_offset": 2, "day": 31}}
    due_overrides: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    # Key into app.services.applicability.APPLICABILITY_RULES
    applicability_rule: Mapped[str] = mapped_column(String(64), default="always", nullable=False)

    default_fee_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # Days before the due date at which reminders fire.
    reminder_offsets_days: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    # Documents the client must supply for this filing.
    required_documents: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)

    is_system: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    items: Mapped[list[ComplianceItem]] = relationship(
        back_populates="compliance_type", cascade="all, delete-orphan"
    )


class ComplianceItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One filing obligation for one client for one period."""

    __tablename__ = "compliance_items"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "compliance_type_id", "period_label", name="uq_compliance_item_period"
        ),
        Index("ix_compliance_item_firm_due", "firm_id", "due_date"),
        Index("ix_compliance_item_firm_status", "firm_id", "status"),
        Index("ix_compliance_item_period", "firm_id", "period_label"),
    )

    firm_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("firms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    compliance_type_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("compliance_types.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigned_practitioner_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("practitioners.id", ondelete="SET NULL"), index=True
    )

    # e.g. "2026-07" (monthly), "FY2026-27-Q1" (quarterly), "FY2025-26" (annual)
    period_label: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    status: Mapped[ComplianceStatus] = mapped_column(
        EnumString(ComplianceStatus, 32), default=ComplianceStatus.PENDING, nullable=False
    )
    filed_on: Mapped[date | None] = mapped_column(Date)
    acknowledgement_number: Mapped[str | None] = mapped_column(String(128))
    fee_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    is_billed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    client: Mapped[Client] = relationship(back_populates="compliance_items")
    compliance_type: Mapped[ComplianceType] = relationship(back_populates="items")
    assigned_practitioner: Mapped[Practitioner | None] = relationship()

    # --- Derived display state -------------------------------------------------
    DUE_SOON_WINDOW_DAYS = 7

    def derive_display_status(self, today: date | None = None) -> str:
        """Colour-coded state used by the calendar UI."""
        today = today or date.today()
        if self.status in (ComplianceStatus.FILED, ComplianceStatus.DELAYED_FILED):
            return "filed"
        if self.status == ComplianceStatus.NOT_APPLICABLE:
            return "not_applicable"
        days_left = (self.due_date - today).days
        if days_left < 0:
            return "overdue"
        if days_left <= self.DUE_SOON_WINDOW_DAYS:
            return "due_soon"
        return "upcoming"

    def days_until_due(self, today: date | None = None) -> int:
        return (self.due_date - (today or date.today())).days
