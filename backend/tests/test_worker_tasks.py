"""Tests for the Celery jobs.

The tasks are plain functions wrapped by Celery, so they are called directly —
no broker or worker is involved. They open their own ``SessionLocal``, which in
the test suite points at the same throwaway SQLite file as the ``db`` fixture,
so fixture writes must be committed before a task runs.

``draft_client_message`` is stubbed everywhere it would be reached: reminder
bodies are an AI feature, and these tests are about scheduling, not wording.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.base import (
    ComplianceStatus,
    EntityType,
    ReminderChannel,
    ReminderStatus,
    ReminderType,
)
from app.models.client import Client
from app.models.compliance import ComplianceItem, ComplianceType
from app.models.firm import Firm
from app.models.reminder import Reminder
from app.worker import tasks

RUN_DATE = date(2026, 7, 1)


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """Reminder bodies come from a stub, not a live model."""
    monkeypatch.setattr(
        tasks, "draft_client_message", lambda **kwargs: f"Please send documents for {kwargs['client_name']}."
    )


# ------------------------------------------------------------------ factories --


def make_firm(db, *, name="Sharma & Associates", is_active=True) -> Firm:
    firm = Firm(name=name, email=f"{name.split()[0].lower()}@example.in", is_active=is_active)
    db.add(firm)
    db.flush()
    return firm


def make_client(db, firm, *, name="Nimbus Textiles", email="accounts@nimbus.in", is_active=True):
    client = Client(
        firm_id=firm.id,
        name=name,
        entity_type=EntityType.PRIVATE_LIMITED,
        email=email,
        is_active=is_active,
        onboarded_on=date(2025, 4, 1),
    )
    db.add(client)
    db.flush()
    return client


def get_type(db, code: str) -> ComplianceType:
    return db.scalar(select(ComplianceType).where(ComplianceType.code == code))


def make_item(
    db,
    firm,
    client,
    compliance_type,
    *,
    due_date,
    period_label="2026-06",
    status=ComplianceStatus.PENDING,
) -> ComplianceItem:
    item = ComplianceItem(
        firm_id=firm.id,
        client_id=client.id,
        compliance_type_id=compliance_type.id,
        period_label=period_label,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        due_date=due_date,
        status=status,
    )
    db.add(item)
    db.flush()
    return item


def make_reminder(db, firm, client, *, scheduled_for, recipient="accounts@nimbus.in", **kwargs):
    reminder = Reminder(
        firm_id=firm.id,
        client_id=client.id,
        reminder_type=ReminderType.FILING,
        channel=ReminderChannel.EMAIL,
        status=kwargs.pop("status", ReminderStatus.SCHEDULED),
        recipient=recipient,
        scheduled_for=scheduled_for,
        **kwargs,
    )
    db.add(reminder)
    db.flush()
    return reminder


# --------------------------------------------------------- reminder scheduling --


class TestScheduleComplianceReminders:
    def test_queues_a_reminder_at_an_exact_offset(self, db):
        """GSTR-3B declares offsets [10, 5, 2, 1]; 10 days out fires one."""
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=10))
        db.commit()

        result = tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat())

        assert result == {"queued": 1}
        reminder = db.scalars(select(Reminder)).one()
        assert reminder.status is ReminderStatus.SCHEDULED
        assert reminder.reminder_type is ReminderType.FILING
        assert reminder.recipient == "accounts@nimbus.in"
        assert reminder.extra["offset_days"] == 10
        assert "GSTR-3B" in reminder.subject
        assert "2026-06" in reminder.subject
        assert reminder.body == "Please send documents for Nimbus Textiles."

    def test_no_reminder_on_a_day_that_is_not_an_offset(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")  # offsets [10, 5, 2, 1]
        make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=8))
        db.commit()

        assert tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat()) == {
            "queued": 0
        }
        assert db.scalars(select(Reminder)).all() == []

    def test_each_offset_fires_once_as_the_deadline_approaches(self, db):
        """Escalation: the same item reminds again at 5, 2 and 1 days out."""
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        due = RUN_DATE + timedelta(days=10)
        make_item(db, firm, client, ctype, due_date=due)
        db.commit()

        queued = [
            tasks.schedule_compliance_reminders_task(
                today=(due - timedelta(days=offset)).isoformat()
            )["queued"]
            for offset in (10, 5, 2, 1)
        ]

        assert queued == [1, 1, 1, 1]
        offsets = sorted(r.extra["offset_days"] for r in db.scalars(select(Reminder)).all())
        assert offsets == [1, 2, 5, 10]

    def test_running_twice_on_the_same_day_does_not_double_send(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=5))
        db.commit()

        first = tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat())
        second = tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat())

        assert (first["queued"], second["queued"]) == (1, 0)
        assert len(db.scalars(select(Reminder)).all()) == 1

    @pytest.mark.parametrize(
        "status",
        [ComplianceStatus.FILED, ComplianceStatus.DELAYED_FILED, ComplianceStatus.NOT_APPLICABLE],
    )
    def test_closed_items_are_never_reminded(self, db, status):
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        make_item(
            db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=10), status=status
        )
        db.commit()

        assert tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat()) == {
            "queued": 0
        }

    def test_in_progress_items_are_still_reminded(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        make_item(
            db,
            firm,
            client,
            ctype,
            due_date=RUN_DATE + timedelta(days=10),
            status=ComplianceStatus.IN_PROGRESS,
        )
        db.commit()

        assert tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat()) == {
            "queued": 1
        }

    def test_deactivated_clients_are_skipped(self, db):
        firm = make_firm(db)
        client = make_client(db, firm, is_active=False)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=10))
        db.commit()

        assert tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat()) == {
            "queued": 0
        }

    def test_already_overdue_items_are_not_reminded(self, db):
        """The scheduler looks forward only; chasing past deadlines is separate."""
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        make_item(db, firm, client, ctype, due_date=RUN_DATE - timedelta(days=1))
        db.commit()

        assert tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat()) == {
            "queued": 0
        }

    def test_deadlines_beyond_the_sixty_day_horizon_are_ignored(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "ITR_NON_AUDIT")  # offsets include 45
        make_item(
            db,
            firm,
            client,
            ctype,
            due_date=RUN_DATE + timedelta(days=61),
            period_label="FY2025-26",
        )
        db.commit()

        assert tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat()) == {
            "queued": 0
        }

    def test_long_lead_reminders_inside_the_horizon_fire(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "ITR_NON_AUDIT")  # offsets [45, 30, 15, 7, 3]
        make_item(
            db,
            firm,
            client,
            ctype,
            due_date=RUN_DATE + timedelta(days=45),
            period_label="FY2025-26",
        )
        db.commit()

        assert tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat()) == {
            "queued": 1
        }

    def test_reminder_is_scheduled_for_nine_am_ist(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=10))
        db.commit()

        tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat())

        scheduled = db.scalars(select(Reminder)).one().scheduled_for
        # 09:00 IST == 03:30 UTC the same day.
        assert (scheduled.hour, scheduled.minute) == (3, 30)
        assert scheduled.date() == RUN_DATE

    def test_each_client_gets_its_own_reminder(self, db):
        firm = make_firm(db)
        first = make_client(db, firm, name="Nimbus Textiles", email="a@nimbus.in")
        second = make_client(db, firm, name="Ravi Traders", email="b@ravi.in")
        ctype = get_type(db, "GSTR3B_MONTHLY")
        due = RUN_DATE + timedelta(days=10)
        make_item(db, firm, first, ctype, due_date=due)
        make_item(db, firm, second, ctype, due_date=due)
        db.commit()

        assert tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat()) == {
            "queued": 2
        }
        assert {r.recipient for r in db.scalars(select(Reminder)).all()} == {
            "a@nimbus.in",
            "b@ravi.in",
        }

    def test_defaults_to_today_when_no_date_is_given(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        make_item(db, firm, client, ctype, due_date=date.today() + timedelta(days=10))
        db.commit()

        assert tasks.schedule_compliance_reminders_task() == {"queued": 1}


# ------------------------------------------------------------ reminder dispatch --


class TestDispatchDueReminders:
    def test_sends_reminders_whose_time_has_come(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(db, firm, client, scheduled_for=datetime.now(UTC) - timedelta(hours=1))
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {"sent": 1, "failed": 0, "retrying": 0}

        db.expire_all()
        reminder = db.scalars(select(Reminder)).one()
        assert reminder.status is ReminderStatus.SENT
        assert reminder.sent_at is not None
        assert reminder.attempt_count == 1

    def test_leaves_future_reminders_alone(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(db, firm, client, scheduled_for=datetime.now(UTC) + timedelta(hours=2))
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {"sent": 0, "failed": 0, "retrying": 0}

        db.expire_all()
        assert db.scalars(select(Reminder)).one().status is ReminderStatus.SCHEDULED

    def test_a_client_with_no_contact_address_fails_loudly(self, db):
        firm = make_firm(db)
        client = make_client(db, firm, email=None)
        make_reminder(
            db,
            firm,
            client,
            scheduled_for=datetime.now(UTC) - timedelta(hours=1),
            recipient=None,
        )
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {"sent": 0, "failed": 1, "retrying": 0}

        db.expire_all()
        reminder = db.scalars(select(Reminder)).one()
        assert reminder.status is ReminderStatus.FAILED
        assert "No recipient" in reminder.error_message
        assert reminder.attempt_count == 1

    def test_already_sent_reminders_are_not_resent(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(
            db,
            firm,
            client,
            scheduled_for=datetime.now(UTC) - timedelta(hours=1),
            status=ReminderStatus.SENT,
        )
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {"sent": 0, "failed": 0, "retrying": 0}

    def test_cancelled_reminders_are_not_sent(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(
            db,
            firm,
            client,
            scheduled_for=datetime.now(UTC) - timedelta(hours=1),
            status=ReminderStatus.CANCELLED,
        )
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {"sent": 0, "failed": 0, "retrying": 0}

    def test_batch_limit_is_respected(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        past = datetime.now(UTC) - timedelta(hours=1)
        for _ in range(5):
            make_reminder(db, firm, client, scheduled_for=past)
        db.commit()

        assert tasks.dispatch_due_reminders_task(limit=2) == {
            "sent": 2,
            "failed": 0,
            "retrying": 0,
        }

        db.expire_all()
        remaining = [
            r
            for r in db.scalars(select(Reminder)).all()
            if r.status is ReminderStatus.SCHEDULED
        ]
        assert len(remaining) == 3


# -------------------------------------------------------- generation & overdue --


class TestGenerateComplianceItemsTask:
    def test_generates_items_for_active_firms(self, db):
        firm = make_firm(db)
        make_client(db, firm)
        db.commit()

        result = tasks.generate_compliance_items_task()

        assert result["firms"] == 1
        assert result["created"] > 0
        assert db.scalar(select(ComplianceItem).limit(1)) is not None

    def test_is_idempotent(self, db):
        firm = make_firm(db)
        make_client(db, firm)
        db.commit()

        first = tasks.generate_compliance_items_task()
        second = tasks.generate_compliance_items_task()

        assert first["created"] > 0
        assert second["created"] == 0

    def test_inactive_firms_are_skipped(self, db):
        firm = make_firm(db, is_active=False)
        make_client(db, firm)
        db.commit()

        assert tasks.generate_compliance_items_task() == {"created": 0, "firms": 0}


class TestFlagOverdueTask:
    def test_counts_clients_with_an_overdue_filing(self, db):
        firm = make_firm(db)
        overdue_client = make_client(db, firm, name="Late Ltd", email="late@example.in")
        on_time = make_client(db, firm, name="Prompt Ltd", email="prompt@example.in")
        ctype = get_type(db, "GSTR3B_MONTHLY")
        make_item(db, firm, overdue_client, ctype, due_date=date.today() - timedelta(days=3))
        make_item(db, firm, on_time, ctype, due_date=date.today() + timedelta(days=30))
        db.commit()

        result = tasks.flag_overdue_task()

        assert result["clients_with_overdue"] == 1
        assert result["has_active_clients"] is True

    def test_filed_items_are_not_overdue(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        make_item(
            db,
            firm,
            client,
            ctype,
            due_date=date.today() - timedelta(days=3),
            status=ComplianceStatus.FILED,
        )
        db.commit()

        assert tasks.flag_overdue_task()["clients_with_overdue"] == 0

    def test_empty_practice_reports_nothing(self, db):
        assert tasks.flag_overdue_task() == {
            "clients_with_overdue": 0,
            "has_active_clients": False,
        }


class TestIstMorning:
    def test_converts_nine_am_ist_to_utc(self):
        moment = tasks._ist_morning(date(2026, 7, 1))
        assert moment == datetime(2026, 7, 1, 3, 30, tzinfo=UTC)

    def test_result_is_timezone_aware(self):
        assert tasks._ist_morning(date(2026, 7, 1)).tzinfo is UTC
