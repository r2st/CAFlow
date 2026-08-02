"""Client CRUD, with automatic compliance-item generation."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentFirm, CurrentPractitioner, DbSession, Manager
from app.models.base import ComplianceStatus
from app.models.client import Client
from app.models.compliance import ComplianceItem
from app.models.firm import Practitioner
from app.schemas.client import (
    ClientComplianceSummary,
    ClientCreate,
    ClientCreateResponse,
    ClientDetailOut,
    ClientOut,
    ClientUpdate,
)
from app.schemas.common import Page
from app.schemas.compliance import ComplianceGenerateRequest, ComplianceGenerateResponse
from app.services import audit, firms
from app.services.compliance_generator import generate_compliance_items

router = APIRouter(prefix="/clients", tags=["clients"])


def _claim_client_slot(db: Session, firm) -> None:
    """Take a client slot, or answer 402.

    Called from creation *and* from reactivation. The plan caps how many
    clients a firm has active at once, and switching a deactivated one back on
    adds to that count exactly as creating one does — so leaving it out of this
    made the cap a formality: deactivate ten, create ten, switch the ten back
    on, and a fifty-client plan holds sixty.
    """
    try:
        firms.claim_client_slot(db, firm)
    except firms.PlanLimitReached as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
        ) from exc

# Flags that change which compliance types apply — a change means we re-generate.
REGISTRATION_FLAGS = (
    "entity_type",
    "gst_registered",
    "gst_filing_frequency",
    "tds_applicable",
    "income_tax_applicable",
    "tax_audit_applicable",
    "roc_applicable",
    "payroll_applicable",
)


def _get_client_or_404(db: Session, firm_id: uuid.UUID, client_id: uuid.UUID) -> Client:
    client = db.get(Client, client_id)
    if client is None or client.firm_id != firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return client


def _validate_assignee(db: Session, firm_id: uuid.UUID, practitioner_id: uuid.UUID | None):
    if practitioner_id is None:
        return
    assignee = db.get(Practitioner, practitioner_id)
    if assignee is None or assignee.firm_id != firm_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assigned practitioner does not belong to this firm",
        )


def _compliance_summary(db: Session, client_id: uuid.UUID) -> ClientComplianceSummary:
    today = date.today()
    rows = db.scalars(
        select(ComplianceItem).where(ComplianceItem.client_id == client_id)
    ).all()
    summary = ClientComplianceSummary(total=len(rows), pending=0, overdue=0, due_soon=0, filed=0)
    for item in rows:
        state = item.derive_display_status(today)
        if state == "filed":
            summary.filed += 1
        elif state == "overdue":
            summary.overdue += 1
            summary.pending += 1
        elif state == "due_soon":
            summary.due_soon += 1
            summary.pending += 1
        elif state == "upcoming":
            summary.pending += 1
    return summary


@router.post(
    "",
    response_model=ClientCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a client and generate its calendar",
)
def create_client(
    payload: ClientCreate, practitioner: Manager, firm: CurrentFirm, db: DbSession
):
    """Create a client and auto-generate its compliance items.

    Which items get created follows from the client's registrations: a
    GST-registered monthly filer gets GSTR-1 and GSTR-3B for every month,
    a TDS deductor gets quarterly returns, a company gets ROC filings, and so on.
    """
    _claim_client_slot(db, firm)

    if payload.pan:
        duplicate = db.scalar(
            select(Client).where(Client.firm_id == firm.id, Client.pan == payload.pan)
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A client with PAN {payload.pan} already exists",
            )

    _validate_assignee(db, firm.id, payload.assigned_practitioner_id)

    data = payload.model_dump(exclude={"generate_compliance_items", "onboarded_on"})
    client = Client(
        firm_id=firm.id,
        onboarded_on=payload.onboarded_on or date.today(),
        **data,
    )
    db.add(client)
    db.flush()

    created = 0
    if payload.generate_compliance_items:
        created = generate_compliance_items(db, client).created_count

    audit.record(
        db,
        action="client.create",
        entity_type="client",
        entity_id=client.id,
        actor=practitioner,
        summary=f"Created client {client.name} with {created} compliance item(s)",
    )
    db.commit()
    db.refresh(client)
    return ClientCreateResponse(
        client=ClientOut.model_validate(client), compliance_items_created=created
    )


@router.get("", response_model=Page[ClientOut], summary="List clients")
def list_clients(
    practitioner: CurrentPractitioner,
    db: DbSession,
    search: str | None = Query(default=None, max_length=255),
    is_active: bool | None = Query(default=None),
    gst_registered: bool | None = Query(default=None),
    assigned_to: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    filters = [Client.firm_id == practitioner.firm_id]
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                Client.name.ilike(pattern),
                Client.pan.ilike(pattern),
                Client.gstin.ilike(pattern),
                Client.email.ilike(pattern),
            )
        )
    if is_active is not None:
        filters.append(Client.is_active.is_(is_active))
    if gst_registered is not None:
        filters.append(Client.gst_registered.is_(gst_registered))
    if assigned_to is not None:
        filters.append(Client.assigned_practitioner_id == assigned_to)

    total = db.scalar(select(func.count(Client.id)).where(*filters)) or 0
    rows = db.scalars(
        select(Client).where(*filters).order_by(Client.name).limit(limit).offset(offset)
    ).all()
    return Page[ClientOut](
        items=[ClientOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{client_id}",
    response_model=ClientDetailOut,
    summary="A client with its filings and documents",
)
def get_client(client_id: uuid.UUID, practitioner: CurrentPractitioner, db: DbSession):
    client = _get_client_or_404(db, practitioner.firm_id, client_id)
    assignee = (
        db.get(Practitioner, client.assigned_practitioner_id)
        if client.assigned_practitioner_id
        else None
    )
    return ClientDetailOut(
        **ClientOut.model_validate(client).model_dump(),
        assigned_practitioner_name=assignee.full_name if assignee else None,
        compliance_summary=_compliance_summary(db, client.id),
    )


@router.patch("/{client_id}", response_model=ClientCreateResponse, summary="Update a client")
def update_client(
    client_id: uuid.UUID,
    payload: ClientUpdate,
    practitioner: Manager,
    firm: CurrentFirm,
    db: DbSession,
):
    """Update a client. Changing a registration flag tops up compliance items."""
    client = _get_client_or_404(db, practitioner.firm_id, client_id)
    updates = payload.model_dump(exclude_unset=True)

    # Before the flag is applied, so the count is of what the firm holds
    # without this one. Only on the transition: re-saving an already-active
    # client must not be charged a slot it is already occupying.
    if updates.get("is_active") is True and not client.is_active:
        _claim_client_slot(db, firm)

    if "assigned_practitioner_id" in updates:
        _validate_assignee(db, client.firm_id, updates["assigned_practitioner_id"])
    if updates.get("pan") and updates["pan"] != client.pan:
        duplicate = db.scalar(
            select(Client).where(
                Client.firm_id == client.firm_id,
                Client.pan == updates["pan"],
                Client.id != client.id,
            )
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A client with PAN {updates['pan']} already exists",
            )

    before = {key: getattr(client, key) for key in updates}
    for key, value in updates.items():
        setattr(client, key, value)
    db.flush()

    created = 0
    registrations_changed = any(
        key in updates and before[key] != updates[key] for key in REGISTRATION_FLAGS
    )
    if registrations_changed:
        created = generate_compliance_items(db, client).created_count

    audit.record(
        db,
        action="client.update",
        entity_type="client",
        entity_id=client.id,
        actor=practitioner,
        summary=f"Updated client {client.name}"
        + (f"; generated {created} new compliance item(s)" if created else ""),
        changes=audit.diff(before, updates),
    )
    db.commit()
    db.refresh(client)
    return ClientCreateResponse(
        client=ClientOut.model_validate(client), compliance_items_created=created
    )


@router.post(
    "/{client_id}/compliance-items",
    response_model=ComplianceGenerateResponse,
    summary="Top up a client's compliance calendar",
)
def generate_items(
    client_id: uuid.UUID,
    payload: ComplianceGenerateRequest,
    practitioner: Manager,
    db: DbSession,
):
    """Explicitly (re)generate compliance items for a client over a window."""
    client = _get_client_or_404(db, practitioner.firm_id, client_id)
    result = generate_compliance_items(
        db, client, window_start=payload.window_start, window_end=payload.window_end
    )
    audit.record(
        db,
        action="client.generate_compliance",
        entity_type="client",
        entity_id=client.id,
        actor=practitioner,
        summary=f"Generated {result.created_count} compliance item(s) for {client.name}",
    )
    db.commit()
    return ComplianceGenerateResponse(
        created=result.created_count,
        skipped_existing=result.skipped_existing,
        window_start=result.window_start,
        window_end=result.window_end,
    )


@router.delete(
    "/{client_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate a client",
)
def deactivate_client(client_id: uuid.UUID, practitioner: Manager, db: DbSession):
    """Soft-delete: clients are deactivated, never destroyed (audit trail)."""
    client = _get_client_or_404(db, practitioner.firm_id, client_id)
    client.is_active = False
    # Outstanding obligations for an off-boarded client are no longer tracked.
    db.query(ComplianceItem).filter(
        ComplianceItem.client_id == client.id,
        ComplianceItem.status.in_([ComplianceStatus.PENDING, ComplianceStatus.IN_PROGRESS]),
    ).update({ComplianceItem.status: ComplianceStatus.NOT_APPLICABLE})
    audit.record(
        db,
        action="client.deactivate",
        entity_type="client",
        entity_id=client.id,
        actor=practitioner,
        summary=f"Deactivated client {client.name}",
    )
    db.commit()
