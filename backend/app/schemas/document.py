"""Document upload, categorisation and checklist schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.base import DocumentCategory, DocumentStatus
from app.schemas.common import ORMModel


class DocumentOut(ORMModel):
    id: uuid.UUID
    firm_id: uuid.UUID
    client_id: uuid.UUID
    compliance_item_id: uuid.UUID | None
    original_filename: str
    content_type: str | None
    size_bytes: int
    checksum_sha256: str | None
    category: DocumentCategory
    category_confidence: float | None
    is_category_confirmed: bool
    status: DocumentStatus
    extracted_data: dict
    satisfies_requirements: list
    uploaded_via_portal: bool
    is_shared_with_client: bool
    processing_error: str | None
    processed_at: datetime | None
    created_at: datetime

    # Denormalised for list views.
    client_name: str | None = None
    compliance_label: str | None = None


class DocumentUpdate(BaseModel):
    category: DocumentCategory | None = None
    compliance_item_id: uuid.UUID | None = None
    satisfies_requirements: list[str] | None = None
    is_shared_with_client: bool | None = None
    # Setting the category by hand confirms it and clears the AI confidence.
    is_category_confirmed: bool | None = None


class RequirementStateOut(BaseModel):
    requirement: str
    label: str
    satisfied: bool
    document_ids: list[uuid.UUID]


class ChecklistOut(BaseModel):
    compliance_item_id: uuid.UUID
    compliance_type_name: str | None = None
    period_label: str | None = None
    due_date: str | None = None
    client_id: uuid.UUID | None = None
    client_name: str | None = None
    requirements: list[RequirementStateOut]
    missing: list[str]
    is_complete: bool


class OutstandingDocumentsResponse(BaseModel):
    """Everything the practice is still waiting on, deadline-first."""

    from_date: str
    to_date: str
    total_items: int
    total_missing: int
    checklists: list[ChecklistOut]


class DocumentUploadResponse(BaseModel):
    document: DocumentOut
    checklist: ChecklistOut | None = None


class SharedDocumentOut(BaseModel):
    """Trimmed view for the client portal — no firm-internal fields."""

    id: uuid.UUID
    original_filename: str
    category: DocumentCategory
    size_bytes: int
    uploaded_via_portal: bool
    created_at: datetime
    compliance_label: str | None = None


class PractitionerUploadRequest(BaseModel):
    """Multipart form fields accepted alongside the file itself."""

    compliance_item_id: uuid.UUID | None = None
    requirement: str | None = Field(default=None, max_length=64)
    category: DocumentCategory | None = None
    share_with_client: bool = False
