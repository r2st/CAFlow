"""Shared column types, mixins and enums for all CAFlow models."""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, Uuid

# JSONB on PostgreSQL, plain JSON on SQLite (test-suite).
JSONType = JSON().with_variant(JSONB(), "postgresql")

# sqlalchemy.Uuid renders as native UUID on PostgreSQL and CHAR(32) elsewhere.
UuidType = Uuid(as_uuid=True)


class EnumString(TypeDecorator):
    """Store an enum as VARCHAR but hand back the enum member on load.

    Plain ``String`` columns would round-trip as bare ``str``, which silently
    breaks ``.value`` access and ``isinstance`` checks throughout the app. A
    native PostgreSQL ENUM would be worse: every added status needs a migration.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class, length: int = 32, **kwargs):
        self.enum_class = enum_class
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return self.enum_class(value).value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return self.enum_class(value)


def utcnow() -> datetime:
    return datetime.now(UTC)


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(UuidType, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


# Members compare equal to, and serialise as, their string value.
StrEnum = enum.StrEnum


class PractitionerRole(StrEnum):
    OWNER = "owner"
    PARTNER = "partner"
    MANAGER = "manager"
    JUNIOR = "junior"


class FirmPlan(StrEnum):
    SOLO = "solo"
    PRACTICE = "practice"
    FIRM = "firm"


class EntityType(StrEnum):
    INDIVIDUAL = "individual"
    PROPRIETORSHIP = "proprietorship"
    PARTNERSHIP = "partnership"
    LLP = "llp"
    PRIVATE_LIMITED = "private_limited"
    PUBLIC_LIMITED = "public_limited"
    HUF = "huf"
    TRUST = "trust"
    AOP = "aop"
    SOCIETY = "society"


class ComplianceCategory(StrEnum):
    GST = "gst"
    TDS = "tds"
    INCOME_TAX = "income_tax"
    ROC = "roc"
    PAYROLL = "payroll"
    AUDIT = "audit"
    OTHER = "other"


class Frequency(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    ONE_TIME = "one_time"


class ComplianceStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    FILED = "filed"
    DELAYED_FILED = "delayed_filed"
    NOT_APPLICABLE = "not_applicable"


class GSTFilingFrequency(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"  # QRMP scheme


class DocumentCategory(StrEnum):
    BANK_STATEMENT = "bank_statement"
    PURCHASE_INVOICE = "purchase_invoice"
    SALES_INVOICE = "sales_invoice"
    FORM_16 = "form_16"
    FORM_26AS = "form_26as"
    AIS_TIS = "ais_tis"
    SALARY_REGISTER = "salary_register"
    GST_RETURN = "gst_return"
    TDS_CHALLAN = "tds_challan"
    BALANCE_SHEET = "balance_sheet"
    PROFIT_AND_LOSS = "profit_and_loss"
    PAN_CARD = "pan_card"
    AADHAAR = "aadhaar"
    INCORPORATION_DOC = "incorporation_doc"
    OTHER = "other"


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"


class ReminderType(StrEnum):
    DOCUMENT = "document"
    PAYMENT = "payment"
    FILING = "filing"
    CUSTOM = "custom"


class ReminderChannel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    IN_APP = "in_app"


class ReminderStatus(StrEnum):
    SCHEDULED = "scheduled"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"
