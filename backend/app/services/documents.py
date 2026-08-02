"""Document checklists: what a filing needs, and what is still outstanding.

Every ``ComplianceType`` declares ``required_documents`` — labels such as
``"bank_statement"`` or ``"export_invoices"``. A requirement counts as met when
the client has attached a document to that compliance item whose category
matches the label, or which was explicitly uploaded against it (see
``Document.satisfies_requirements``); most labels are ``DocumentCategory``
values, a few are not.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.base import ComplianceStatus, DocumentCategory, DocumentStatus
from app.models.client import Client
from app.models.compliance import ComplianceItem
from app.models.document import Document
from app.schemas.common import sanitize_text
from app.services import storage
from app.services.ai import categorise_document, regex_extract

OPEN_STATUSES = (ComplianceStatus.PENDING, ComplianceStatus.IN_PROGRESS)

# Requirement keys are snake_case, but these labels are read by the client —
# in the portal checklist and in the document-request emails. Naively
# capitalising a key turns every statutory acronym into "Tds challan" or "Gst
# return", so the ones a CA's client would recognise are spelled out here.
REQUIREMENT_LABELS = {
    "ais_tis": "AIS / TIS",
    "credit_debit_notes": "Credit / debit notes",
    "export_invoices": "Export invoices",
    "form_16": "Form 16",
    "form_26as": "Form 26AS",
    "gst_return": "GST return",
    "incorporation_doc": "Incorporation document",
    "pan_card": "PAN card",
    "profit_and_loss": "Profit & loss",
    "tds_challan": "TDS challan",
}


def requirement_label(requirement: str) -> str:
    """A label to show a client. Unknown keys degrade to readable title case."""
    if requirement in REQUIREMENT_LABELS:
        return REQUIREMENT_LABELS[requirement]
    return requirement.replace("_", " ").capitalize()


def is_category_requirement(requirement: str) -> bool:
    return requirement in {category.value for category in DocumentCategory}


# A requirement key is this system's own vocabulary — the snake_case labels a
# ``ComplianceType`` declares — not free text. It arrives as a multipart form
# field, which is the one inbound string that never passes through a Pydantic
# model, so nothing else caps its length, strips its control characters or
# folds its case.
#
# It reaches ``Document.satisfies_requirements``, a JSON column, and the portal
# upload that writes it is reachable by anyone holding a client's magic link.
# Unbounded, that is a megabyte of attacker-chosen text per upload sitting in a
# column the checklist reads on every page load; unfolded, "Bank_Statement"
# silently satisfies nothing while looking to the uploader like it did.
REQUIREMENT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_REQUIREMENT_KEY_LENGTH = 64
# One document answering more than this many checklist rows is not a document,
# it is someone filling the column.
MAX_SATISFIED_REQUIREMENTS = 20


class InvalidRequirement(ValueError):
    """A requirement key that is not shaped like one."""


def clean_requirement(value: str | None) -> str | None:
    """Normalise a caller-supplied requirement key, refusing free text.

    Blank (and whitespace-only) is ``None`` rather than an error: a form that
    submits an empty field means "no requirement", which is the same thing as
    omitting it.
    """
    if value is None:
        return None
    candidate = sanitize_text(value).lower()
    if not candidate:
        return None
    if len(candidate) > MAX_REQUIREMENT_KEY_LENGTH:
        raise InvalidRequirement(
            f"A requirement key is at most {MAX_REQUIREMENT_KEY_LENGTH} characters"
        )
    if not REQUIREMENT_KEY_RE.match(candidate):
        raise InvalidRequirement(
            f"{value!r} is not a requirement key — expected lower-case letters, "
            "digits and underscores, as in 'bank_statement'"
        )
    return candidate


@dataclass
class RequirementState:
    requirement: str
    label: str
    satisfied: bool
    document_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass
class Checklist:
    compliance_item_id: uuid.UUID
    requirements: list[RequirementState]

    @property
    def missing(self) -> list[str]:
        return [r.requirement for r in self.requirements if not r.satisfied]

    @property
    def satisfied_count(self) -> int:
        return sum(1 for r in self.requirements if r.satisfied)

    @property
    def is_complete(self) -> bool:
        return not self.missing


def satisfies(document: Document, requirement: str) -> bool:
    """Does this upload answer ``requirement``?"""
    if document.category is not None and document.category.value == requirement:
        return True
    return requirement in (document.satisfies_requirements or [])


def build_checklist(item: ComplianceItem, documents: list[Document]) -> Checklist:
    """Pure checklist computation — pass the documents already attached."""
    required = list(item.compliance_type.required_documents or [])
    states = []
    for requirement in required:
        matches = [doc.id for doc in documents if satisfies(doc, requirement)]
        states.append(
            RequirementState(
                requirement=requirement,
                label=requirement_label(requirement),
                satisfied=bool(matches),
                document_ids=matches,
            )
        )
    return Checklist(compliance_item_id=item.id, requirements=states)


def checklist_for_item(db: Session, item: ComplianceItem) -> Checklist:
    documents = list(
        db.scalars(select(Document).where(Document.compliance_item_id == item.id)).all()
    )
    return build_checklist(item, documents)


def checklists_for_items(db: Session, items: list[ComplianceItem]) -> dict[uuid.UUID, Checklist]:
    """Checklists for many items in two queries rather than one per item."""
    if not items:
        return {}
    item_ids = [item.id for item in items]
    documents = list(
        db.scalars(select(Document).where(Document.compliance_item_id.in_(item_ids))).all()
    )
    by_item: dict[uuid.UUID, list[Document]] = {item_id: [] for item_id in item_ids}
    for document in documents:
        by_item[document.compliance_item_id].append(document)
    return {item.id: build_checklist(item, by_item[item.id]) for item in items}


def ingest_upload(
    db: Session,
    *,
    client: Client,
    filename: str,
    data: bytes,
    content_type: str | None = None,
    compliance_item_id: uuid.UUID | None = None,
    requirement: str | None = None,
    category: DocumentCategory | None = None,
    uploaded_by_practitioner_id: uuid.UUID | None = None,
    via_portal: bool = False,
    share_with_client: bool = False,
) -> Document:
    """Store a file, categorise it, and record it against the client.

    Categorisation runs inline because it is cheap and degrades to a filename
    heuristic when no API key is configured — a client uploading through the
    portal should see the category immediately, not after a worker round-trip.
    An explicit ``category`` from the uploader always wins.
    """
    stored = storage.save_upload(
        firm_id=client.firm_id,
        client_id=client.id,
        filename=filename,
        data=data,
        content_type=content_type,
    )
    # Record what the bytes actually are, not what the upload claimed. A
    # browser that sends application/octet-stream for a PDF would otherwise
    # keep that PDF out of the text extractor for good.
    content_type = storage.effective_content_type(content_type, data, filename)

    document = Document(
        firm_id=client.firm_id,
        client_id=client.id,
        compliance_item_id=compliance_item_id,
        uploaded_by_practitioner_id=uploaded_by_practitioner_id,
        original_filename=filename or stored.safe_filename,
        storage_path=stored.storage_path,
        content_type=content_type,
        size_bytes=stored.size_bytes,
        checksum_sha256=stored.checksum_sha256,
        uploaded_via_portal=via_portal,
        is_shared_with_client=share_with_client,
        satisfies_requirements=(
            [requirement] if requirement and not is_category_requirement(requirement) else []
        ),
    )

    if category is not None:
        document.category = category
        document.category_confidence = None
        document.is_category_confirmed = True
    elif requirement and is_category_requirement(requirement):
        # The uploader answered a specific checklist row, which is a stronger
        # signal than anything a classifier could infer from the file.
        document.category = DocumentCategory(requirement)
        document.category_confidence = None
        document.is_category_confirmed = True
    else:
        excerpt = storage.text_excerpt(data, content_type, filename)
        result = categorise_document(filename, excerpt)
        document.category = result.category
        document.category_confidence = result.confidence
        document.extracted_data = result.extracted

    if not document.extracted_data:
        # PAN/GSTIN/TAN are worth pulling out even when the category was given.
        excerpt = storage.text_excerpt(data, content_type, filename)
        if excerpt:
            document.extracted_data = regex_extract(excerpt)

    document.status = DocumentStatus.PROCESSED
    document.processed_at = datetime.now(UTC)

    db.add(document)
    db.flush()
    return document


def items_awaiting_documents(
    db: Session,
    firm_id: uuid.UUID,
    *,
    from_date: date,
    to_date: date,
    client_id: uuid.UUID | None = None,
) -> list[tuple[ComplianceItem, Checklist]]:
    """Open filings in the window that are still missing at least one document.

    This is what drives both the "chase the client" screen and the automated
    document-collection reminders.
    """
    filters = [
        ComplianceItem.firm_id == firm_id,
        ComplianceItem.status.in_(OPEN_STATUSES),
        ComplianceItem.due_date >= from_date,
        ComplianceItem.due_date <= to_date,
    ]
    if client_id is not None:
        filters.append(ComplianceItem.client_id == client_id)

    items = list(
        db.scalars(
            select(ComplianceItem)
            .options(
                selectinload(ComplianceItem.client),
                selectinload(ComplianceItem.compliance_type),
            )
            .where(*filters)
            .order_by(ComplianceItem.due_date)
        ).all()
    )
    checklists = checklists_for_items(db, items)
    return [
        (item, checklists[item.id])
        for item in items
        if item.compliance_type.required_documents and not checklists[item.id].is_complete
    ]
