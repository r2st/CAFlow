"""Document intake: upload, AI categorisation, checklists and downloads."""

from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentPractitioner, DbSession
from app.models.base import DocumentCategory
from app.models.client import Client
from app.models.compliance import ComplianceItem
from app.models.document import Document
from app.schemas.common import Page
from app.schemas.document import (
    ChecklistOut,
    DocumentOut,
    DocumentUpdate,
    DocumentUploadResponse,
    OutstandingDocumentsResponse,
    RequirementStateOut,
)
from app.services import audit, storage
from app.services import documents as document_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

# How far ahead the "what are we still waiting for" view looks by default.
OUTSTANDING_HORIZON_DAYS = 45


def _get_client_or_404(db: Session, firm_id: uuid.UUID, client_id: uuid.UUID) -> Client:
    client = db.get(Client, client_id)
    if client is None or client.firm_id != firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return client


def _get_document_or_404(db: Session, firm_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    document = db.get(Document, document_id)
    if document is None or document.firm_id != firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


def _get_item_or_404(db: Session, firm_id: uuid.UUID, item_id: uuid.UUID) -> ComplianceItem:
    item = db.get(ComplianceItem, item_id)
    if item is None or item.firm_id != firm_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Compliance item not found"
        )
    return item


def clean_requirement_or_422(value: str | None) -> str | None:
    """The service's requirement check, reported as a 422 on the form field.

    Shared with the portal upload, which takes the same field from a party
    outside the firm entirely.
    """
    try:
        return document_service.clean_requirement(value)
    except document_service.InvalidRequirement as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


def compliance_label(item: ComplianceItem | None) -> str | None:
    if item is None:
        return None
    return f"{item.compliance_type.name} — {item.period_label}"


def serialise_document(document: Document) -> DocumentOut:
    out = DocumentOut.model_validate(document)
    out.client_name = document.client.name if document.client else None
    out.compliance_label = compliance_label(document.compliance_item)
    return out


def serialise_checklist(
    item: ComplianceItem, checklist: document_service.Checklist
) -> ChecklistOut:
    return ChecklistOut(
        compliance_item_id=item.id,
        compliance_type_name=item.compliance_type.name if item.compliance_type else None,
        period_label=item.period_label,
        due_date=item.due_date.isoformat(),
        client_id=item.client_id,
        client_name=item.client.name if item.client else None,
        requirements=[
            RequirementStateOut(
                requirement=state.requirement,
                label=state.label,
                satisfied=state.satisfied,
                document_ids=state.document_ids,
            )
            for state in checklist.requirements
        ],
        missing=checklist.missing,
        is_complete=checklist.is_complete,
    )


# --------------------------------------------------------------- outstanding --
# Declared before /{document_id} so the literal path is not swallowed by it.


@router.get(
    "/outstanding",
    response_model=OutstandingDocumentsResponse,
    summary="Filings still waiting on documents",
)
def outstanding_documents(
    practitioner: CurrentPractitioner,
    db: DbSession,
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    client_id: uuid.UUID | None = Query(default=None),
):
    """Open filings still missing documents — the chase list."""
    start = from_date or date.today()
    end = to_date or start + timedelta(days=OUTSTANDING_HORIZON_DAYS)
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to_date must not be before from_date",
        )

    pairs = document_service.items_awaiting_documents(
        db, practitioner.firm_id, from_date=start, to_date=end, client_id=client_id
    )
    checklists = [serialise_checklist(item, checklist) for item, checklist in pairs]
    return OutstandingDocumentsResponse(
        from_date=start.isoformat(),
        to_date=end.isoformat(),
        total_items=len(checklists),
        total_missing=sum(len(c.missing) for c in checklists),
        checklists=checklists,
    )


@router.get(
    "/checklist/{compliance_item_id}",
    response_model=ChecklistOut,
    summary="The document checklist for one filing",
)
def item_checklist(
    compliance_item_id: uuid.UUID, practitioner: CurrentPractitioner, db: DbSession
):
    item = _get_item_or_404(db, practitioner.firm_id, compliance_item_id)
    return serialise_checklist(item, document_service.checklist_for_item(db, item))


# -------------------------------------------------------------------- upload --


@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document for a client",
)
def upload_document(
    practitioner: CurrentPractitioner,
    db: DbSession,
    file: UploadFile = File(...),
    client_id: uuid.UUID = Form(...),
    compliance_item_id: uuid.UUID | None = Form(default=None),
    requirement: str | None = Form(default=None),
    category: DocumentCategory | None = Form(default=None),
    share_with_client: bool = Form(default=False),
):
    """Upload a document on the client's behalf and categorise it."""
    requirement = clean_requirement_or_422(requirement)
    client = _get_client_or_404(db, practitioner.firm_id, client_id)
    item = (
        _get_item_or_404(db, practitioner.firm_id, compliance_item_id)
        if compliance_item_id
        else None
    )
    if item is not None and item.client_id != client.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That filing belongs to a different client",
        )

    data = file.file.read()
    try:
        document = document_service.ingest_upload(
            db,
            client=client,
            filename=file.filename or "upload",
            data=data,
            content_type=file.content_type,
            compliance_item_id=compliance_item_id,
            requirement=requirement,
            category=category,
            uploaded_by_practitioner_id=practitioner.id,
            share_with_client=share_with_client,
        )
    except storage.UploadTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except storage.UnsupportedFileType as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc

    audit.record(
        db,
        action="document.upload",
        entity_type="document",
        entity_id=document.id,
        actor=practitioner,
        summary=f"Uploaded {document.original_filename} for {client.name}",
    )
    db.commit()
    db.refresh(document)

    checklist = (
        serialise_checklist(item, document_service.checklist_for_item(db, item))
        if item is not None
        else None
    )
    return DocumentUploadResponse(
        document=serialise_document(document), checklist=checklist
    )


# ---------------------------------------------------------------------- CRUD --


@router.get("", response_model=Page[DocumentOut], summary="List documents")
def list_documents(
    practitioner: CurrentPractitioner,
    db: DbSession,
    client_id: uuid.UUID | None = Query(default=None),
    compliance_item_id: uuid.UUID | None = Query(default=None),
    category: DocumentCategory | None = Query(default=None),
    uploaded_via_portal: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    filters = [Document.firm_id == practitioner.firm_id]
    if client_id is not None:
        filters.append(Document.client_id == client_id)
    if compliance_item_id is not None:
        filters.append(Document.compliance_item_id == compliance_item_id)
    if category is not None:
        filters.append(Document.category == category)
    if uploaded_via_portal is not None:
        filters.append(Document.uploaded_via_portal.is_(uploaded_via_portal))

    total = db.scalar(select(func.count(Document.id)).where(*filters)) or 0
    rows = db.scalars(
        select(Document)
        .options(
            selectinload(Document.client),
            selectinload(Document.compliance_item).selectinload(ComplianceItem.compliance_type),
        )
        .where(*filters)
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return Page[DocumentOut](
        items=[serialise_document(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{document_id}", response_model=DocumentOut, summary="A single document")
def get_document(document_id: uuid.UUID, practitioner: CurrentPractitioner, db: DbSession):
    return serialise_document(_get_document_or_404(db, practitioner.firm_id, document_id))


@router.get("/{document_id}/download", summary="Download a document")
def download_document(
    document_id: uuid.UUID, practitioner: CurrentPractitioner, db: DbSession
):
    document = _get_document_or_404(db, practitioner.firm_id, document_id)
    try:
        payload = storage.open_stored(document.storage_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="The stored file is no longer available",
        ) from exc
    return Response(
        content=payload,
        media_type=document.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{storage.safe_filename(document.original_filename)}"'
            )
        },
    )


@router.patch(
    "/{document_id}",
    response_model=DocumentOut,
    summary="Re-categorise, re-link or share a document",
)
def update_document(
    document_id: uuid.UUID,
    payload: DocumentUpdate,
    practitioner: CurrentPractitioner,
    db: DbSession,
):
    """Correct a category, re-link a document to a filing, or share it."""
    document = _get_document_or_404(db, practitioner.firm_id, document_id)
    updates = payload.model_dump(exclude_unset=True)

    if updates.get("satisfies_requirements") is not None:
        # Same column, same reasoning as the upload form — this is the other
        # way a caller writes into it.
        keys = updates["satisfies_requirements"]
        if len(keys) > document_service.MAX_SATISFIED_REQUIREMENTS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "A document can answer at most "
                    f"{document_service.MAX_SATISFIED_REQUIREMENTS} requirements"
                ),
            )
        # Blanks drop out rather than being stored as keys nothing can match.
        cleaned = [clean_requirement_or_422(key) for key in keys]
        updates["satisfies_requirements"] = sorted({key for key in cleaned if key})

    if updates.get("compliance_item_id") is not None:
        item = _get_item_or_404(db, practitioner.firm_id, updates["compliance_item_id"])
        if item.client_id != document.client_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That filing belongs to a different client",
            )

    before = {key: getattr(document, key) for key in updates}
    for key, value in updates.items():
        setattr(document, key, value)

    # A human choosing the category makes the AI's confidence meaningless.
    if "category" in updates:
        document.is_category_confirmed = updates.get("is_category_confirmed", True)
        document.category_confidence = None

    audit.record(
        db,
        action="document.update",
        entity_type="document",
        entity_id=document.id,
        actor=practitioner,
        summary=f"Updated {document.original_filename}",
        changes=audit.diff(before, updates),
    )
    db.commit()
    db.refresh(document)
    return serialise_document(document)


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document",
)
def delete_document(
    document_id: uuid.UUID, practitioner: CurrentPractitioner, db: DbSession
):
    """Delete a document, record first and bytes second.

    Only one of the two stores can be rolled back. Unlinking before the commit
    meant a commit that failed — a dropped connection, a statement timeout, a
    deadlock — left the row describing a file that was already gone: still
    listed, still offered to the client in the portal, downloading 410 for
    ever, and no way to get the bytes back. Committing first inverts the
    failure into an orphaned file, which costs disk and can be swept.
    """
    document = _get_document_or_404(db, practitioner.firm_id, document_id)
    filename = document.original_filename
    storage_path = document.storage_path
    db.delete(document)
    audit.record(
        db,
        action="document.delete",
        entity_type="document",
        entity_id=document_id,
        actor=practitioner,
        summary=f"Deleted {filename}",
    )
    db.commit()

    # Logged, not raised: the record is gone, which is what was asked for, and
    # the caller has nothing left to retry — a second DELETE would only 404.
    try:
        storage.delete_stored(storage_path)
    except OSError:
        logger.warning(
            "Deleted document %s but could not remove %s", document_id, storage_path,
            exc_info=True,
        )
