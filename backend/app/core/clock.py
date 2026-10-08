"""The clock the practice runs on.

Every business date in DoAide Reach is a date in India. A return is due on the 20th
IST, a filing lodged at 02:00 on the 31st was lodged on the 31st, and an
invoice raised on 1 April belongs to the new financial year — none of those
questions have anything to do with where the server happens to be.

``date.today()`` answers a different question: what date it is in the
process's own timezone. Nothing here sets ``TZ``, so that is UTC in every
container this ships in, and UTC is behind IST by five and a half hours. For
the first five and a half hours of every Indian day the two disagree, and every
one of those hours is a working hour on a deadline:

* A GSTR-3B lodged at 01:30 IST on the 20th — the last night of the window, and
  exactly when it gets done — was stamped as filed on the 19th. Recorded a day
  early, against the wrong period's cut-off, in the record the firm would show
  an assessing officer.
* Celery beat is configured in ``Asia/Kolkata``, so the nightly jobs are
  scheduled at 01:30, 02:00 and 02:30 IST — all of which are *the previous day*
  in UTC. Every one of them ran asking about yesterday: invoices went overdue a
  day late, and the task generator's horizon was short by a day.
* A practitioner working past midnight saw every "days remaining" off by one.

So business dates come from here instead. Timestamps — ``created_at``, when a
reminder was sent, token issue times — stay UTC and are stored aware; those are
instants, not dates, and UTC is right for them. This is only for the moment an
instant has to be collapsed into "which working day was that".
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

# India has one timezone and does not observe daylight saving, so a fixed
# offset is the whole of it — no tz database needed, and no ambiguity to
# resolve at a transition.
IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET, "IST")


def now() -> datetime:
    """The current instant, expressed in IST."""
    return datetime.now(IST)


def today() -> date:
    """The current date in India — what a practitioner means by "today"."""
    return now().date()


def to_ist(moment: datetime) -> datetime:
    """Re-express an instant in IST.

    A naive datetime is read as UTC, which is what the database hands back on
    SQLite and what every timestamp in this system is stored as.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(IST)


def date_of(moment: datetime) -> date:
    """Which Indian working day an instant fell on.

    Used wherever a stored timestamp is counted into a day, a week or a month:
    a task completed at 04:00 IST on 1 August is August's work, though UTC
    still calls it July.
    """
    return to_ist(moment).date()
