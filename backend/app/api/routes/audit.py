"""Reading the audit trail.

Every mutating endpoint writes to ``audit_log`` through ``services.audit``.
This module is the only way to read it back, and it is read-only by design:
a trail that can be edited is not a trail.

Restricted to owners and partners. The log records what every member of the
firm did, including their manager, so it is not a manager-level view.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select

from app.api.deps import DbSession, FirmAdmin
from app.core import clock
from app.core.search import LIKE_ESCAPE, contains_pattern
from app.models.audit import AuditLog
from app.schemas.audit import AuditActionOut, AuditActionsResponse, AuditLogOut
from app.schemas.common import Page

router = APIRouter(prefix="/audit", tags=["audit"])


def _day_bounds(
    from_date: date | None, to_date: date | None
) -> tuple[datetime | None, datetime | None]:
    """Turn inclusive calendar days into the UTC instants that bound them.

    ``to_date`` is inclusive to the user: asking for entries up to the 5th
    should include everything that happened on the 5th, not stop at midnight
    as a naive ``<= date`` comparison would.

    The days are Indian days. Entries are stored as UTC instants, which is
    right for an instant, but the person typing a date into this filter is
    picking a working day in India — and UTC is five and a half hours behind
    it. Bounding in UTC put the first five and a half hours of every Indian day
    under the previous date: a return lodged at 02:00 IST on the 6th, on the
    last night of the window and exactly when that work gets done, was
    unfindable on the 6th and turned up on the 5th. The audit trail is the
    firm's account of who did what and when, so the one filter over it must
    agree with the clock the rest of the practice runs on.
    """
    start = (
        datetime.combine(from_date, time.min, tzinfo=clock.IST).astimezone(UTC)
        if from_date
        else None
    )
    end = (
        datetime.combine(to_date, time.max, tzinfo=clock.IST).astimezone(UTC)
        if to_date
        else None
    )
    return start, end


@router.get("", response_model=Page[AuditLogOut], summary="Read the audit trail")
def list_audit_log(
    practitioner: FirmAdmin,
    db: DbSession,
    action: str | None = Query(default=None, max_length=64),
    entity_type: str | None = Query(default=None, max_length=64),
    entity_id: uuid.UUID | None = Query(default=None),
    actor_practitioner_id: uuid.UUID | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    search: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Newest first, scoped to the caller's firm.

    Passing ``entity_type`` and ``entity_id`` together gives the history of a
    single record — who touched this invoice, and what did they change.
    """
    if from_date and to_date and to_date < from_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to_date must not be before from_date",
        )

    filters = [AuditLog.firm_id == practitioner.firm_id]
    if action is not None:
        filters.append(AuditLog.action == action)
    if entity_type is not None:
        filters.append(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        filters.append(AuditLog.entity_id == entity_id)
    if actor_practitioner_id is not None:
        filters.append(AuditLog.actor_practitioner_id == actor_practitioner_id)

    start, end = _day_bounds(from_date, to_date)
    if start is not None:
        filters.append(AuditLog.created_at >= start)
    if end is not None:
        filters.append(AuditLog.created_at <= end)

    # Escaped, because ``%`` and ``_`` in the caller's text are pattern syntax
    # rather than text — see :mod:`app.core.search`. A summary quotes invoice
    # numbers and email addresses, which is where an underscore lives, and a
    # trail that silently answers a narrower question than it was asked is one
    # nobody can rely on.
    if (pattern := contains_pattern(search)) is not None:
        filters.append(
            or_(
                AuditLog.summary.ilike(pattern, escape=LIKE_ESCAPE),
                AuditLog.actor_label.ilike(pattern, escape=LIKE_ESCAPE),
            )
        )

    total = db.scalar(select(func.count(AuditLog.id)).where(*filters)) or 0
    rows = db.scalars(
        select(AuditLog)
        .where(*filters)
        # id breaks ties: entries written inside one request share a timestamp
        # on backends that store it at second resolution, and a paged list
        # whose order is not total can repeat or drop rows between pages.
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return Page[AuditLogOut](
        items=[AuditLogOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/actions",
    response_model=AuditActionsResponse,
    summary="The actions and entity types this firm has recorded",
)
def audit_actions(practitioner: FirmAdmin, db: DbSession):
    """Drives the filter lists, so they only offer filters that match something."""
    action_rows = db.execute(
        select(AuditLog.action, func.count(AuditLog.id))
        .where(AuditLog.firm_id == practitioner.firm_id)
        .group_by(AuditLog.action)
        .order_by(func.count(AuditLog.id).desc(), AuditLog.action)
    ).all()

    entity_rows = db.scalars(
        select(AuditLog.entity_type)
        .where(AuditLog.firm_id == practitioner.firm_id)
        .group_by(AuditLog.entity_type)
        .order_by(AuditLog.entity_type)
    ).all()

    return AuditActionsResponse(
        actions=[AuditActionOut(action=action, count=count) for action, count in action_rows],
        entity_types=list(entity_rows),
    )
