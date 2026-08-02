"""Indian financial-year period maths and statutory due-date computation.

The Indian financial year runs 1 April - 31 March. Fiscal quarters are
Q1 Apr-Jun, Q2 Jul-Sep, Q3 Oct-Dec, Q4 Jan-Mar.

A compliance type describes its deadline as "day ``due_day`` of the month
``due_month_offset`` months after the period ends", with optional per-period
overrides (e.g. the TDS return for Q4 is due 31 May, two months out, not one).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

FY_START_MONTH = 4


@dataclass(frozen=True)
class Period:
    """A single filing period."""

    label: str
    start: date
    end: date

    @property
    def key(self) -> str:
        """Suffix used to look up per-period due-date overrides."""
        return self.label.rsplit("-", 1)[-1]


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def add_months(d: date, months: int) -> date:
    """Shift a date by ``months``, clamping the day to the target month's length."""
    total = (d.year * 12 + d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(d.day, days_in_month(year, month)))


def month_end(year: int, month: int) -> date:
    return date(year, month, days_in_month(year, month))


def fiscal_year_start(d: date, start_month: int = FY_START_MONTH) -> int:
    """Return the calendar year in which the financial year containing ``d`` began."""
    return d.year if d.month >= start_month else d.year - 1


def fy_label(start_year: int) -> str:
    """2025 -> 'FY2025-26'."""
    return f"FY{start_year}-{(start_year + 1) % 100:02d}"


def fiscal_quarter_index(d: date, start_month: int = FY_START_MONTH) -> int:
    """1-based fiscal quarter number for a date (Apr-Jun == 1)."""
    return ((d.month - start_month) % 12) // 3 + 1


def monthly_periods(start: date, end: date) -> list[Period]:
    """Every calendar month that overlaps [start, end]."""
    periods: list[Period] = []
    year, month = start.year, start.month
    while date(year, month, 1) <= end:
        p_start = date(year, month, 1)
        p_end = month_end(year, month)
        periods.append(Period(label=f"{year}-{month:02d}", start=p_start, end=p_end))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return periods


def quarterly_periods(start: date, end: date, start_month: int = FY_START_MONTH) -> list[Period]:
    """Every fiscal quarter that overlaps [start, end]."""
    periods: list[Period] = []
    # Rewind to the first day of the fiscal quarter containing `start`.
    offset = (start.month - start_month) % 12 % 3
    cursor = add_months(date(start.year, start.month, 1), -offset)
    while cursor <= end:
        q_end = add_months(cursor, 2)
        q_end = month_end(q_end.year, q_end.month)
        fy = fiscal_year_start(cursor, start_month)
        quarter = fiscal_quarter_index(cursor, start_month)
        periods.append(
            Period(label=f"{fy_label(fy)}-Q{quarter}", start=cursor, end=q_end)
        )
        cursor = add_months(cursor, 3)
    return periods


def annual_periods(start: date, end: date, start_month: int = FY_START_MONTH) -> list[Period]:
    """Every financial year that overlaps [start, end]."""
    periods: list[Period] = []
    fy = fiscal_year_start(start, start_month)
    last_fy = fiscal_year_start(end, start_month)
    while fy <= last_fy:
        p_start = date(fy, start_month, 1)
        p_end = add_months(p_start, 12) - timedelta(days=1)
        periods.append(Period(label=fy_label(fy), start=p_start, end=p_end))
        fy += 1
    return periods


def compute_due_date(
    period: Period,
    due_day: int,
    due_month_offset: int = 1,
    due_overrides: dict | None = None,
) -> date:
    """Resolve the statutory due date for ``period``.

    ``due_overrides`` maps a period key (``"Q4"``, ``"03"``) to a partial rule,
    e.g. ``{"Q4": {"month_offset": 2, "day": 31}}``.
    """
    day = due_day
    offset = due_month_offset

    override = (due_overrides or {}).get(period.key)
    if override:
        day = override.get("day", day)
        offset = override.get("month_offset", offset)

    anchor = add_months(date(period.end.year, period.end.month, 1), offset)
    return date(anchor.year, anchor.month, min(day, days_in_month(anchor.year, anchor.month)))


def periods_for_frequency(
    frequency: str, start: date, end: date, fy_start_month: int = FY_START_MONTH
) -> list[Period]:
    """Dispatch to the right period generator for a compliance type's frequency."""
    match frequency:
        case "monthly":
            return monthly_periods(start, end)
        case "quarterly":
            return quarterly_periods(start, end, fy_start_month)
        case "annual":
            return annual_periods(start, end, fy_start_month)
        case "one_time":
            return [Period(label=f"{start:%Y-%m-%d}", start=start, end=end)]
        case _:
            raise ValueError(f"Unsupported frequency: {frequency!r}")
