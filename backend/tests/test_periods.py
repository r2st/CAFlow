"""Unit tests for financial-year period maths and statutory due dates.

These encode the actual Indian deadlines, so a regression here is a regression
in every generated compliance item.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.periods import (
    Period,
    add_months,
    annual_periods,
    compute_due_date,
    fiscal_quarter_index,
    fiscal_year_start,
    fy_label,
    monthly_periods,
    periods_for_frequency,
    quarterly_periods,
)
from app.seeds.compliance_types import seed_by_code


def due_for(code: str, period: Period) -> date:
    seed = seed_by_code(code)
    return compute_due_date(
        period, seed["due_day"], seed["due_month_offset"], seed.get("due_overrides")
    )


class TestDateArithmetic:
    def test_add_months_clamps_to_shorter_month(self):
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # leap year

    def test_add_months_crosses_year_boundaries(self):
        assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
        assert add_months(date(2026, 2, 15), -3) == date(2025, 11, 15)


class TestFinancialYear:
    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (date(2026, 4, 1), 2026),
            (date(2026, 12, 31), 2026),
            (date(2027, 3, 31), 2026),
            (date(2026, 3, 31), 2025),
        ],
    )
    def test_fiscal_year_start(self, day, expected):
        assert fiscal_year_start(day) == expected

    def test_fy_label(self):
        assert fy_label(2025) == "FY2025-26"
        assert fy_label(2099) == "FY2099-00"

    @pytest.mark.parametrize(
        ("day", "quarter"),
        [
            (date(2026, 4, 1), 1),
            (date(2026, 6, 30), 1),
            (date(2026, 9, 1), 2),
            (date(2026, 11, 5), 3),
            (date(2027, 1, 1), 4),
            (date(2027, 3, 31), 4),
        ],
    )
    def test_fiscal_quarter_index(self, day, quarter):
        assert fiscal_quarter_index(day) == quarter


class TestPeriodGeneration:
    def test_monthly_periods_span_and_labels(self):
        periods = monthly_periods(date(2026, 4, 10), date(2026, 6, 5))
        assert [p.label for p in periods] == ["2026-04", "2026-05", "2026-06"]
        assert periods[0].start == date(2026, 4, 1)
        assert periods[0].end == date(2026, 4, 30)

    def test_quarterly_periods_use_fiscal_quarters(self):
        periods = quarterly_periods(date(2026, 5, 1), date(2027, 2, 1))
        assert [p.label for p in periods] == [
            "FY2026-27-Q1",
            "FY2026-27-Q2",
            "FY2026-27-Q3",
            "FY2026-27-Q4",
        ]
        assert periods[0].start == date(2026, 4, 1)
        assert periods[0].end == date(2026, 6, 30)
        assert periods[3].start == date(2027, 1, 1)
        assert periods[3].end == date(2027, 3, 31)

    def test_annual_periods_run_april_to_march(self):
        periods = annual_periods(date(2026, 6, 1), date(2027, 6, 1))
        assert [p.label for p in periods] == ["FY2026-27", "FY2027-28"]
        assert periods[0].start == date(2026, 4, 1)
        assert periods[0].end == date(2027, 3, 31)

    def test_periods_for_frequency_dispatch(self):
        assert periods_for_frequency("monthly", date(2026, 4, 1), date(2026, 4, 30))
        assert periods_for_frequency("quarterly", date(2026, 4, 1), date(2026, 6, 30))
        assert periods_for_frequency("annual", date(2026, 4, 1), date(2027, 3, 31))

    def test_unknown_frequency_raises(self):
        with pytest.raises(ValueError, match="Unsupported frequency"):
            periods_for_frequency("fortnightly", date(2026, 4, 1), date(2026, 5, 1))

    def test_period_key_extraction(self):
        assert Period("2026-07", date(2026, 7, 1), date(2026, 7, 31)).key == "07"
        assert Period("FY2026-27-Q4", date(2027, 1, 1), date(2027, 3, 31)).key == "Q4"


class TestPeriodBoundaries:
    """The edges of the generators, which is where a missing filing comes from.

    Every case here survived a deliberate break of the code it exercises: the
    generators were covered line-for-line, and the suite still passed with the
    year rollover off by one and the window's last month dropped. A period that
    is never generated is a statutory filing nobody is reminded about, so these
    are checked at the boundary rather than in the middle.
    """

    def test_monthly_periods_cross_the_calendar_year(self):
        # December is where the month counter rolls, and the label carries the
        # year — so an off-by-one here mislabels every period after it.
        periods = monthly_periods(date(2026, 11, 5), date(2027, 2, 10))
        assert [p.label for p in periods] == ["2026-11", "2026-12", "2027-01", "2027-02"]
        assert periods[1].end == date(2026, 12, 31)
        assert periods[2].start == date(2027, 1, 1)

    def test_monthly_window_ending_on_the_first_still_includes_that_month(self):
        # The window is inclusive, and a month that has begun has a filing due
        # for it. Excluding it loses the return entirely.
        periods = monthly_periods(date(2026, 4, 10), date(2026, 6, 1))
        assert [p.label for p in periods] == ["2026-04", "2026-05", "2026-06"]

    def test_monthly_window_inside_one_month_yields_that_month(self):
        periods = monthly_periods(date(2026, 4, 10), date(2026, 4, 12))
        assert [p.label for p in periods] == ["2026-04"]

    @pytest.mark.parametrize(
        ("start", "quarter_start"),
        [
            (date(2026, 4, 20), date(2026, 4, 1)),
            (date(2026, 5, 20), date(2026, 4, 1)),
            (date(2026, 6, 20), date(2026, 4, 1)),
            (date(2026, 7, 20), date(2026, 7, 1)),
            (date(2026, 8, 20), date(2026, 7, 1)),
            (date(2026, 9, 20), date(2026, 7, 1)),
            (date(2026, 10, 20), date(2026, 10, 1)),
            (date(2026, 11, 20), date(2026, 10, 1)),
            (date(2026, 12, 20), date(2026, 10, 1)),
            (date(2027, 1, 20), date(2027, 1, 1)),
            (date(2027, 2, 20), date(2027, 1, 1)),
            (date(2027, 3, 20), date(2027, 1, 1)),
        ],
    )
    def test_quarterly_periods_rewind_to_the_quarter_containing_the_start(
        self, start, quarter_start
    ):
        # Onboarding happens on whatever day a firm signs a client up, so the
        # rewind runs from all twelve months in practice. Checked from each of
        # them: the modulo that does it is wrong in a different month for every
        # way of getting it wrong.
        periods = quarterly_periods(start, start)
        assert periods[0].start == quarter_start

    def test_quarterly_window_ending_on_a_quarter_start_includes_that_quarter(self):
        periods = quarterly_periods(date(2026, 4, 1), date(2026, 7, 1))
        assert [p.label for p in periods] == ["FY2026-27-Q1", "FY2026-27-Q2"]

    def test_annual_window_inside_one_year_yields_that_year(self):
        periods = annual_periods(date(2026, 6, 1), date(2026, 8, 1))
        assert [p.label for p in periods] == ["FY2026-27"]

    def test_one_time_period_is_labelled_by_its_start_date(self):
        periods = periods_for_frequency("one_time", date(2026, 7, 4), date(2026, 9, 30))
        assert [p.label for p in periods] == ["2026-07-04"]
        assert periods[0].end == date(2026, 9, 30)

    def test_due_date_defaults_to_the_month_after_the_period(self):
        # The default offset is what every seed omitting one relies on.
        july = Period("2026-07", date(2026, 7, 1), date(2026, 7, 31))
        assert compute_due_date(july, 20) == date(2026, 8, 20)

    def test_due_date_clamps_a_day_past_the_end_of_a_short_month(self):
        january = Period("2026-01", date(2026, 1, 1), date(2026, 1, 31))
        assert compute_due_date(january, 31) == date(2026, 2, 28)


class TestStatutoryDueDates:
    """The dates a CA would recite from memory."""

    def test_gstr1_monthly_is_due_on_the_11th(self):
        july = Period("2026-07", date(2026, 7, 1), date(2026, 7, 31))
        assert due_for("GSTR1_MONTHLY", july) == date(2026, 8, 11)

    def test_gstr3b_monthly_is_due_on_the_20th(self):
        july = Period("2026-07", date(2026, 7, 1), date(2026, 7, 31))
        assert due_for("GSTR3B_MONTHLY", july) == date(2026, 8, 20)

    def test_december_gst_rolls_into_january(self):
        december = Period("2026-12", date(2026, 12, 1), date(2026, 12, 31))
        assert due_for("GSTR3B_MONTHLY", december) == date(2027, 1, 20)

    @pytest.mark.parametrize(
        ("quarter", "start", "end", "expected"),
        [
            ("FY2026-27-Q1", date(2026, 4, 1), date(2026, 6, 30), date(2026, 7, 31)),
            ("FY2026-27-Q2", date(2026, 7, 1), date(2026, 9, 30), date(2026, 10, 31)),
            ("FY2026-27-Q3", date(2026, 10, 1), date(2026, 12, 31), date(2027, 1, 31)),
            # Q4 gets an extra month by statute.
            ("FY2026-27-Q4", date(2027, 1, 1), date(2027, 3, 31), date(2027, 5, 31)),
        ],
    )
    def test_tds_quarterly_returns(self, quarter, start, end, expected):
        period = Period(quarter, start, end)
        assert due_for("TDS_RETURN_24Q", period) == expected
        assert due_for("TDS_RETURN_26Q", period) == expected

    def test_tds_payment_is_the_7th_except_for_march(self):
        june = Period("2026-06", date(2026, 6, 1), date(2026, 6, 30))
        march = Period("2026-03", date(2026, 3, 1), date(2026, 3, 31))
        assert due_for("TDS_PAYMENT_MONTHLY", june) == date(2026, 7, 7)
        assert due_for("TDS_PAYMENT_MONTHLY", march) == date(2026, 4, 30)

    def test_income_tax_return_non_audit_is_31_july(self):
        fy = Period("FY2025-26", date(2025, 4, 1), date(2026, 3, 31))
        assert due_for("ITR_NON_AUDIT", fy) == date(2026, 7, 31)

    def test_income_tax_return_audit_is_31_october(self):
        fy = Period("FY2025-26", date(2025, 4, 1), date(2026, 3, 31))
        assert due_for("ITR_AUDIT", fy) == date(2026, 10, 31)

    def test_tax_audit_report_is_30_september(self):
        fy = Period("FY2025-26", date(2025, 4, 1), date(2026, 3, 31))
        assert due_for("TAX_AUDIT_3CD", fy) == date(2026, 9, 30)

    def test_roc_annual_filings(self):
        fy = Period("FY2025-26", date(2025, 4, 1), date(2026, 3, 31))
        assert due_for("ROC_AOC4", fy) == date(2026, 10, 30)
        assert due_for("ROC_MGT7", fy) == date(2026, 11, 29)
        assert due_for("ROC_LLP_FORM11", fy) == date(2026, 5, 30)
        assert due_for("ROC_LLP_FORM8", fy) == date(2026, 10, 30)

    def test_gstr9_annual_is_31_december(self):
        fy = Period("FY2025-26", date(2025, 4, 1), date(2026, 3, 31))
        assert due_for("GSTR9_ANNUAL", fy) == date(2026, 12, 31)

    def test_advance_tax_falls_inside_the_quarter(self):
        q1 = Period("FY2026-27-Q1", date(2026, 4, 1), date(2026, 6, 30))
        q4 = Period("FY2026-27-Q4", date(2027, 1, 1), date(2027, 3, 31))
        assert due_for("ADVANCE_TAX", q1) == date(2026, 6, 15)
        assert due_for("ADVANCE_TAX", q4) == date(2027, 3, 15)

    def test_due_day_clamps_to_short_months(self):
        # A "31st" rule landing on February must clamp, not overflow.
        january = Period("2026-01", date(2026, 1, 1), date(2026, 1, 31))
        assert compute_due_date(january, due_day=31, due_month_offset=1) == date(2026, 2, 28)
