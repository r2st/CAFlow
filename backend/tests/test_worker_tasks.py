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

from app.config import settings
from app.core import clock
from app.models.base import (
    ComplianceStatus,
    DocumentCategory,
    EntityType,
    InvoiceStatus,
    ReminderChannel,
    ReminderStatus,
    ReminderType,
)
from app.models.client import Client
from app.models.compliance import ComplianceItem, ComplianceType
from app.models.document import Document
from app.models.firm import Firm
from app.models.invoice import Invoice
from app.models.reminder import Reminder
from app.services import ai
from app.services import reminders as reminder_service
from app.worker import tasks
from app.worker.celery_app import celery_app

RUN_DATE = date(2026, 7, 1)
PDF_BYTES = b"%PDF-1.4\n% ledger\n"


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
        reminder_type=kwargs.pop("reminder_type", ReminderType.FILING),
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
        make_item(db, firm, client, ctype, due_date=clock.today() + timedelta(days=10))
        db.commit()

        assert tasks.schedule_compliance_reminders_task() == {"queued": 1}


class TestOnlyOneFilingSweepQueuesAtATime:
    """"Once per (item, offset)" was a read-decide-write nothing ordered.

    Read what is already queued for this filing, decide there is nothing, add
    one. The run does not commit until it has been through every tenant, and it
    spends up to ``ai_draft_budget_seconds`` on the wording along the way, so
    the gap between the read and the commit is a minute or two wide.

    Two runs inside that gap both read an empty queue and both add. Beat firing
    while a previous run is still drafting is the ordinary way to get two, and
    Celery acknowledges this task only once it finishes, so a redelivery
    overlaps it too. What came out was a client told twice, on the same morning
    and over their own CA's name, that the same return is due on the 20th.

    The document and payment sweeps were ordered for exactly this; the filing
    reminder lives in the worker and was left behind. Ordering is the whole
    fix, so ordering is what is asserted: a lock taken after the decision
    orders the writes and nothing else.
    """

    def _record_order(self, monkeypatch) -> list[str]:
        order: list[str] = []
        monkeypatch.setattr(
            reminder_service.firms, "lock_firm", lambda session, fid: order.append("lock")
        )
        real = reminder_service.already_queued
        monkeypatch.setattr(
            tasks.reminder_service,
            "already_queued",
            lambda *a, **kw: (order.append("read"), real(*a, **kw))[1],
        )
        return order

    def test_the_firm_is_held_before_its_queue_is_read(self, db, monkeypatch):
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=10))
        db.commit()
        order = self._record_order(monkeypatch)

        tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat())

        assert order[:2] == ["lock", "read"]

    def test_a_reminder_queued_while_we_waited_is_not_queued_again(self, db, monkeypatch):
        """The interleaving itself, in the order it happens.

        The competing run is staged on the lock: it runs and commits on its own
        session at the moment this one takes the firm's row, which is the
        instant a real loser resumes at. Everything after that is the ordinary
        code path deciding what is left to chase.
        """
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        item = make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=10))
        item_id = item.id
        db.commit()

        fired: list = []

        def winner_commits_first(session, fid):
            if fired:
                return
            fired.append(fid)
            tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat())

        monkeypatch.setattr(reminder_service.firms, "lock_firm", winner_commits_first)

        tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat())

        assert fired, "the competing run never happened"
        db.expire_all()
        queued = db.scalars(
            select(Reminder).where(Reminder.compliance_item_id == item_id)
        ).all()
        assert len(queued) == 1, "the client was told twice about the same deadline"

    def test_a_firm_that_is_not_served_is_never_held(self, db, monkeypatch):
        """Nothing is queued for it, so there is nothing to order — and taking
        its row would block that firm's own requests for a sweep doing no work
        on it."""
        firm = make_firm(db, name="Dormant & Co", is_active=False)
        client = make_client(db, firm, email="ops@dormant.in")
        ctype = get_type(db, "GSTR3B_MONTHLY")
        make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=10))
        db.commit()
        held: list = []
        monkeypatch.setattr(
            reminder_service.firms, "lock_firm", lambda session, fid: held.append(fid)
        )

        assert tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat()) == {
            "queued": 0
        }
        assert held == []

    def test_the_sweep_still_spans_every_firm_and_holds_them_in_order(self, db, monkeypatch):
        """Locking per firm must not have narrowed what a beat run covers."""
        for name in ("Zephyr Associates", "Anand & Co", "Mehta Partners"):
            firm = make_firm(db, name=name)
            client = make_client(db, firm, email=f"{name.split()[0].lower()}@example.in")
            ctype = get_type(db, "GSTR3B_MONTHLY")
            make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=10))
        db.commit()
        held: list = []
        monkeypatch.setattr(
            reminder_service.firms, "lock_firm", lambda session, fid: held.append(fid)
        )

        result = tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat())

        assert result == {"queued": 3}
        assert set(held) == reminder_service.firms.servable_firm_ids(db)
        assert held == sorted(held), "an undefined lock order lets two sweeps cross"


# ------------------------------------------------------------ reminder dispatch --


class TestDispatchDueReminders:
    def test_sends_reminders_whose_time_has_come(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(db, firm, client, scheduled_for=datetime.now(UTC) - timedelta(hours=1))
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {"sent": 1, "failed": 0, "retrying": 0, "withdrawn": 0}

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

        assert tasks.dispatch_due_reminders_task() == {"sent": 0, "failed": 0, "retrying": 0, "withdrawn": 0}

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

        assert tasks.dispatch_due_reminders_task() == {"sent": 0, "failed": 1, "retrying": 0, "withdrawn": 0}

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

        assert tasks.dispatch_due_reminders_task() == {"sent": 0, "failed": 0, "retrying": 0, "withdrawn": 0}

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

        assert tasks.dispatch_due_reminders_task() == {"sent": 0, "failed": 0, "retrying": 0, "withdrawn": 0}

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
            "withdrawn": 0,
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
        # Which rows it names is asserted separately, where the join it reads
        # from is the subject.
        assert "SKIP LOCKED" in str(statement.compile(dialect=postgresql.dialect()))
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
        assert tasks.dispatch_due_reminders_task() == {"sent": 2, "failed": 0, "retrying": 0, "withdrawn": 0}

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

        assert result == {"sent": 0, "failed": 0, "retrying": 1, "withdrawn": 0}
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
                "withdrawn": 0,
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
                "withdrawn": 0,
            }

        assert "batch limit" not in caplog.text


class TestASwitchedOffFirmStopsTalkingToItsClients:
    """Deactivating a firm has to reach the mail it is still sending.

    Sign-in refuses one, the API refuses one, and the generation jobs skip one
    — but the reminder jobs swept compliance items and invoices, which a
    switched-off firm keeps, so its clients went on being chased in its name.
    The queue made it worse: a document chase is written fifteen days ahead, so
    even stopping the sweep leaves a fortnight of mail already addressed.
    """

    def make_overdue_invoice(self, db, firm, client, *, due_date):
        invoice = Invoice(
            firm_id=firm.id,
            client_id=client.id,
            invoice_number=f"INV/FY2026-27/{str(client.id)[:4]}",
            issue_date=due_date - timedelta(days=30),
            due_date=due_date,
            status=InvoiceStatus.SENT,
            subtotal_paise=100_000,
            tax_paise=18_000,
            total_paise=118_000,
        )
        db.add(invoice)
        db.flush()
        return invoice

    def test_no_filing_reminder_is_queued_for_a_switched_off_firm(self, db):
        firm = make_firm(db, is_active=False)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")  # offsets [10, 5, 2, 1]
        make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=10))
        db.commit()

        result = tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat())

        assert result == {"queued": 0}
        assert db.scalars(select(Reminder)).all() == []

    def test_the_neighbour_in_the_same_run_is_unaffected(self, db):
        """The sweep is global, so refusing one firm must not refuse the rest."""
        off = make_firm(db, name="Closed Books", is_active=False)
        on = make_firm(db, name="Sharma Associates")
        ctype = get_type(db, "GSTR3B_MONTHLY")
        for firm in (off, on):
            client = make_client(db, firm, email=f"{firm.name[:4].lower()}@nimbus.in")
            make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=10))
        db.commit()

        assert tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat()) == {
            "queued": 1
        }

        queued = db.scalars(select(Reminder)).one()
        assert queued.firm_id == on.id

    def test_no_payment_chase_is_queued_for_a_switched_off_firm(self, db):
        """The debt is still owed; chasing it in the firm's name is not ours."""
        firm = make_firm(db, is_active=False)
        client = make_client(db, firm)
        self.make_overdue_invoice(db, firm, client, due_date=RUN_DATE - timedelta(days=7))
        db.commit()

        assert tasks.queue_payment_reminders_task(today=RUN_DATE.isoformat()) == {"queued": 0}

    def test_the_document_sweep_never_looks_at_a_switched_off_firm(self, db, monkeypatch):
        """Asked at the firm level, before any item of theirs is read."""
        off = make_firm(db, name="Closed Books", is_active=False)
        on = make_firm(db, name="Sharma Associates")
        ctype = get_type(db, "GSTR3B_MONTHLY")
        for firm in (off, on):
            client = make_client(db, firm, email=f"{firm.name[:4].lower()}@nimbus.in")
            make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=10))
        db.commit()

        swept = []
        monkeypatch.setattr(
            reminder_service.documents,
            "items_awaiting_documents",
            lambda db, firm_id, **kwargs: swept.append(firm_id) or [],
        )

        tasks.queue_document_reminders_task(today=RUN_DATE.isoformat())

        assert swept == [on.id]

    def test_mail_already_queued_is_not_sent(self, db):
        """The fortnight of messages written before the switch-off."""
        firm = make_firm(db, is_active=False)
        client = make_client(db, firm)
        make_reminder(db, firm, client, scheduled_for=datetime.now(UTC) - timedelta(hours=1))
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {"sent": 0, "failed": 0, "retrying": 0, "withdrawn": 0}

    def test_held_mail_is_kept_scheduled_rather_than_failed(self, db):
        """Suspension is a state a firm comes back from.

        Marking the queue ``FAILED`` would be a decision nothing reverses:
        reactivating the firm would leave every chase it had lined up dead.
        """
        firm = make_firm(db, is_active=False)
        client = make_client(db, firm)
        make_reminder(db, firm, client, scheduled_for=datetime.now(UTC) - timedelta(hours=1))
        db.commit()

        tasks.dispatch_due_reminders_task()

        db.expire_all()
        held = db.scalars(select(Reminder)).one()
        assert held.status is ReminderStatus.SCHEDULED
        assert held.attempt_count == 0
        assert held.error_message is None

    def test_switching_the_firm_back_on_resumes_the_queue(self, db):
        firm = make_firm(db, is_active=False)
        client = make_client(db, firm)
        make_reminder(db, firm, client, scheduled_for=datetime.now(UTC) - timedelta(hours=1))
        db.commit()
        tasks.dispatch_due_reminders_task()

        firm.is_active = True
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {"sent": 1, "failed": 0, "retrying": 0, "withdrawn": 0}

    def test_held_mail_is_not_reported_as_a_backlog(self, db, caplog):
        """A firm on hold is not work the next run is expected to clear.

        Counted as overflow, a single suspended firm would put a backlog
        warning in the log on every run for as long as it stayed suspended.
        """
        off = make_firm(db, name="Closed Books", is_active=False)
        on = make_firm(db, name="Sharma Associates")
        past = datetime.now(UTC) - timedelta(hours=1)
        make_reminder(db, off, make_client(db, off, email="a@closed.in"), scheduled_for=past)
        make_reminder(db, on, make_client(db, on, email="b@sharma.in"), scheduled_for=past)
        db.commit()

        with caplog.at_level(logging.WARNING, logger="app.worker.tasks"):
            assert tasks.dispatch_due_reminders_task(limit=1) == {
                "sent": 1,
                "failed": 0,
                "retrying": 0,
                "withdrawn": 0,
            }

        assert "batch limit" not in caplog.text

    def test_a_deactivated_client_is_not_mailed_either(self, db):
        """Queue-time checked that the client was active. Fifteen days ago."""
        firm = make_firm(db)
        client = make_client(db, firm, is_active=False)
        make_reminder(db, firm, client, scheduled_for=datetime.now(UTC) - timedelta(hours=1))
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {"sent": 0, "failed": 0, "retrying": 0, "withdrawn": 0}

        db.expire_all()
        assert db.scalars(select(Reminder)).one().status is ReminderStatus.SCHEDULED

    def test_the_claim_locks_the_reminder_and_not_the_firm(self):
        """``FOR UPDATE`` over a join takes every table in it.

        The firm row is joined only to be read, and it is the same row a plan
        limit and an invoice number take for their own ordering — held for the
        length of an SMTP handshake, one dispatch would stall the firm's own
        practitioners. SQLite renders no locking clause, so this is only
        readable on the dialect that runs in production.
        """
        recorder = _StatementRecorder()

        tasks._claim_next_due(recorder, cutoff=datetime.now(UTC), exclude=set())

        sql = str(recorder.statements[0].compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE OF reminders SKIP LOCKED" in sql
        assert "JOIN firms" in sql


class TestADocumentChaseDoesNotSilenceTheFilingReminder:
    """Two different messages about one filing, on the same day.

    "We still need your bank statement" and "your GSTR-3B is due on the 20th"
    answer different questions, and both offset lists are configured
    independently — the defaults overlap at ten, five and two days out. The
    filing job matched any reminder carrying that offset, so whichever job beat
    it to the item silenced it, and the client was asked for documents without
    ever being told the deadline.
    """

    def queue_filing_reminder(self, db, *, run_date=RUN_DATE) -> int:
        return tasks.schedule_compliance_reminders_task(today=run_date.isoformat())["queued"]

    def setup_item(self, db, *, days_out=10):
        firm = make_firm(db)
        client = make_client(db, firm)
        ctype = get_type(db, "GSTR3B_MONTHLY")  # offsets [10, 5, 2, 1]
        item = make_item(db, firm, client, ctype, due_date=RUN_DATE + timedelta(days=days_out))
        return firm, client, item

    def test_a_document_chase_at_the_same_offset_does_not_suppress_it(self, db):
        firm, client, item = self.setup_item(db)
        make_reminder(
            db,
            firm,
            client,
            scheduled_for=datetime.now(UTC),
            compliance_item_id=item.id,
            reminder_type=ReminderType.DOCUMENT,
            extra={"kind": "document", "offset_days": 10},
        )
        db.commit()

        assert self.queue_filing_reminder(db) == 1

    def test_the_filing_reminder_still_only_fires_once(self, db):
        """The kind is added to the check, not swapped in for the offset."""
        self.setup_item(db)
        db.commit()

        assert self.queue_filing_reminder(db) == 1
        assert self.queue_filing_reminder(db) == 0

    def test_a_row_queued_before_the_kind_was_recorded_still_counts(self, db):
        """Filing reminders were the first kind and wrote no ``kind`` at all.

        Read as a different kind, every one already in a live queue would be
        sent a second time on the first run after this deploys.
        """
        firm, client, item = self.setup_item(db)
        make_reminder(
            db,
            firm,
            client,
            scheduled_for=datetime.now(UTC),
            compliance_item_id=item.id,
            extra={"offset_days": 10},
        )
        db.commit()

        assert self.queue_filing_reminder(db) == 0

    def test_a_filing_reminder_does_not_suppress_the_document_chase(self, db):
        """The other direction, which already held — and has to keep holding."""
        firm, client, item = self.setup_item(db)
        make_reminder(
            db,
            firm,
            client,
            scheduled_for=datetime.now(UTC),
            compliance_item_id=item.id,
            extra={"kind": "filing", "offset_days": 10},
        )
        db.commit()

        existing = list(db.scalars(select(Reminder)).all())
        assert reminder_service.already_queued(existing, "document", 10) is False

    def test_a_new_filing_reminder_records_its_kind(self, db):
        """So the next kind added does not have to guess at these rows too."""
        self.setup_item(db)
        db.commit()

        self.queue_filing_reminder(db)

        assert db.scalars(select(Reminder)).one().extra["kind"] == "filing"


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


class TestTheWordingNeverCostsTheRunItsReminders:
    """A queueing run has ten minutes and drafts one message per reminder.

    Each draft is a blocking HTTP call that can take the OpenRouter timeout
    twice — the model, then the fallback. A firm's filings cluster on one
    offset day, because every GST client is due on the 20th, so a run drafts
    as many messages as the firm has clients rather than a handful.

    Nothing bounded that, and the run is a single transaction, so a firm large
    enough had the task killed at the soft limit and every reminder it had
    already built rolled back. No rows, no error the firm would ever see, and
    ``days_left in offsets`` matches one day exactly — so that reminder was not
    late, it was never sent.
    """

    def _firm_with_clients(self, db, count: int):
        firm = make_firm(db)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        due = RUN_DATE + timedelta(days=ctype.reminder_offsets_days[0])
        for index in range(count):
            person = make_client(
                db, firm, name=f"Client {index}", email=f"c{index}@nimbus.in"
            )
            make_item(
                db, firm, person, ctype, due_date=due, period_label=f"2026-{index:02d}"
            )
        db.commit()
        return firm

    @pytest.fixture
    def refuse_every_model_call(self, monkeypatch):
        """A configured key, and a transport that fails the test if dialled."""
        monkeypatch.setattr(ai.settings, "openrouter_api_key", "sk-or-test")

        def forbidden(*args, **kwargs):
            raise AssertionError("the run reached for the model past its allowance")

        monkeypatch.setattr(ai.httpx, "post", forbidden)

    def test_an_exhausted_allowance_still_queues_every_reminder(
        self, db, monkeypatch, no_llm, refuse_every_model_call
    ):
        # The real drafting, not the module stub, so the allowance is what
        # decides — otherwise this would pass with no budget at all.
        monkeypatch.setattr(tasks, "draft_client_message", ai.draft_client_message)
        monkeypatch.setattr(ai.settings, "ai_draft_budget_seconds", 0)
        self._firm_with_clients(db, 5)

        result = tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat())

        assert result["queued"] == 5
        db.rollback()
        queued = db.scalars(select(Reminder)).all()
        assert len(queued) == 5
        # Plainly worded, and still carrying what the client has to act on.
        for reminder in queued:
            assert reminder.body.startswith("Dear Client")
            assert "GSTR-3B" in reminder.subject

    def test_the_document_chase_is_bounded_the_same_way(
        self, db, monkeypatch, refuse_every_model_call
    ):
        monkeypatch.setattr(ai.settings, "ai_draft_budget_seconds", 0)
        firm = make_firm(db)
        ctype = get_type(db, "GSTR3B_MONTHLY")
        for index in range(3):
            person = make_client(
                db, firm, name=f"Client {index}", email=f"d{index}@nimbus.in"
            )
            make_item(
                db,
                firm,
                person,
                ctype,
                due_date=RUN_DATE + timedelta(days=max(settings.document_reminder_offsets)),
                period_label=f"2026-{index:02d}",
            )
        db.commit()

        result = tasks.queue_document_reminders_task(today=RUN_DATE.isoformat())

        assert result["queued"] == 3

    def test_the_payment_chase_is_bounded_the_same_way(
        self, db, monkeypatch, refuse_every_model_call
    ):
        monkeypatch.setattr(ai.settings, "ai_draft_budget_seconds", 0)
        firm = make_firm(db)
        person = make_client(db, firm)
        db.add(
            Invoice(
                firm_id=firm.id,
                client_id=person.id,
                invoice_number="INV/FY2026-27/0001",
                issue_date=RUN_DATE - timedelta(days=30),
                due_date=RUN_DATE,
                subtotal_paise=100_000,
                tax_paise=18_000,
                total_paise=118_000,
                status=InvoiceStatus.SENT,
            )
        )
        db.commit()

        result = tasks.queue_payment_reminders_task(today=RUN_DATE.isoformat())

        assert result["queued"] == 1

    def test_the_run_reports_how_the_allowance_went(
        self, db, monkeypatch, no_llm, refuse_every_model_call, caplog
    ):
        """The log line is the only place the change in wording is visible.

        Falling back is not a failure and must not read as one, but a run that
        drafted nothing itself is worth being able to see — it is the symptom
        of a provider that has stopped answering.
        """
        monkeypatch.setattr(tasks, "draft_client_message", ai.draft_client_message)
        monkeypatch.setattr(ai.settings, "ai_draft_budget_seconds", 0)
        self._firm_with_clients(db, 2)

        with caplog.at_level(logging.INFO, logger="app.worker.tasks"):
            tasks.schedule_compliance_reminders_task(today=RUN_DATE.isoformat())

        assert "0 message(s) drafted by the model, 2 from the template" in caplog.text


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
        make_item(db, firm, overdue_client, ctype, due_date=clock.today() - timedelta(days=3))
        make_item(db, firm, on_time, ctype, due_date=clock.today() + timedelta(days=30))
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
            due_date=clock.today() - timedelta(days=3),
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


class TestAChaseThatNoLongerStands:
    """A reminder is queued because something was outstanding *then*, and sent
    later. The sweeps queue at 07:00 IST for 09:00 IST, and a message held for
    a greylisting relay waits longer still.

    Nothing re-read the reason in between, so the queue went out on a fact
    hours to days stale: the client sent the bank statement and the return was
    filed at half past eight, and at nine they were emailed to ask for it; they
    paid the invoice and were then chased for a balance they no longer owed.
    """

    def _document_chase(self, db, *, item_status=ComplianceStatus.PENDING):
        firm = make_firm(db)
        client = make_client(db, firm)
        item = make_item(
            db,
            firm,
            client,
            get_type(db, "GSTR3B_MONTHLY"),
            due_date=clock.today() + timedelta(days=10),
            status=item_status,
        )
        reminder = make_reminder(
            db,
            firm,
            client,
            scheduled_for=datetime.now(UTC) - timedelta(hours=1),
            compliance_item_id=item.id,
            reminder_type=ReminderType.DOCUMENT,
            subject="We still need your bank statement",
            extra={"kind": "document", "offset_days": 10},
        )
        return firm, client, item, reminder

    def _sent_invoice(self, db, firm, client, *, paid=0, status=InvoiceStatus.SENT):
        invoice = Invoice(
            firm_id=firm.id,
            client_id=client.id,
            invoice_number="INV/FY2026-27/0001",
            issue_date=clock.today() - timedelta(days=40),
            due_date=clock.today() - timedelta(days=10),
            subtotal_paise=200_000,
            tax_paise=36_000,
            total_paise=236_000,
            amount_paid_paise=paid,
            status=status,
        )
        db.add(invoice)
        db.flush()
        return invoice

    def _payment_chase(self, db, firm, client, invoice):
        return make_reminder(
            db,
            firm,
            client,
            scheduled_for=datetime.now(UTC) - timedelta(hours=1),
            invoice_id=invoice.id,
            reminder_type=ReminderType.PAYMENT,
            subject=f"Invoice {invoice.invoice_number} — ₹2,360.00 outstanding",
            extra={"kind": "payment", "offset_days": 10},
        )

    # ------------------------------------------------------------- filings --

    def test_a_document_chase_for_a_filed_return_is_not_sent(self, db):
        _, _, item, _ = self._document_chase(db)
        item.status = ComplianceStatus.FILED
        item.filed_on = clock.today()
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {
            "sent": 0,
            "failed": 0,
            "retrying": 0,
            "withdrawn": 1,
        }

    def test_the_withdrawal_is_recorded_on_the_reminder(self, db):
        _, _, item, reminder = self._document_chase(db)
        item.status = ComplianceStatus.FILED
        item.filed_on = clock.today()
        db.commit()
        tasks.dispatch_due_reminders_task()

        db.expire_all()
        row = db.get(Reminder, reminder.id)
        assert row.status is ReminderStatus.CANCELLED
        assert "filed" in row.extra["withdrawn_because"]
        # No attempt was made, so none is counted.
        assert row.attempt_count == 0
        assert row.sent_at is None

    def test_a_late_filing_counts_as_filed(self, db):
        _, _, item, _ = self._document_chase(db)
        item.status = ComplianceStatus.DELAYED_FILED
        item.filed_on = clock.today()
        db.commit()

        assert tasks.dispatch_due_reminders_task()["withdrawn"] == 1

    def test_a_filing_the_client_no_longer_owes_is_not_chased(self, db):
        """Surrendering a registration closes the item and cancels the task
        raised for it; the queued mail was the one thing left going out."""
        _, _, item, reminder = self._document_chase(db)
        item.status = ComplianceStatus.NOT_APPLICABLE
        db.commit()
        tasks.dispatch_due_reminders_task()

        db.expire_all()
        assert "no longer one this client owes" in db.get(Reminder, reminder.id).extra[
            "withdrawn_because"
        ]

    def test_a_filing_reminder_is_held_to_the_same_test(self, db):
        firm, client, item, _ = self._document_chase(db)
        reminder = make_reminder(
            db,
            firm,
            client,
            scheduled_for=datetime.now(UTC) - timedelta(hours=1),
            compliance_item_id=item.id,
            reminder_type=ReminderType.FILING,
            extra={"kind": "filing", "offset_days": 10},
        )
        item.status = ComplianceStatus.FILED
        db.commit()
        tasks.dispatch_due_reminders_task()

        db.expire_all()
        assert db.get(Reminder, reminder.id).status is ReminderStatus.CANCELLED

    def test_a_row_queued_before_the_kind_was_recorded_is_still_checked(self, db):
        """Filing reminders predate the key, so a row without one is one."""
        firm, client, item, _ = self._document_chase(db)
        reminder = make_reminder(
            db,
            firm,
            client,
            scheduled_for=datetime.now(UTC) - timedelta(hours=1),
            compliance_item_id=item.id,
            reminder_type=ReminderType.FILING,
            extra={"offset_days": 10},
        )
        item.status = ComplianceStatus.FILED
        db.commit()
        tasks.dispatch_due_reminders_task()

        db.expire_all()
        assert db.get(Reminder, reminder.id).status is ReminderStatus.CANCELLED

    def test_an_open_filing_is_still_chased(self, db):
        """The point is not to stop chasing — it is to stop chasing the done."""
        self._document_chase(db)
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {
            "sent": 1,
            "failed": 0,
            "retrying": 0,
            "withdrawn": 0,
        }

    def test_work_in_progress_is_still_chased(self, db):
        self._document_chase(db, item_status=ComplianceStatus.IN_PROGRESS)
        db.commit()

        assert tasks.dispatch_due_reminders_task()["sent"] == 1

    # -------------------------------------------------------------- money --

    def test_a_settled_invoice_is_not_chased(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        invoice = self._sent_invoice(db, firm, client, paid=236_000, status=InvoiceStatus.PAID)
        reminder = self._payment_chase(db, firm, client, invoice)
        db.commit()

        assert tasks.dispatch_due_reminders_task()["withdrawn"] == 1
        db.expire_all()
        assert "settled in full" in db.get(Reminder, reminder.id).extra["withdrawn_because"]

    def test_a_cancelled_invoice_is_not_chased(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        invoice = self._sent_invoice(db, firm, client, status=InvoiceStatus.CANCELLED)
        reminder = self._payment_chase(db, firm, client, invoice)
        db.commit()

        assert tasks.dispatch_due_reminders_task()["withdrawn"] == 1
        db.expire_all()
        assert "cancelled" in db.get(Reminder, reminder.id).extra["withdrawn_because"]

    def test_a_part_payment_does_not_stop_the_chase(self, db):
        """Something is still outstanding, and the message quotes the balance."""
        firm = make_firm(db)
        client = make_client(db, firm)
        invoice = self._sent_invoice(
            db, firm, client, paid=100_000, status=InvoiceStatus.PARTIALLY_PAID
        )
        self._payment_chase(db, firm, client, invoice)
        db.commit()

        assert tasks.dispatch_due_reminders_task()["sent"] == 1

    def test_an_unpaid_invoice_is_still_chased(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        invoice = self._sent_invoice(db, firm, client, status=InvoiceStatus.OVERDUE)
        self._payment_chase(db, firm, client, invoice)
        db.commit()

        assert tasks.dispatch_due_reminders_task()["sent"] == 1

    # ------------------------------------------------------------- manual --

    def test_a_message_a_practitioner_composed_is_never_second_guessed(self, db):
        """A filing confirmation is *about* a filed return, and quotes the
        acknowledgement number back. Withdrawing it would silence the one
        message the client is waiting for."""
        firm, client, item, _ = self._document_chase(db)
        reminder = reminder_service.build_manual_reminder(
            db,
            client=client,
            reminder_type=ReminderType.FILING,
            subject="Your GSTR-3B has been filed",
            body="Acknowledgement number AA2707260012345.",
            scheduled_for=datetime.now(UTC) - timedelta(hours=1),
            compliance_item_id=item.id,
        )
        item.status = ComplianceStatus.FILED
        db.commit()

        assert tasks.dispatch_due_reminders_task()["sent"] == 1
        db.expire_all()
        assert db.get(Reminder, reminder.id).status is ReminderStatus.SENT

    # -------------------------------------------------------- the paperwork --

    def _attach(self, db, item, requirement: str) -> Document:
        """The client answering one checklist row, the way the portal does."""
        document = Document(
            firm_id=item.firm_id,
            client_id=item.client_id,
            compliance_item_id=item.id,
            original_filename=f"{requirement}.pdf",
            storage_path=f"{item.firm_id}/{item.client_id}/{requirement}.pdf",
            content_type="application/pdf",
            size_bytes=len(PDF_BYTES),
            checksum_sha256="0" * 64,
            category=DocumentCategory(requirement),
            uploaded_via_portal=True,
            satisfies_requirements=[],
        )
        db.add(document)
        db.flush()
        return document

    def _answer_the_whole_checklist(self, db, item) -> None:
        for requirement in item.compliance_type.required_documents:
            self._attach(db, item, requirement)

    def test_a_document_chase_is_withdrawn_once_the_documents_arrive(self, db):
        """The ordinary case, and the one the queue cannot see.

        The sweep asks at 07:00 for 09:00 and the portal is open in between.
        The filing itself is not touched until the practitioner sits down to it
        that afternoon, so the already-filed test catches none of this: what
        went out was the firm asking its own client, by name, for paperwork it
        was already holding and could see in the portal.
        """
        _, _, item, reminder = self._document_chase(db)
        self._answer_the_whole_checklist(db, item)
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {
            "sent": 0,
            "failed": 0,
            "retrying": 0,
            "withdrawn": 1,
        }
        db.expire_all()
        row = db.get(Reminder, reminder.id)
        assert row.status is ReminderStatus.CANCELLED
        assert "has since arrived" in row.extra["withdrawn_because"]
        assert row.attempt_count == 0

    def test_a_document_still_missing_keeps_the_chase_going(self, db):
        """Withdrawal is for a question that has been answered, not one that
        has been partly answered — the rest of the list is still wanted."""
        _, _, item, _ = self._document_chase(db)
        requirements = item.compliance_type.required_documents
        assert len(requirements) > 1, "this test needs a list to leave a gap in"
        for requirement in requirements[:-1]:
            self._attach(db, item, requirement)
        db.commit()

        assert tasks.dispatch_due_reminders_task()["sent"] == 1

    def test_a_chase_whose_list_is_still_untouched_goes_out(self, db):
        _, _, item, _ = self._document_chase(db)
        db.commit()

        assert tasks.dispatch_due_reminders_task()["sent"] == 1

    def test_the_documents_arriving_does_not_silence_the_deadline_reminder(self, db):
        """A filing reminder is about the due date, not about the list.

        Holding the paperwork is precisely when the client most needs telling
        the return is due — the firm can now file it, and the deadline has not
        moved an inch.
        """
        firm, client, item, _ = self._document_chase(db)
        reminder = make_reminder(
            db,
            firm,
            client,
            scheduled_for=datetime.now(UTC) - timedelta(hours=1),
            compliance_item_id=item.id,
            reminder_type=ReminderType.FILING,
            extra={"kind": "filing", "offset_days": 10},
        )
        self._answer_the_whole_checklist(db, item)
        db.commit()

        tasks.dispatch_due_reminders_task()

        db.expire_all()
        assert db.get(Reminder, reminder.id).status is ReminderStatus.SENT

    def test_a_document_a_practitioner_uploaded_answers_the_row_too(self, db):
        """Who sent it is not the question. A CA who receives the statement by
        email and files it against the item is holding it just the same."""
        _, _, item, _ = self._document_chase(db)
        for requirement in item.compliance_type.required_documents:
            document = self._attach(db, item, requirement)
            document.uploaded_via_portal = False
        db.commit()

        assert tasks.dispatch_due_reminders_task()["withdrawn"] == 1

    def test_an_explicit_requirement_answers_the_row_without_the_category(self, db):
        """``satisfies_requirements`` is the other half of the checklist, and
        the half a corrected category leaves behind."""
        _, _, item, _ = self._document_chase(db)
        for requirement in item.compliance_type.required_documents:
            document = self._attach(db, item, requirement)
            document.category = DocumentCategory.OTHER
            document.satisfies_requirements = [requirement]
        db.commit()

        assert tasks.dispatch_due_reminders_task()["withdrawn"] == 1

    def test_a_message_a_practitioner_composed_is_not_withdrawn_by_an_upload(self, db):
        """Same standing as a filing confirmation: what a practitioner wrote is
        theirs, and a document arriving is not this code's cue to bin it."""
        _, client, item, _ = self._document_chase(db)
        reminder = reminder_service.build_manual_reminder(
            db,
            client=client,
            reminder_type=ReminderType.DOCUMENT,
            subject="One more thing on the June return",
            body="Could you confirm the closing stock figure?",
            scheduled_for=datetime.now(UTC) - timedelta(hours=1),
            compliance_item_id=item.id,
        )
        self._answer_the_whole_checklist(db, item)
        db.commit()

        # The automated chase beside it is withdrawn; this one is not.
        assert tasks.dispatch_due_reminders_task() == {
            "sent": 1,
            "failed": 0,
            "retrying": 0,
            "withdrawn": 1,
        }
        db.expire_all()
        assert db.get(Reminder, reminder.id).status is ReminderStatus.SENT

    def test_a_chase_naming_nothing_goes_out_as_before(self, db):
        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(
            db, firm, client, scheduled_for=datetime.now(UTC) - timedelta(hours=1)
        )
        db.commit()

        assert tasks.dispatch_due_reminders_task()["sent"] == 1

    # ------------------------------------------------------ the batch loop --

    def test_a_withdrawal_does_not_stop_the_rest_of_the_batch(self, db):
        firm, client, item, _ = self._document_chase(db)
        item.status = ComplianceStatus.FILED
        make_reminder(
            db, firm, client, scheduled_for=datetime.now(UTC) - timedelta(hours=2)
        )
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {
            "sent": 1,
            "failed": 0,
            "retrying": 0,
            "withdrawn": 1,
        }

    def test_a_withdrawn_reminder_is_not_reconsidered_next_run(self, db):
        _, _, item, _ = self._document_chase(db)
        item.status = ComplianceStatus.FILED
        db.commit()
        tasks.dispatch_due_reminders_task()

        assert tasks.dispatch_due_reminders_task() == {
            "sent": 0,
            "failed": 0,
            "retrying": 0,
            "withdrawn": 0,
        }
