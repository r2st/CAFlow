"""The business clock: dates are Indian dates, whatever the server thinks.

Every deployment of this runs its containers on UTC — nothing sets ``TZ`` — and
UTC is five and a half hours behind IST. For the first five and a half hours of
each Indian working day the two disagree about what day it is, and the tests
here pin the places where that mattered.

The interval is not an edge case anyone can wait out: Celery beat is scheduled
in ``Asia/Kolkata`` and three of its nightly jobs fire at 01:30, 02:00 and
02:30 IST, all of which land inside it. Nor is it quiet for people — filing on
the last night of a GST window is ordinary practice.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

from app.core import clock
from app.services import billing
from app.services.reminders import REMINDER_HOUR_IST, ist_morning

# 01:30 IST on 20 August — the hour a GSTR-3B actually gets filed. The same
# instant is 20:00 UTC on the *19th*, which is what the process clock reports.
LATE_NIGHT_IST = datetime(2026, 8, 20, 1, 30, tzinfo=clock.IST)
LATE_NIGHT_UTC = LATE_NIGHT_IST.astimezone(UTC)


class TestIstZone:
    def test_the_offset_is_five_and_a_half_hours(self):
        assert clock.IST.utcoffset(None) == timedelta(hours=5, minutes=30)

    def test_it_does_not_shift_across_the_year(self):
        """India keeps one offset all year — no daylight saving to resolve."""
        winter = datetime(2026, 1, 15, 12, tzinfo=clock.IST)
        summer = datetime(2026, 7, 15, 12, tzinfo=clock.IST)
        assert winter.utcoffset() == summer.utcoffset() == clock.IST_OFFSET

    def test_now_is_expressed_in_ist_and_today_is_its_date(self):
        moment = clock.now()
        assert moment.utcoffset() == clock.IST_OFFSET
        assert clock.today() == moment.date()

    def test_today_tracks_ist_rather_than_the_process_timezone(self):
        """The two agree for most of the day and must not be assumed to always."""
        assert clock.today() == datetime.now(UTC).astimezone(clock.IST).date()


class TestCollapsingAnInstantToAWorkingDay:
    def test_the_small_hours_belong_to_the_indian_day_that_has_begun(self):
        assert LATE_NIGHT_UTC.date() == date(2026, 8, 19)
        assert clock.date_of(LATE_NIGHT_UTC) == date(2026, 8, 20)

    def test_a_naive_timestamp_is_read_as_utc(self):
        """SQLite hands datetimes back without a zone; they are still UTC."""
        assert clock.date_of(LATE_NIGHT_UTC.replace(tzinfo=None)) == date(2026, 8, 20)

    def test_an_instant_in_any_zone_lands_on_the_same_indian_day(self):
        elsewhere = LATE_NIGHT_UTC.astimezone(timezone(timedelta(hours=-4)))
        assert clock.date_of(elsewhere) == date(2026, 8, 20)

    def test_it_agrees_with_utc_for_the_rest_of_the_day(self):
        midday = datetime(2026, 8, 20, 12, tzinfo=clock.IST)
        assert clock.date_of(midday) == midday.astimezone(UTC).date() == date(2026, 8, 20)

    def test_to_ist_preserves_the_instant(self):
        assert clock.to_ist(LATE_NIGHT_UTC) == LATE_NIGHT_UTC == LATE_NIGHT_IST


class TestNineInTheMorning:
    """``ist_morning`` is the one place that already knew about IST."""

    def test_it_lands_at_nine_indian_time_on_the_day_asked_for(self):
        scheduled = ist_morning(date(2026, 8, 20))
        in_ist = clock.to_ist(scheduled)
        assert (in_ist.date(), in_ist.hour) == (date(2026, 8, 20), REMINDER_HOUR_IST)

    def test_it_is_stored_as_an_aware_utc_instant(self):
        scheduled = ist_morning(date(2026, 8, 20))
        assert scheduled.utcoffset() == timedelta(0)
        assert (scheduled.hour, scheduled.minute) == (3, 30)


class TestWorkFinishedInTheSmallHoursCountsForTheRightMonth:
    """``workload`` reads ``completed_at`` — a UTC instant — into a month."""

    def test_the_first_of_the_month_at_four_in_the_morning_is_this_month(self):
        # 04:00 IST on 1 August is 22:30 UTC on 31 July.
        completed = datetime(2026, 8, 1, 4, tzinfo=clock.IST).astimezone(UTC)
        month_start = date(2026, 8, 1)

        assert completed.date() < month_start  # what the old comparison saw
        assert clock.date_of(completed) >= month_start


class TestBillingDatesFollowTheBusinessClock:
    def test_an_invoice_raised_at_one_in_the_morning_on_the_first_of_april(self):
        """The financial year turns over at midnight IST, not at 05:30.

        Numbered off the process clock in that window, an invoice would carry
        the *previous* year's series while being dated in the new one — and the
        series is what a GST return is reconciled against.
        """
        turnover = datetime(2026, 4, 1, 1, 0, tzinfo=clock.IST)
        assert billing.number_prefix(clock.date_of(turnover)) == "INV/FY2026-27/"
        assert billing.number_prefix(turnover.astimezone(UTC).date()) == "INV/FY2025-26/"
