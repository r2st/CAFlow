"""Tests for the Celery jobs.

The tasks are plain functions wrapped by Celery, so they are called directly —
no broker or worker is involved. They open their own ``SessionLocal``, which in
the test suite points at the same throwaway SQLite file as the ``db`` fixture,
so fixture writes must be committed before a task runs.

``draft_client_message`` is stubbed everywhere it would be reached: reminder
bodies are an AI feature, and these tests are about scheduling, not wording.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

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
from app.worker.celery_app import celery_app

RUN_DATE = date(2026, 7, 1)


class _StatementRecorder:
    """A stand-in session that keeps the statement instead of running it.

    Lets a test read the SQL the claim would issue, on a dialect the suite does
    not run against.
    """

    def __init__(self) -> None:
        self.statements: list = []

    def scalars(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(first=lambda: None)


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


class TestDispatchSendsEachReminderOnce:
    """The dispatcher's guarantee: a reminder is mailed once, not once per run.

    Beat fires it every fifteen minutes and the worker runs two processes, so
    two dispatches genuinely overlap whenever a batch outlives its interval.
    The old shape — read the whole batch, send it, commit at the end — turned
    both an overlap and a crash into a firm's entire client list being mailed
    twice. These cover the two halves of the replacement: the row is claimed
    exclusively, and each outcome is committed before the next send starts.
    """

    def test_the_claim_takes_a_row_lock_a_second_dispatcher_skips(self):
        """The lock is the whole mechanism, and SQLite cannot show it.

        ``FOR UPDATE SKIP LOCKED`` is what makes two concurrent dispatchers
        divide the queue rather than duplicate it, and the test suite runs on
        SQLite, which renders no locking clause at all — so a change that
        dropped ``with_for_update`` would pass every other test in this file
        while quietly restoring the double-send. Compile the statement against
        both dialects and read the SQL instead.
        """
        recorder = _StatementRecorder()

        tasks._claim_next_due(recorder, cutoff=datetime.now(UTC), exclude=set())

        statement = recorder.statements[0]
        assert "FOR UPDATE SKIP LOCKED" in str(statement.compile(dialect=postgresql.dialect()))
        # Stated rather than assumed: this is why the behaviour is unobservable
        # in the rest of the suite.
        assert "FOR UPDATE" not in str(statement.compile(dialect=sqlite.dialect()))

    def test_a_crash_mid_batch_keeps_the_deliveries_already_made(self, db, monkeypatch):
        """A worker killed at message three has sent two, and says so.

        With one commit at the end of the batch, those two were mailed and
        recorded nowhere — and ``task_acks_late`` then redelivers the task, so
        both clients hear it again. Committing per reminder costs at most the
        one in flight.
        """
        firm = make_firm(db)
        past = datetime.now(UTC) - timedelta(hours=1)
        for index in range(4):
            recipient = f"c{index}@nimbus.in"
            client = make_client(db, firm, name=f"Client {index}", email=recipient)
            make_reminder(
                db,
                firm,
                client,
                scheduled_for=past + timedelta(minutes=index),
                recipient=recipient,
            )
        db.commit()

        delivered: list[str] = []

        def dies_on_the_third(reminder, firm=None):
            delivered.append(reminder.recipient)
            if len(delivered) == 3:
                raise RuntimeError("worker killed mid-send")
            return tasks.delivery.DeliveryOutcome(delivered=True, transport="log")

        monkeypatch.setattr(tasks.delivery, "deliver", dies_on_the_third)

        with pytest.raises(RuntimeError):
            tasks.dispatch_due_reminders_task()

        db.expire_all()
        status = {r.recipient: r.status for r in db.scalars(select(Reminder)).all()}
        assert status["c0@nimbus.in"] is ReminderStatus.SENT
        assert status["c1@nimbus.in"] is ReminderStatus.SENT
        # In flight when it died: nobody knows whether the relay took it, so it
        # stays as it was found and the next run may send it once more.
        assert status["c2@nimbus.in"] is ReminderStatus.SCHEDULED
        assert status["c3@nimbus.in"] is ReminderStatus.SCHEDULED
        # And the two already recorded are not offered again.
        monkeypatch.setattr(
            tasks.delivery,
            "deliver",
            lambda reminder, firm=None: tasks.delivery.DeliveryOutcome(
                delivered=True, transport="log"
            ),
        )
        assert tasks.dispatch_due_reminders_task() == {"sent": 2, "failed": 0, "retrying": 0}

    def test_a_reminder_held_for_the_next_run_is_not_retried_inside_this_one(
        self, db, monkeypatch
    ):
        """A transient failure defers; it does not spin.

        Re-selecting after every commit means the reminder that was just left
        ``SCHEDULED`` is the very next row the query would return. Without the
        exclusion it would be retried until the batch limit or the attempt
        ceiling ran out — turning one greylisted message into three.
        """
        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(db, firm, client, scheduled_for=datetime.now(UTC) - timedelta(hours=1))
        db.commit()

        attempts = []

        def greylisted(reminder, firm=None):
            attempts.append(reminder.id)
            raise tasks.mailer.DeliveryError("451 greylisted, try again later")

        monkeypatch.setattr(tasks.delivery, "deliver", greylisted)

        result = tasks.dispatch_due_reminders_task(limit=25)

        assert result == {"sent": 0, "failed": 0, "retrying": 1}
        assert len(attempts) == 1
        db.expire_all()
        reminder = db.scalars(select(Reminder)).one()
        assert reminder.status is ReminderStatus.SCHEDULED
        assert reminder.attempt_count == 1

    def test_the_longest_wait_goes_out_first(self, db, monkeypatch):
        """A partial run must drain the backlog, not the newest arrivals."""
        firm = make_firm(db)
        now = datetime.now(UTC)
        for label, hours in (("newest", 1), ("oldest", 3), ("middle", 2)):
            client = make_client(db, firm, name=label, email=f"{label}@nimbus.in")
            make_reminder(
                db,
                firm,
                client,
                scheduled_for=now - timedelta(hours=hours),
                recipient=f"{label}@nimbus.in",
            )
        db.commit()

        order: list[str] = []

        def record(reminder, firm=None):
            order.append(reminder.recipient)
            return tasks.delivery.DeliveryOutcome(delivered=True, transport="log")

        monkeypatch.setattr(tasks.delivery, "deliver", record)

        tasks.dispatch_due_reminders_task()

        assert order == ["oldest@nimbus.in", "middle@nimbus.in", "newest@nimbus.in"]

    def test_a_truncated_batch_says_so_rather_than_looking_complete(self, db, caplog):
        """Silently stopping at the limit reads as "everything went out"."""
        firm = make_firm(db)
        client = make_client(db, firm)
        past = datetime.now(UTC) - timedelta(hours=1)
        for _ in range(3):
            make_reminder(db, firm, client, scheduled_for=past)
        db.commit()

        with caplog.at_level(logging.WARNING, logger="app.worker.tasks"):
            assert tasks.dispatch_due_reminders_task(limit=2) == {
                "sent": 2,
                "failed": 0,
                "retrying": 0,
            }

        assert "batch limit" in caplog.text
        assert "1 reminder(s) still due" in caplog.text

    def test_a_run_that_exactly_empties_the_queue_is_quiet(self, db, caplog):
        """Reaching the limit is not the same as leaving work behind."""
        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(db, firm, client, scheduled_for=datetime.now(UTC) - timedelta(hours=1))
        db.commit()

        with caplog.at_level(logging.WARNING, logger="app.worker.tasks"):
            tasks.dispatch_due_reminders_task(limit=1)

        assert "batch limit" not in caplog.text

    def test_a_deferred_reminder_is_not_reported_as_a_backlog(self, db, monkeypatch, caplog):
        """A greylisted message waits for the next run by design.

        Counting it as overflow would put a warning in the log on every run
        where one relay was slow, which is how a real backlog warning stops
        being read.
        """
        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(db, firm, client, scheduled_for=datetime.now(UTC) - timedelta(hours=1))
        db.commit()

        def greylisted(reminder, firm=None):
            raise tasks.mailer.DeliveryError("451 greylisted, try again later")

        monkeypatch.setattr(tasks.delivery, "deliver", greylisted)

        with caplog.at_level(logging.WARNING, logger="app.worker.tasks"):
            assert tasks.dispatch_due_reminders_task(limit=1) == {
                "sent": 0,
                "failed": 0,
                "retrying": 1,
            }

        assert "batch limit" not in caplog.text


class TestWorkerRuntimeLimits:
    """The three timeouts that keep ``task_acks_late`` from meaning "twice".

    Acknowledging after the work is done is what lets a dead worker's task be
    picked up by a live one. The cost is that every one of these numbers has to
    agree with the others: a task that outlives the broker's redelivery window
    is handed to a second worker *while the first is still running it*.
    """

    def test_a_task_cannot_outlive_the_brokers_redelivery_window(self):
        conf = celery_app.conf
        soft = conf.task_soft_time_limit
        hard = conf.task_time_limit
        visibility = conf.broker_transport_options["visibility_timeout"]

        # Soft first, so the task raises inside itself and can roll back; hard
        # as the backstop; redelivery only after the hard kill has happened.
        assert soft < hard < visibility

    def test_redelivery_is_the_premise_the_limits_are_protecting(self):
        assert celery_app.conf.task_acks_late is True
        # One task in flight per process, so a slow batch cannot also be
        # sitting on a queue of reserved work nobody is looking at.
        assert celery_app.conf.worker_prefetch_multiplier == 1

    def test_a_wedged_dispatch_cannot_outlast_its_own_schedule(self):
        """Beat fires the dispatcher four times an hour.

        A run allowed to exceed that interval piles up behind itself, and every
        overlapping run is another set of row locks contending for the same
        queue.
        """
        schedule = celery_app.conf.beat_schedule["dispatch-due-reminders"]["schedule"]
        assert schedule.minute == set(range(0, 60, 15))

        assert celery_app.conf.task_soft_time_limit <= 15 * 60


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
