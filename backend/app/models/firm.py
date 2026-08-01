"""Firm (tenant root) and practitioner (user) models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import (
    EnumString,
    FirmPlan,
    JSONType,
    PractitionerRole,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    UuidType,
)

if TYPE_CHECKING:
    from app.models.client import Client


class Firm(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A CA firm — the tenant boundary for every other record."""

    __tablename__ = "firms"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    icai_registration_number: Mapped[str | None] = mapped_column(String(64))
    pan: Mapped[str | None] = mapped_column(String(10))
    gstin: Mapped[str | None] = mapped_column(String(15))
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    address_line1: Mapped[str | None] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    pincode: Mapped[str | None] = mapped_column(String(10))

    plan: Mapped[FirmPlan] = mapped_column(
        EnumString(FirmPlan, 32), default=FirmPlan.SOLO, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    settings: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    practitioners: Mapped[list[Practitioner]] = relationship(
        back_populates="firm", cascade="all, delete-orphan"
    )
    clients: Mapped[list[Client]] = relationship(
        back_populates="firm", cascade="all, delete-orphan"
    )

    @property
    def client_limit(self) -> int | None:
        return {FirmPlan.SOLO: 50, FirmPlan.PRACTICE: 200, FirmPlan.FIRM: None}[self.plan]

    @property
    def user_limit(self) -> int | None:
        return {FirmPlan.SOLO: 1, FirmPlan.PRACTICE: 5, FirmPlan.FIRM: None}[self.plan]


class Practitioner(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A CA or staff member inside a firm. This is the login identity."""

    __tablename__ = "practitioners"
    __table_args__ = (UniqueConstraint("firm_id", "email", name="uq_practitioner_firm_email"),)

    firm_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("firms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(20))
    membership_number: Mapped[str | None] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[PractitionerRole] = mapped_column(
        EnumString(PractitionerRole, 32), default=PractitionerRole.JUNIOR, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    firm: Mapped[Firm] = relationship(back_populates="practitioners")

    @property
    def can_manage_firm(self) -> bool:
        return self.role in (PractitionerRole.OWNER, PractitionerRole.PARTNER)
