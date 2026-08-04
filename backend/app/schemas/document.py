"""Document upload, categorisation and checklist schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from app.models.base import DocumentCategory, DocumentStatus
from app.schemas.common import ORMModel, SanitizedModel, not_clearable


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


class DocumentUpdate(SanitizedModel):
    category: DocumentCategory | None = None
    compliance_item_id: uuid.UUID | None = None
    satisfies_requirements: list[str] | None = None
    is_shared_with_client: bool | None = None
    # Setting the category by hand confirms it and clears the AI confidence.
    is_category_confirmed: bool | None = None

    # ``compliance_item_id`` is the one field here a caller may null out — that
    # is how a document is unlinked from a filing. The rest back ``NOT NULL``
    # columns; an uncategorised document is ``other``, not nothing.
    _no_nulls = not_clearable(
        "category",
        "satisfies_requirements",
        "is_shared_with_client",
        "is_category_confirmed",
    )


class RequirementStateOut(SanitizedModel):
    requirement: str
    label: str
    satisfied: bool
    document_ids: list[uuid.UUID]


class ChecklistOut(SanitizedModel):
    compliance_item_id: uuid.UUID
    compliance_type_name: str | None = None
    period_label: str | None = None
    due_date: str | None = None
    client_id: uuid.UUID | None = None
    client_name: str | None = None
    requirements: list[RequirementStateOut]
    missing: list[str]
    is_complete: bool


class OutstandingDocumentsResponse(SanitizedModel):
    """Everything the practice is still waiting on, deadline-first."""

    from_date: str
    to_date: str
    total_items: int
    total_missing: int
    checklists: list[ChecklistOut]


class DocumentUploadResponse(SanitizedModel):
    document: DocumentOut
    checklist: ChecklistOut | None = None


class SharedDocumentOut(SanitizedModel):
    """Trimmed view for the client portal — no firm-internal fields."""

    id: uuid.UUID
    original_filename: str
    category: DocumentCategory
    size_bytes: int
    uploaded_via_portal: bool
    created_at: datetime
    compliance_label: str | None = None


# There is deliberately no model for the upload form. Multipart fields are
# declared on the route as ``Form(...)`` parameters and never pass through a
# schema, so a model here would look like validation while enforcing nothing —
# which is exactly what the unused ``max_length=64`` that used to live here
# did. ``services.documents.clean_requirement`` is where that check now runs.
