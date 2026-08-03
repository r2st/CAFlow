"""Auto-generation of per-client compliance items from the statutory calendar.

Given a client's registration flags (GST registered? TDS deductor? company?),
this walks the applicable compliance types, expands each into filing periods
over a date window, and materialises ``ComplianceItem`` rows.

Generation is idempotent: an item already present for
(client, compliance_type, period_label) is never duplicated, so this can be
re-run safely after a client's registrations change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.core import clock
from app.core.periods import add_months, compute_due_date, periods_for_frequency
from app.models.base import ComplianceStatus
from app.models.client import Client
from app.models.compliance import ComplianceItem, ComplianceType
from app.services import tasks as task_service
from app.services.applicability import applies_to


@dataclass
class GenerationResult:
    created: list[ComplianceItem]
    skipped_existing: int
    window_start: date
    window_end: date

    @property
    def created_count(self) -> int:
        return len(self.created)


def default_window(client: Client, today: date | None = None) -> tuple[date, date]:
    """The date range to generate for: from onboarding (or today) forward."""
    today = today or clock.today()
    start = max(client.onboarded_on, add_months(today, -3))
    end = add_months(today, settings.compliance_generation_months)
    return start, end


def applicable_types(db: Session, client: Client) -> list[ComplianceType]:
    """Active compliance types (system-wide plus the firm's own) matching the client."""
    stmt = select(ComplianceType).where(
        ComplianceType.is_active.is_(True),
        (ComplianceType.firm_id.is_(None)) | (ComplianceType.firm_id == client.firm_id),
    )
    return [ct for ct in db.scalars(stmt).all() if applies_to(ct.applicability_rule, client)]


def max_lookback_months(compliance_type: ComplianceType) -> int:
    """The largest gap between a period ending and its filing falling due.

    Needed because a period can end well before the generation window yet still
    be due inside it — FY2025-26 ends 31 Mar 2026 but its AOC-4 is due 30 Oct
    2026. Without looking back this far, annual filings silently go missing.
    """
    offsets = [compliance_type.due_month_offset]
    for override in (compliance_type.due_overrides or {}).values():
        offsets.append(override.get("month_offset", compliance_type.due_month_offset))
    return max(offsets)


def _existing_keys(db: Session, client_id: uuid.UUID) -> set[tuple[uuid.UUID, str]]:
    rows = db.execute(
        select(ComplianceItem.compliance_type_id, ComplianceItem.period_label).where(
            ComplianceItem.client_id == client_id
        )
    ).all()
    return {(row[0], row[1]) for row in rows}


def generate_compliance_items(
    db: Session,
    client: Client,
    *,
    window_start: date | None = None,
    window_end: date | None = None,
    today: date | None = None,
) -> GenerationResult:
    """Create the compliance items a client owes over the window.

    Only periods whose due date falls inside the window are materialised, so a
    client onboarded mid-year does not inherit deadlines that already passed
    before the firm took them on.

    What is created is flushed before returning. The session runs with
    ``autoflush=False``, so an unflushed row is one the next call's
    already-generated lookup cannot see — and a second run inside the same
    transaction would then create every filing a second time.
    """
    today = today or clock.today()
    default_start, default_end = default_window(client, today)
    start = window_start or default_start
    end = window_end or default_end

    existing = _existing_keys(db, client.id)
    created: list[ComplianceItem] = []
    skipped = 0

    for compliance_type in applicable_types(db, client):
        # Search back far enough to catch periods that ended before the window
        # but fall due inside it; the due-date filter below trims the excess.
        # The extra month is slack, and only slack: searching further back is
        # not observable in the result, just slower. Nothing pins that +1, and
        # nothing can.
        search_start = add_months(start, -(max_lookback_months(compliance_type) + 1))
        periods = periods_for_frequency(
            compliance_type.frequency,
            search_start,
            end,
            client.financial_year_start_month,
        )
        for period in periods:
            due_date = compute_due_date(
                period,
                compliance_type.due_day,
                compliance_type.due_month_offset,
                compliance_type.due_overrides,
            )
            if not (start <= due_date <= end):
                continue
            if (compliance_type.id, period.label) in existing:
                skipped += 1
                continue

            fee = client.service_fees.get(compliance_type.code, compliance_type.default_fee_paise)
            item = ComplianceItem(
                firm_id=client.firm_id,
                client_id=client.id,
                compliance_type_id=compliance_type.id,
                assigned_practitioner_id=client.assigned_practitioner_id,
                period_label=period.label,
                period_start=period.start,
                period_end=period.end,
                due_date=due_date,
                fee_paise=int(fee or 0),
            )
            db.add(item)
            created.append(item)
            existing.add((compliance_type.id, period.label))

    if created:
        db.flush()

    return GenerationResult(
        created=created, skipped_existing=skipped, window_start=start, window_end=end
    )


@dataclass
class ReconcileResult:
    withdrawn: int
    reinstated: int


# Statuses a change of registration may close. A filed return is a record of
# what was lodged and stays one; an item already ruled out has nothing to do.
OPEN_STATUSES = (ComplianceStatus.PENDING, ComplianceStatus.IN_PROGRESS)


def reconcile_applicability(
    db: Session, client: Client, *, today: date | None = None
) -> ReconcileResult:
    """Close the filings a client's registrations no longer call for, reopen those they do.

    Generation only ever added. A client who surrenders their GST registration —
    or moves to QRMP, or converts from a company to an LLP — kept every filing
    the old registration had already materialised, a year of them: pending on
    the calendar, counted on the dashboard, going overdue one by one, raising
    tasks, and emailing the client to ask for the paperwork behind a return
    nobody owes. Marked filed by someone working down the list, each one then
    carries a fee onto an invoice.

    Only periods that have not begun. A registration surrendered in the middle
    of a month still owes that month's return, and the period a change lands
    inside is precisely the one this cannot decide — so it is left for the
    practitioner, who knows the effective date, and only obligations that
    cannot have arisen are closed.

    ``offboarded_from_status`` records what the item was, which is what makes
    this reversible: a flag switched back on reinstates exactly the filings it
    closed, at the status they held. Reinstatement is needed for the same
    reason it is after an off-boarding — generation skips a (type, period) that
    already exists whatever its status, so nothing else would ever bring them
    back.
    """
    today = today or clock.today()
    applicable = {ct.id for ct in applicable_types(db, client)}
    items = list(
        db.scalars(
            select(ComplianceItem)
            .options(selectinload(ComplianceItem.compliance_type))
            .where(ComplianceItem.client_id == client.id)
        ).all()
    )

    closed: list[uuid.UUID] = []
    reopened: list[uuid.UUID] = []
    for item in items:
        applies = item.compliance_type_id in applicable
        if not applies and item.status in OPEN_STATUSES and item.period_start > today:
            item.offboarded_from_status = item.status
            item.status = ComplianceStatus.NOT_APPLICABLE
            closed.append(item.id)
        elif (
            applies
            and item.status == ComplianceStatus.NOT_APPLICABLE
            and item.offboarded_from_status is not None
        ):
            item.status = item.offboarded_from_status
            item.offboarded_from_status = None
            reopened.append(item.id)

    if closed or reopened:
        db.flush()
    # The work raised for these filings follows them. A task outlives the
    # obligation it exists to discharge otherwise: still on a queue, still
    # counted, still counting down to a deadline the client stopped owing when
    # they surrendered the registration behind it.
    task_service.withdraw_tasks_for_items(db, closed)
    task_service.reinstate_tasks_for_items(db, reopened)
    return ReconcileResult(withdrawn=len(closed), reinstated=len(reopened))


def regenerate_for_firm(db: Session, firm_id: uuid.UUID, today: date | None = None) -> int:
    """Top up compliance items for every active client of a firm. Returns rows created."""
    clients = db.scalars(
        select(Client).where(Client.firm_id == firm_id, Client.is_active.is_(True))
    ).all()
    total = 0
    for client in clients:
        total += generate_compliance_items(db, client, today=today).created_count
    return total
