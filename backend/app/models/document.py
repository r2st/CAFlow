"""Uploaded client documents, with AI categorisation / extraction results."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import (
    DocumentCategory,
    DocumentStatus,
    EnumString,
    JSONType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    UuidType,
)
from app.models.client import Client
from app.models.compliance import ComplianceItem


class Document(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "documents"

    firm_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("firms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    compliance_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("compliance_items.id", ondelete="SET NULL"), index=True
    )
    uploaded_by_practitioner_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("practitioners.id", ondelete="SET NULL")
    )

    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), index=True)

    category: Mapped[DocumentCategory] = mapped_column(
        EnumString(DocumentCategory, 64), default=DocumentCategory.OTHER, nullable=False, index=True
    )
    # Confidence of the AI categorisation, 0..1. NULL when set manually.
    category_confidence: Mapped[float | None] = mapped_column(Float)
    is_category_confirmed: Mapped[bool] = mapped_column(default=False, nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        EnumString(DocumentStatus, 32), default=DocumentStatus.UPLOADED, nullable=False
    )

    # AI-extracted fields: {"pan": "...", "gstin": "...", "total_amount_paise": 12345, ...}
    extracted_data: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    processing_error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Requirement labels from ``ComplianceType.required_documents`` that this
    # upload answers. Most requirements are satisfied by a category match; this
    # covers the ones that have no category of their own ("export_invoices").
    satisfies_requirements: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    # True when the client uploaded it through the portal rather than the firm.
    uploaded_via_portal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Firm → client sharing: reports and computation sheets the client may download.
    is_shared_with_client: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    client: Mapped[Client] = relationship()
    compliance_item: Mapped[ComplianceItem | None] = relationship()
