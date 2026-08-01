"""Append-only audit trail of every mutating action."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import JSONType, TimestampMixin, UUIDPrimaryKeyMixin, UuidType


class AuditLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_firm_created", "firm_id", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    firm_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("firms.id", ondelete="CASCADE"), index=True
    )
    actor_practitioner_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("practitioners.id", ondelete="SET NULL"), index=True
    )
    actor_label: Mapped[str | None] = mapped_column(String(255))

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UuidType)
    summary: Mapped[str | None] = mapped_column(Text)
    # {"before": {...}, "after": {...}} — only the changed fields.
    changes: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
