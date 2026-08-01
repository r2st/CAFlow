"""Client model — the businesses/individuals a firm files for."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import (
    EntityType,
    EnumString,
    GSTFilingFrequency,
    JSONType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    UuidType,
)
from app.models.firm import Firm, Practitioner

if TYPE_CHECKING:
    from app.models.compliance import ComplianceItem


class Client(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "clients"
    __table_args__ = (
        UniqueConstraint("firm_id", "pan", name="uq_client_firm_pan"),
        Index("ix_client_firm_name", "firm_id", "name"),
        # Nearly every client query filters out deactivated clients.
        Index("ix_client_firm_active", "firm_id", "is_active"),
        # The chase list walks clients whose portal is on but who have not
        # signed in.
        Index("ix_client_firm_portal", "firm_id", "portal_enabled"),
    )

    firm_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("firms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigned_practitioner_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("practitioners.id", ondelete="SET NULL"), index=True
    )

    # --- Identity ---
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[EntityType] = mapped_column(
        EnumString(EntityType, 32), default=EntityType.INDIVIDUAL, nullable=False
    )
    pan: Mapped[str | None] = mapped_column(String(10), index=True)
    gstin: Mapped[str | None] = mapped_column(String(15), index=True)
    tan: Mapped[str | None] = mapped_column(String(10))
    cin: Mapped[str | None] = mapped_column(String(21))

    # --- Contact ---
    contact_person: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(20))
    whatsapp: Mapped[str | None] = mapped_column(String(20))
    address: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(100))

    # --- Registration flags: these drive compliance item generation ---
    gst_registered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    gst_filing_frequency: Mapped[GSTFilingFrequency] = mapped_column(
        EnumString(GSTFilingFrequency, 16), default=GSTFilingFrequency.MONTHLY, nullable=False
    )
    tds_applicable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    income_tax_applicable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    tax_audit_applicable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    roc_applicable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payroll_applicable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Engagement ---
    onboarded_on: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    financial_year_start_month: Mapped[int] = mapped_column(default=4, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    # Per-service fee overrides: {"<compliance_type_code>": <paise>}
    service_fees: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    # --- Client portal (passwordless magic-link access) ---
    portal_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Revocation cut-off: a magic link issued before this instant is refused.
    # Set (not cleared) when the firm revokes access, so links already emailed
    # die without needing a token blacklist.
    portal_token_valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    portal_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    firm: Mapped[Firm] = relationship(back_populates="clients")
    assigned_practitioner: Mapped[Practitioner | None] = relationship()
    compliance_items: Mapped[list[ComplianceItem]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )

    ENTITY_TYPES_WITH_ROC = frozenset(
        {EntityType.LLP, EntityType.PRIVATE_LIMITED, EntityType.PUBLIC_LIMITED}
    )

    @property
    def roc_required(self) -> bool:
        """ROC filing applies if explicitly flagged or implied by entity type."""
        return self.roc_applicable or self.entity_type in self.ENTITY_TYPES_WITH_ROC
