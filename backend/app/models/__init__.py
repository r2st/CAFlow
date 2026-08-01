"""Import every model so Alembic's autogenerate and Base.metadata see them all."""

from app.models.audit import AuditLog
from app.models.base import (
    ComplianceCategory,
    ComplianceStatus,
    DocumentCategory,
    DocumentStatus,
    EntityType,
    FirmPlan,
    Frequency,
    GSTFilingFrequency,
    InvoiceStatus,
    PractitionerRole,
    ReminderChannel,
    ReminderStatus,
    ReminderType,
    TaskPriority,
    TaskStatus,
)
from app.models.client import Client
from app.models.compliance import ComplianceItem, ComplianceType
from app.models.document import Document
from app.models.firm import Firm, Practitioner
from app.models.invoice import Invoice, InvoiceLine
from app.models.reminder import Reminder
from app.models.task import Task

__all__ = [
    "AuditLog",
    "Client",
    "ComplianceCategory",
    "ComplianceItem",
    "ComplianceStatus",
    "ComplianceType",
    "Document",
    "DocumentCategory",
    "DocumentStatus",
    "EntityType",
    "Firm",
    "FirmPlan",
    "Frequency",
    "GSTFilingFrequency",
    "Invoice",
    "InvoiceLine",
    "InvoiceStatus",
    "Practitioner",
    "PractitionerRole",
    "Reminder",
    "ReminderChannel",
    "ReminderStatus",
    "ReminderType",
    "Task",
    "TaskPriority",
    "TaskStatus",
]
