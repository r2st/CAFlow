"""Compliance calendar: listing by period, status filtering and filing updates."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import case, func, or_, select
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
from app.services import tasks as task_service

router = APIRouter(prefix="/compliance", tags=["compliance"])

# Display states derived from due date + filing status.
DISPLAY_STATES = ("upcoming", "due_soon", "overdue", "filed", "not_applicable")

FILED_STATUSES = (ComplianceStatus.FILED, ComplianceStatus.DELAYED_FILED)

# The statuses that report themselves rather than being read off the due date.
# Its complement is what ``ComplianceItem.derive_display_status`` resolves into
# overdue / due_soon / upcoming, and the dashboard groups by the same split —
# written as "everything but these" so a status added later buckets the same
# way in both places.
SETTLED_STATUSES = (*FILED_STATUSES, ComplianceStatus.NOT_APPLICABLE)


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


def _tally(bucket: ComplianceCalendarBucket, state: str, count: int = 1) -> None:
    bucket.total += count
    if state in ("upcoming", "due_soon", "overdue", "filed"):
        setattr(bucket, state, getattr(bucket, state) + count)


def display_state_sql(today: date):
    """``derive_display_status`` as the SQL expression it already is.

    The same five-way split the model computes in Python, so the calendar's
    counts and its rows cannot disagree — and the same one
    ``clients._compliance_summary`` and the dashboard already express. ``case``
    rather than an aggregate ``FILTER`` clause, for the reason those give:
    FILTER wants SQLite 3.30 and this has to render the same on both backends.
    """
    return case(
        (ComplianceItem.status.in_(FILED_STATUSES), "filed"),
        (ComplianceItem.status == ComplianceStatus.NOT_APPLICABLE, "not_applicable"),
        (ComplianceItem.due_date < today, "overdue"),
        (
            ComplianceItem.due_date
            <= today + timedelta(days=ComplianceItem.DUE_SOON_WINDOW_DAYS),
            "due_soon",
        ),
        else_="upcoming",
    )


def _task_follow_up(item: ComplianceItem, was: ComplianceStatus) -> str | None:
    """Which way the work raised for ``item`` has to move, if either.

    Split out from :func:`_follow_with_tasks` so the batch endpoint can decide
    for every filing first and then move them in one query each — see
    :func:`bulk_update_status`.
    """
    if was == item.status:
        return None
    if item.status == ComplianceStatus.NOT_APPLICABLE:
        return "withdraw"
    if was == ComplianceStatus.NOT_APPLICABLE:
        return "reinstate"
    return None


def _follow_with_tasks(db: Session, item: ComplianceItem, was: ComplianceStatus) -> None:
    """Move the work raised for a filing whose status a practitioner just changed.

    Ruling a filing not-applicable is the same decision as off-boarding the
    client or dropping the registration behind it — the obligation is gone —
    and both of those already take the task with them. Editing the filing
    directly did not, and that is the path a practitioner actually uses: the
    everyday "this one does not apply to them" on the calendar screen, and the
    end-of-deadline batch on ``bulk-status``.

    What was left behind is a task nobody can discharge. It stays TODO on
    somebody's queue, it is counted in the workload view a manager reads to
    decide who is drowning, and it counts down to a statutory deadline against
    a return this firm has decided is not owed — going *overdue* on the day
    that deadline passes, in red, for good. Marked done by someone working
    down the list, it becomes a record that a return was filed which nobody
    ever owed.

    Reversible on the same terms as everywhere else: ``withdrawn_from_status``
    is what a reinstatement reads, so putting the filing back puts its task
    back at the status it held. A task a manager cancelled themselves carries
    no marker and is left alone.
    """
    match _task_follow_up(item, was):
        case "withdraw":
            task_service.withdraw_tasks_for_items(db, [item.id])
        case "reinstate":
            task_service.reinstate_tasks_for_items(db, [item.id])


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

    Counted by the database and paged by the database, rather than read whole
    and reduced here. ``display_status`` is derived from the due date rather
    than stored, and that is what used to make this the one list endpoint whose
    ``limit`` bought nothing: every filing in the window was loaded, hydrated
    into a mapped object *and* validated into a response model, and then all but
    one page of it was thrown away.

    The window is the caller's, so its size is too. A filing is a permanent
    record and the nightly generator adds a year of them per client ahead of
    time, so ``?from_date=2000-01-01&to_date=2099-12-31`` is a firm's entire
    calendar — past, present and pre-generated — materialised to render two
    hundred rows. This is the screen a practitioner opens first each morning,
    and every one of them opens it in the same half hour.

    ``display_state_sql`` is what makes it expressible: the derived state is a
    date comparison, so the filter, the per-period buckets and the summary are
    all one grouped query, and only the page itself is read as rows. The counts
    are still counts of the filtered set — ``total`` is the sum of the buckets,
    so the two cannot drift apart.
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

    display_state = display_state_sql(today)
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
    if display_status:
        filters.append(display_state == display_status)

    def narrow(stmt):
        stmt = stmt.where(*filters)
        if category:
            stmt = stmt.join(ComplianceType).where(ComplianceType.category == category)
        return stmt

    summary = _empty_bucket("all")
    buckets: dict[str, ComplianceCalendarBucket] = {}
    grouped = db.execute(
        narrow(
            select(
                ComplianceItem.period_label, display_state, func.count(ComplianceItem.id)
            )
        ).group_by(ComplianceItem.period_label, display_state)
    ).all()
    for period_label, state, count in grouped:
        _tally(summary, state, count)
        bucket = buckets.setdefault(period_label, _empty_bucket(period_label))
        _tally(bucket, state, count)

    rows = db.scalars(
        narrow(
            select(ComplianceItem).options(
                selectinload(ComplianceItem.client),
                selectinload(ComplianceItem.compliance_type),
            )
        )
        # ``id`` breaks the tie: two filings of one client can share a deadline
        # and a period, and a page boundary falling between them would otherwise
        # repeat or drop a row depending on how the rows came back.
        .order_by(ComplianceItem.due_date, ComplianceItem.period_label, ComplianceItem.id)
        .limit(limit)
        .offset(offset)
    ).all()

    return ComplianceCalendarResponse(
        from_date=start,
        to_date=end,
        total=summary.total,
        limit=limit,
        offset=offset,
        summary=summary,
        buckets=sorted(buckets.values(), key=lambda b: b.period_label),
        items=[_serialise(item, today) for item in rows],
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
    was_status = item.status
    was_due = item.due_date
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

    # After the normalisation, so the transition is read off the status the
    # item actually ends on rather than the one the caller named.
    _follow_with_tasks(db, item, was_status)
    # And after that, so a task the line above reinstated is open in time to be
    # moved. A deadline that has shifted takes the work raised for it along;
    # see :func:`task_service.retarget_tasks_for_item`.
    moved = task_service.retarget_tasks_for_item(db, item, was_due=was_due)

    audit.record(
        db,
        action="compliance_item.update",
        entity_type="compliance_item",
        entity_id=item.id,
        actor=practitioner,
        summary=f"{item.compliance_type.code} {item.period_label} → {item.status.value}"
        + (f"; moved {moved} task(s) to the new deadline" if moved else ""),
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
    # Decided for the whole batch, then moved in one query each. The work
    # follows the filing exactly as it does on the single-item patch — see
    # :func:`_follow_with_tasks` — but this endpoint takes up to five hundred
    # ids, and asking per filing meant up to five hundred round-trips inside one
    # transaction to answer a question one ``IN`` clause answers. It is the
    # end-of-deadline batch: a firm marks a month of GST returns filed in a
    # single click, on the twentieth, when every other practice is doing the
    # same to the same database.
    withdraw: list[uuid.UUID] = []
    reinstate: list[uuid.UUID] = []
    for item in items:
        was_status = item.status
        item.status = payload.status
        if payload.acknowledgement_number:
            item.acknowledgement_number = payload.acknowledgement_number
        # Never "given" here: a non-filed status has just been refused a date
        # above, so reverting a batch always clears the stale one.
        _normalise_filing(item, filed_on=payload.filed_on, filed_on_given=False)
        match _task_follow_up(item, was_status):
            case "withdraw":
                withdraw.append(item.id)
            case "reinstate":
                reinstate.append(item.id)
    task_service.withdraw_tasks_for_items(db, withdraw)
    task_service.reinstate_tasks_for_items(db, reinstate)

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
    # Counted against the *distinct* ids asked for. A selection is built by
    # clicking rows, and a list rebuilt between clicks — the calendar re-reads
    # on every filter change — hands the same filing back twice. Measured
    # against the raw list, each repeat became a phantom skip: "12 updated, 3
    # skipped" for a batch of fifteen clicks on twelve filings, all twelve of
    # which were written. There is nothing to go and look for, and the number
    # a practitioner is meant to act on is the one naming filings the firm
    # cannot reach — a stale id, or another firm's.
    return BulkStatusUpdateResult(
        updated=len(items), skipped=len(set(payload.item_ids)) - len(items)
    )


@router.get("/dashboard", response_model=DashboardStats, summary="Firm dashboard counters")
def dashboard(practitioner: CurrentPractitioner, db: DbSession):
    """Practice-wide compliance status at a glance.

    Counted by the database rather than by loading the firm's filings and
    tallying them here. Every counter on this screen is an aggregate, and this
    is the landing page — so what used to happen on each visit was that every
    compliance item the firm has ever had was read off disk, hydrated into a
    mapped object, joined to its compliance type, and then reduced to eight
    integers and a small dict.

    That set only grows. A filing is a permanent record, and the nightly
    generator adds a year of them per client ahead of time, so a practice with
    a few hundred clients on the books for a few years is materialising six
    figures of rows to render a handful of numbers — and does it again for
    every practitioner who opens the app in the morning, which is all of them
    at once. The counters themselves have not moved; only where they are
    computed has.

    The derived states are the same three ``derive_display_status`` produces
    for an open filing, expressed as the date comparison it already is. Filed,
    delayed-filed and not-applicable items never fell into those buckets, so
    only the two open statuses are grouped.
    """
    today = clock.today()
    firm_id = practitioner.firm_id
    month_start = date(today.year, today.month, 1)
    due_soon_until = today + timedelta(days=ComplianceItem.DUE_SOON_WINDOW_DAYS)

    client_counts = db.execute(
        select(
            func.count(Client.id),
            func.coalesce(func.sum(case((Client.is_active.is_(True), 1), else_=0)), 0),
        ).where(Client.firm_id == firm_id)
    ).one()

    total_items = (
        db.scalar(
            select(func.count(ComplianceItem.id)).where(ComplianceItem.firm_id == firm_id)
        )
        or 0
    )

    # ``case`` rather than an aggregate FILTER clause: FILTER wants SQLite 3.30
    # and this has to render the same on both backends the suite and the deploy
    # use.
    display_state = case(
        (ComplianceItem.due_date < today, "overdue"),
        (ComplianceItem.due_date <= due_soon_until, "due_soon"),
        else_="upcoming",
    )
    open_rows = db.execute(
        select(ComplianceType.category, display_state, func.count(ComplianceItem.id))
        .join(ComplianceType, ComplianceType.id == ComplianceItem.compliance_type_id)
        .where(
            ComplianceItem.firm_id == firm_id,
            ComplianceItem.status.not_in(SETTLED_STATUSES),
        )
        # Ordered so the category breakdown the UI renders is stable between
        # requests rather than however the rows happened to come back.
        .group_by(ComplianceType.category, display_state)
        .order_by(ComplianceType.category)
    ).all()

    # One pass over the filed work for both of its counters: how much was
    # lodged this month, and what of it is still waiting on an invoice.
    filed_this_month, unbilled_fee_paise = db.execute(
        select(
            func.coalesce(
                func.sum(case((ComplianceItem.filed_on >= month_start, 1), else_=0)), 0
            ),
            func.coalesce(
                func.sum(
                    case(
                        (ComplianceItem.is_billed.is_(False), ComplianceItem.fee_paise),
                        else_=0,
                    )
                ),
                0,
            ),
        ).where(
            ComplianceItem.firm_id == firm_id,
            ComplianceItem.status.in_(FILED_STATUSES),
        )
    ).one()

    stats = DashboardStats(
        total_clients=client_counts[0] or 0,
        active_clients=client_counts[1] or 0,
        total_items=total_items,
        overdue=0,
        due_soon=0,
        upcoming=0,
        filed_this_month=filed_this_month or 0,
        unbilled_fee_paise=unbilled_fee_paise or 0,
        by_category={},
    )
    for category, state, count in open_rows:
        setattr(stats, state, getattr(stats, state) + count)
        key = category.value
        stats.by_category[key] = stats.by_category.get(key, 0) + count
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
