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
from sqlalchemy.orm import Session

from app.config import settings
from app.core import clock
from app.core.periods import add_months, compute_due_date, periods_for_frequency
from app.models.client import Client
from app.models.compliance import ComplianceItem, ComplianceType
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


def regenerate_for_firm(db: Session, firm_id: uuid.UUID, today: date | None = None) -> int:
    """Top up compliance items for every active client of a firm. Returns rows created."""
    clients = db.scalars(
        select(Client).where(Client.firm_id == firm_id, Client.is_active.is_(True))
    ).all()
    total = 0
    for client in clients:
        total += generate_compliance_items(db, client, today=today).created_count
    return total
