"""Compliance calendar: listing by period, status filtering and filing updates."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentPractitioner, DbSession
from app.core import clock
from app.core.periods import add_months
from app.models.base import ComplianceCategory, ComplianceStatus
from app.models.client import Client
from app.models.compliance import ComplianceItem, ComplianceType
from app.schemas.compliance import (
    BulkStatusUpdate,
    BulkStatusUpdateResult,
    ComplianceCalendarBucket,
    ComplianceCalendarResponse,
    ComplianceItemOut,
    ComplianceItemUpdate,
    ComplianceTypeOut,
    DashboardStats,
)
from app.services import audit, firms

router = APIRouter(prefix="/compliance", tags=["compliance"])

# Display states derived from due date + filing status.
DISPLAY_STATES = ("upcoming", "due_soon", "overdue", "filed", "not_applicable")

FILED_STATUSES = (ComplianceStatus.FILED, ComplianceStatus.DELAYED_FILED)


def _serialise(item: ComplianceItem, today: date) -> ComplianceItemOut:
    out = ComplianceItemOut.model_validate(item)
    out.client_name = item.client.name if item.client else None
    if item.compliance_type:
        out.compliance_type_code = item.compliance_type.code
        out.compliance_type_name = item.compliance_type.name
        out.category = item.compliance_type.category
        out.form_number = item.compliance_type.form_number
    out.display_status = item.derive_display_status(today)
    out.days_remaining = item.days_until_due(today)
    return out


def _empty_bucket(label: str) -> ComplianceCalendarBucket:
    return ComplianceCalendarBucket(
        period_label=label, total=0, upcoming=0, due_soon=0, overdue=0, filed=0
    )


def _tally(bucket: ComplianceCalendarBucket, state: str) -> None:
    bucket.total += 1
    if state in ("upcoming", "due_soon", "overdue", "filed"):
        setattr(bucket, state, getattr(bucket, state) + 1)


@router.get("/types", response_model=list[ComplianceTypeOut], summary="List compliance types")
def list_compliance_types(
    practitioner: CurrentPractitioner,
    db: DbSession,
    category: ComplianceCategory | None = Query(default=None),
):
    """The statutory calendar: system types plus any the firm has added."""
    filters = [
        ComplianceType.is_active.is_(True),
        or_(ComplianceType.firm_id.is_(None), ComplianceType.firm_id == practitioner.firm_id),
    ]
    if category is not None:
        filters.append(ComplianceType.category == category)
    stmt = (
        select(ComplianceType)
        .where(*filters)
        .order_by(ComplianceType.category, ComplianceType.due_day)
    )
    return list(db.scalars(stmt).all())


@router.get(
    "/calendar",
    response_model=ComplianceCalendarResponse,
    summary="The filing calendar for a period",
)
def compliance_calendar(
    practitioner: CurrentPractitioner,
    db: DbSession,
    from_date: date | None = Query(default=None, description="Due date lower bound (inclusive)"),
    to_date: date | None = Query(default=None, description="Due date upper bound (inclusive)"),
    period: str | None = Query(default=None, description="Exact period label, e.g. 2026-07"),
    client_id: uuid.UUID | None = Query(default=None),
    category: ComplianceCategory | None = Query(default=None),
    compliance_status: ComplianceStatus | None = Query(
        default=None, description="Filter on the stored filing status"
    ),
    display_status: str | None = Query(
        default=None, description="upcoming | due_soon | overdue | filed | not_applicable"
    ),
    assigned_to: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """Compliance items in a window, bucketed by period with status counts.

    ``display_status`` is derived (due date vs today) rather than stored, so it
    is applied after the query; ``total`` then reflects the filtered set.
    """
    if display_status is not None and display_status not in DISPLAY_STATES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"display_status must be one of: {', '.join(DISPLAY_STATES)}",
        )

    today = clock.today()
    start = from_date or date(today.year, today.month, 1)
    end = to_date or add_months(start, 3)
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to_date must not be before from_date",
        )

    filters = [
        ComplianceItem.firm_id == practitioner.firm_id,
        ComplianceItem.due_date >= start,
        ComplianceItem.due_date <= end,
    ]
    if period:
        filters.append(ComplianceItem.period_label == period)
    if client_id:
        filters.append(ComplianceItem.client_id == client_id)
    if compliance_status:
        filters.append(ComplianceItem.status == compliance_status)
    if assigned_to:
        filters.append(ComplianceItem.assigned_practitioner_id == assigned_to)

    stmt = (
        select(ComplianceItem)
        .options(
            selectinload(ComplianceItem.client), selectinload(ComplianceItem.compliance_type)
        )
        .where(*filters)
        .order_by(ComplianceItem.due_date, ComplianceItem.period_label)
    )
    if category:
        stmt = stmt.join(ComplianceType).where(ComplianceType.category == category)

    rows = list(db.scalars(stmt).all())
    serialised = [_serialise(item, today) for item in rows]
    if display_status:
        serialised = [item for item in serialised if item.display_status == display_status]

    summary = _empty_bucket("all")
    buckets: dict[str, ComplianceCalendarBucket] = {}
    for item in serialised:
        _tally(summary, item.display_status)
        bucket = buckets.setdefault(item.period_label, _empty_bucket(item.period_label))
        _tally(bucket, item.display_status)

    return ComplianceCalendarResponse(
        from_date=start,
        to_date=end,
        total=len(serialised),
        limit=limit,
        offset=offset,
        summary=summary,
        buckets=sorted(buckets.values(), key=lambda b: b.period_label),
        items=serialised[offset : offset + limit],
    )


@router.get("/items/{item_id}", response_model=ComplianceItemOut, summary="A single filing")
def get_compliance_item(
    item_id: uuid.UUID, practitioner: CurrentPractitioner, db: DbSession
):
    item = _get_item_or_404(db, practitioner.firm_id, item_id)
    return _serialise(item, clock.today())


@router.patch("/items/{item_id}", response_model=ComplianceItemOut, summary="Update a filing")
def update_compliance_item(
    item_id: uuid.UUID,
    payload: ComplianceItemUpdate,
    practitioner: CurrentPractitioner,
    db: DbSession,
):
    item = _get_item_or_404(db, practitioner.firm_id, item_id)
    updates = payload.model_dump(exclude_unset=True)

    # The same position the bulk endpoint takes, for the same reason: a filing
    # date belongs to a filed item, and recording one against anything else
    # says a return was lodged on a day it was not. The status it is measured
    # against is the one this patch leaves behind — the incoming one when the
    # caller named it, otherwise what the item already is, which is what makes
    # correcting the date of an already-filed item still work.
    resulting_status = updates.get("status") or item.status
    if updates.get("filed_on") is not None and resulting_status not in FILED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"filed_on cannot be set alongside status "
                f"'{resulting_status.value}' — a filing date belongs to a "
                "filed or delayed_filed item"
            ),
        )
    # And on the same terms, for a stronger version of the same reason: a
    # filing date is at least a date, whereas an acknowledgement number is
    # issued by the statutory portal at the moment it accepts a return. One
    # cannot exist for a return that was not lodged.
    if updates.get("acknowledgement_number") is not None and (
        resulting_status not in FILED_STATUSES
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"acknowledgement_number cannot be set alongside status "
                f"'{resulting_status.value}' — the portal issues one when it "
                "accepts a return"
            ),
        )

    if updates.get("assigned_practitioner_id"):
        try:
            firms.assert_assignable(
                db, practitioner.firm_id, updates["assigned_practitioner_id"]
            )
        except firms.NotAssignable as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    # ``_normalise_filing`` derives all three of these below, so all three are
    # watched whether or not the caller named them: a bare date correction that
    # moves a filing past its deadline changes the status too, and that is the
    # half of the change the record most needs to carry.
    watched = set(updates) | {"status", "filed_on", "acknowledgement_number"}
    before = audit.snapshot(item, watched)
    for key, value in updates.items():
        setattr(item, key, value)

    # Also on a bare date change: the filed/delayed split is derived from the
    # date, so a correction to the date has to re-derive it. Nothing re-ran
    # when the status was left out of the patch, and an item stayed `filed`
    # while carrying a date after its own deadline.
    #
    # ``due_date`` is the other half of that comparison and moves for a reason
    # this system exists to track: CBIC and CBDT extend deadlines routinely,
    # and the seeded calendar carries the ordinary dates precisely so a firm
    # can move them. Only ``filed_on`` re-derived, so a GSTR-3B lodged on the
    # 25th against an extension to the 30th kept the ``delayed_filed`` it was
    # given while the due date still said the 20th — the firm's own record
    # calling a return late that was filed five days inside the window, and the
    # same record it would show an assessing officer disputing a late fee. The
    # opposite direction is worse: a deadline corrected *earlier* than the
    # filing date left the item reading ``filed``.
    if {"status", "filed_on", "due_date"} & updates.keys():
        _normalise_filing(
            item,
            filed_on=updates.get("filed_on"),
            filed_on_given="filed_on" in updates,
        )

    audit.record(
        db,
        action="compliance_item.update",
        entity_type="compliance_item",
        entity_id=item.id,
        actor=practitioner,
        summary=f"{item.compliance_type.code} {item.period_label} → {item.status.value}",
        changes=audit.diff(before, audit.snapshot(item, watched)),
    )
    db.commit()
    db.refresh(item)
    return _serialise(item, clock.today())


@router.post(
    "/items/bulk-status",
    response_model=BulkStatusUpdateResult,
    summary="Update the status of many filings",
)
def bulk_update_status(
    payload: BulkStatusUpdate, practitioner: CurrentPractitioner, db: DbSession
):
    """Mark many filings at once — the common end-of-deadline workflow."""
    # ``filed_on`` here means "the date this batch was lodged", so it only says
    # anything alongside a filed status. Accepting it with any other status
    # would have to either discard it silently or record a filing date on
    # something that was not filed; refusing says which of the two fields the
    # caller got wrong.
    if payload.filed_on is not None and payload.status not in FILED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"filed_on cannot be set alongside status '{payload.status.value}' — "
                "a filing date belongs to a filed or delayed_filed item"
            ),
        )
    # An acknowledgement number is not a fact about a batch. The portal issues
    # one per return it accepts, so writing the same one across a selection
    # puts a number belonging to one return onto every other return in it —
    # in the register the firm would show an assessing officer, against
    # periods that number was never issued for. Filed or not: even a batch of
    # genuinely filed returns has as many numbers as it has returns, and this
    # endpoint can only carry one.
    if payload.acknowledgement_number is not None and len(set(payload.item_ids)) != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "acknowledgement_number identifies one return — set it on that "
                "filing rather than across a batch"
            ),
        )
    if payload.acknowledgement_number is not None and payload.status not in FILED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"acknowledgement_number cannot be set alongside status "
                f"'{payload.status.value}' — the portal issues one when it "
                "accepts a return"
            ),
        )

    items = list(
        db.scalars(
            select(ComplianceItem)
            .options(selectinload(ComplianceItem.compliance_type))
            .where(
                ComplianceItem.firm_id == practitioner.firm_id,
                ComplianceItem.id.in_(payload.item_ids),
            )
        ).all()
    )
    for item in items:
        item.status = payload.status
        if payload.acknowledgement_number:
            item.acknowledgement_number = payload.acknowledgement_number
        # Never "given" here: a non-filed status has just been refused a date
        # above, so reverting a batch always clears the stale one.
        _normalise_filing(item, filed_on=payload.filed_on, filed_on_given=False)

    audit.record(
        db,
        action="compliance_item.bulk_status",
        entity_type="compliance_item",
        actor=practitioner,
        summary=f"Set {len(items)} item(s) to {payload.status.value}",
        changes={
            "item_ids": [str(i.id) for i in items],
            "status": payload.status.value,
            # What they became, which the request does not say: a filing
            # lodged after its own deadline is stored as delayed_filed, so a
            # batch marked filed routinely lands on both.
            "resulting_statuses": sorted({item.status.value for item in items}),
        },
    )
    db.commit()
    return BulkStatusUpdateResult(
        updated=len(items), skipped=len(payload.item_ids) - len(items)
    )


@router.get("/dashboard", response_model=DashboardStats, summary="Firm dashboard counters")
def dashboard(practitioner: CurrentPractitioner, db: DbSession):
    """Practice-wide compliance status at a glance."""
    today = clock.today()
    firm_id = practitioner.firm_id

    total_clients = db.scalar(select(func.count(Client.id)).where(Client.firm_id == firm_id)) or 0
    active_clients = (
        db.scalar(
            select(func.count(Client.id)).where(
                Client.firm_id == firm_id, Client.is_active.is_(True)
            )
        )
        or 0
    )

    rows = list(
        db.scalars(
            select(ComplianceItem)
            .options(selectinload(ComplianceItem.compliance_type))
            .where(ComplianceItem.firm_id == firm_id)
        ).all()
    )

    stats = DashboardStats(
        total_clients=total_clients,
        active_clients=active_clients,
        total_items=len(rows),
        overdue=0,
        due_soon=0,
        upcoming=0,
        filed_this_month=0,
        unbilled_fee_paise=0,
        by_category={},
    )
    month_start = date(today.year, today.month, 1)
    for item in rows:
        state = item.derive_display_status(today)
        if state in ("overdue", "due_soon", "upcoming"):
            setattr(stats, state, getattr(stats, state) + 1)
            category = item.compliance_type.category.value
            stats.by_category[category] = stats.by_category.get(category, 0) + 1
        if item.status in FILED_STATUSES and item.filed_on and item.filed_on >= month_start:
            stats.filed_this_month += 1
        if item.status in FILED_STATUSES and not item.is_billed:
            stats.unbilled_fee_paise += item.fee_paise
    return stats


# --------------------------------------------------------------------------- #


def _get_item_or_404(db: Session, firm_id: uuid.UUID, item_id: uuid.UUID) -> ComplianceItem:
    item = db.get(ComplianceItem, item_id)
    if item is None or item.firm_id != firm_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Compliance item not found"
        )
    return item


def _normalise_filing(
    item: ComplianceItem, *, filed_on: date | None, filed_on_given: bool
) -> None:
    """Keep ``filed_on`` and the filed/delayed distinction consistent.

    ``filed_on_given`` says whether the caller named a date, which is a
    different question from whether the date is set: reverting a filing is
    what clears the date, and only a caller who named one is overriding that.

    The bulk endpoint used to pass its whole payload here as the "updates"
    dict, so ``"filed_on" in updates`` was true even when the field had been
    left out — and reverting a batch to pending left every stale filing date
    in place. That is not merely untidy: marking the item filed again later
    without naming a date reuses the stale one, and the stale date is what
    decides ``filed`` against ``delayed_filed``. A return lodged on time gets
    stamped as a late filing, in the record the firm would show an assessing
    officer.
    """
    if item.status in FILED_STATUSES:
        item.filed_on = filed_on or item.filed_on or clock.today()
        # A filing lodged after the due date is recorded as delayed.
        item.status = (
            ComplianceStatus.DELAYED_FILED
            if item.filed_on > item.due_date
            else ComplianceStatus.FILED
        )
        return

    if not filed_on_given:
        item.filed_on = None
    # The acknowledgement number goes with it, and is the more dangerous half
    # to leave behind. The portal issues one when it accepts a return, so a
    # pending item carrying one is a record that a return was accepted — and
    # something reads it: the filing-confirmation draft quotes the number back
    # to the client, so reverting a filing left the firm able to send "your
    # return has been filed successfully, acknowledgement number …" for a
    # return that is sitting on the chase list. Marked filed again later, the
    # item keeps that stale number for good, against a period it was never
    # issued for.
    #
    # Unconditional: setting one alongside a status that is not filed is
    # refused above, so there is no caller intent here to override.
    item.acknowledgement_number = None
