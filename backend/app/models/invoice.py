"""Invoices and invoice lines. All money is stored in paise (integer)."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import BigInteger, Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import (
    EnumString,
    InvoiceStatus,
    JSONType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    UuidType,
)
from app.models.client import Client


class Invoice(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("firm_id", "invoice_number", name="uq_invoice_firm_number"),
        Index("ix_invoice_firm_status", "firm_id", "status"),
        # Ageing and the overdue sweep both walk a firm's invoices by due date.
        Index("ix_invoice_firm_due", "firm_id", "due_date"),
        # A client's outstanding balance.
        Index("ix_invoice_client_status", "client_id", "status"),
    )

    firm_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("firms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )

    invoice_number: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)

    subtotal_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tax_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    amount_paid_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    gst_rate_bps: Mapped[int] = mapped_column(Integer, default=1800, nullable=False)  # 18.00%

    status: Mapped[InvoiceStatus] = mapped_column(
        EnumString(InvoiceStatus, 32), default=InvoiceStatus.DRAFT, nullable=False
    )
    payment_date: Mapped[date | None] = mapped_column(Date)
    payment_reference: Mapped[str | None] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    client: Mapped[Client] = relationship()
    # Ordered, because an invoice is a document of record and the order of its
    # lines is part of what the client is holding a copy of. Nothing said what
    # that order was: the relationship emitted a bare ``SELECT`` and took
    # whatever came back, which on PostgreSQL is unspecified and free to differ
    # between two reads of the same invoice.
    #
    # The order is not incidental either — it is the one thing about the lines
    # a practitioner arranges by hand. ``set_lines`` writes them in exactly the
    # order the editor submitted, and every reader then threw that away: the
    # printed invoice, the client's copy in the portal, and the editor itself,
    # which reloads a draft's lines into rows it keys by position.
    #
    # ``id`` closes the order for rows that predate ``position`` and share its
    # backfilled default; see migration 0007.
    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceLine.position, InvoiceLine.id",
    )

    @property
    def balance_paise(self) -> int:
        return self.total_paise - self.amount_paid_paise


class InvoiceLine(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "invoice_lines"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    compliance_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType, ForeignKey("compliance_items.id", ondelete="SET NULL")
    )

    # Where this line sits on the invoice, counting from zero. Written from the
    # order the caller submitted; see ``Invoice.lines``.
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_price_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sac_code: Mapped[str | None] = mapped_column(String(16))

    invoice: Mapped[Invoice] = relationship(back_populates="lines")
