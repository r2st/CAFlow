"""Tests for SMTP delivery and the reminder dispatcher's retry behaviour.

No real mail server is involved: :func:`smtplib.SMTP` is replaced by a fake
that records what it was handed, or raises the SMTP exception under test.
"""

from __future__ import annotations

import smtplib
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.base import (
    EntityType,
    ReminderChannel,
    ReminderStatus,
    ReminderType,
)
from app.models.client import Client
from app.models.firm import Firm
from app.models.reminder import Reminder
from app.services import delivery, mailer
from app.worker import tasks

# ------------------------------------------------------------------ factories --


def make_firm(db, **kwargs) -> Firm:
    firm = Firm(
        name=kwargs.pop("name", "Sharma & Associates"),
        email=kwargs.pop("email", "office@sharma.in"),
        is_active=True,
        **kwargs,
    )
    db.add(firm)
    db.flush()
    return firm


def make_client(db, firm, *, email="accounts@nimbus.in", **kwargs) -> Client:
    client = Client(
        firm_id=firm.id,
        name=kwargs.pop("name", "Nimbus Textiles"),
        entity_type=EntityType.PRIVATE_LIMITED,
        email=email,
        is_active=True,
        onboarded_on=date(2025, 4, 1),
        **kwargs,
    )
    db.add(client)
    db.flush()
    return client


def make_reminder(db, firm, client, **kwargs) -> Reminder:
    reminder = Reminder(
        firm_id=firm.id,
        client_id=client.id,
        reminder_type=kwargs.pop("reminder_type", ReminderType.DOCUMENT),
        channel=kwargs.pop("channel", ReminderChannel.EMAIL),
        status=kwargs.pop("status", ReminderStatus.SCHEDULED),
        subject=kwargs.pop("subject", "2 document(s) still needed for GSTR-3B"),
        body=kwargs.pop("body", "Please send your bank statement."),
        recipient=kwargs.pop("recipient", "accounts@nimbus.in"),
        scheduled_for=kwargs.pop("scheduled_for", datetime.now(UTC) - timedelta(hours=1)),
        **kwargs,
    )
    db.add(reminder)
    db.flush()
    return reminder


# ----------------------------------------------------------------- fake SMTP --


class FakeSMTP:
    """Stands in for ``smtplib.SMTP``; records sends, or raises on demand."""

    instances: list[FakeSMTP] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.messages = []
        self.raise_on_send: Exception | None = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.messages.append(message)


@pytest.fixture
def smtp(monkeypatch):
    """Configure SMTP settings and swap in the fake transport."""
    FakeSMTP.instances = []
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.in")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_username", "caflow")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    monkeypatch.setattr(settings, "smtp_use_tls", True)
    monkeypatch.setattr(settings, "smtp_use_ssl", False)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


# ------------------------------------------------------------------- mailer --


class TestMailer:
    def test_a_configured_host_sends_over_smtp(self, smtp):
        result = mailer.send(
            to="accounts@nimbus.in",
            subject="Documents needed",
            body="Please send your bank statement.",
            reply_to="office@sharma.in",
            from_name="Sharma & Associates",
        )

        assert result.transport == "smtp"
        sent = smtp.instances[0]
        assert sent.started_tls is True
        assert sent.login_args == ("caflow", "secret")

        message = sent.messages[0]
        assert message["To"] == "accounts@nimbus.in"
        assert message["Subject"] == "Documents needed"
        assert message["Reply-To"] == "office@sharma.in"
        assert "Sharma & Associates" in message["From"]
        assert "bank statement" in message.get_content()

    def test_no_host_configured_logs_instead_of_sending(self, monkeypatch):
        """Development and CI must not need a mail server."""
        monkeypatch.setattr(settings, "smtp_host", "")

        result = mailer.send(to="accounts@nimbus.in", subject="Hi", body="There")

        assert result.transport == "log"

    def test_implicit_tls_skips_starttls(self, monkeypatch):
        recorded = {}

        class FakeSSL(FakeSMTP):
            def __init__(self, host, port, timeout=None):
                super().__init__(host, port, timeout)
                recorded["port"] = port

        monkeypatch.setattr(settings, "smtp_host", "smtp.example.in")
        monkeypatch.setattr(settings, "smtp_port", 465)
        monkeypatch.setattr(settings, "smtp_use_ssl", True)
        monkeypatch.setattr(settings, "smtp_username", "")
        monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)

        result = mailer.send(to="accounts@nimbus.in", subject="Hi", body="There")

        assert result.transport == "smtp"
        assert recorded["port"] == 465

    @pytest.mark.parametrize("address", ["", "   ", "not-an-address", "a@b", "a@@b.in"])
    def test_an_unusable_address_fails_permanently(self, address, smtp):
        with pytest.raises(mailer.DeliveryError) as exc:
            mailer.send(to=address, subject="Hi", body="There")
        assert exc.value.permanent is True

    def test_a_refused_recipient_is_permanent(self, monkeypatch):
        class Refusing(FakeSMTP):
            def send_message(self, message):
                raise smtplib.SMTPRecipientsRefused({})

        monkeypatch.setattr(settings, "smtp_host", "smtp.example.in")
        monkeypatch.setattr(settings, "smtp_username", "")
        monkeypatch.setattr(smtplib, "SMTP", Refusing)

        with pytest.raises(mailer.DeliveryError) as exc:
            mailer.send(to="accounts@nimbus.in", subject="Hi", body="There")
        assert exc.value.permanent is True

    def test_a_connection_problem_is_retryable(self, monkeypatch):
        class Broken(FakeSMTP):
            def send_message(self, message):
                raise smtplib.SMTPServerDisconnected("connection reset")

        monkeypatch.setattr(settings, "smtp_host", "smtp.example.in")
        monkeypatch.setattr(settings, "smtp_username", "")
        monkeypatch.setattr(smtplib, "SMTP", Broken)

        with pytest.raises(mailer.DeliveryError) as exc:
            mailer.send(to="accounts@nimbus.in", subject="Hi", body="There")
        assert exc.value.permanent is False

    def test_bad_credentials_do_not_burn_retries(self, monkeypatch):
        class NoAuth(FakeSMTP):
            def login(self, username, password):
                raise smtplib.SMTPAuthenticationError(535, b"bad password")

        monkeypatch.setattr(settings, "smtp_host", "smtp.example.in")
        monkeypatch.setattr(settings, "smtp_username", "caflow")
        monkeypatch.setattr(smtplib, "SMTP", NoAuth)

        with pytest.raises(mailer.DeliveryError) as exc:
            mailer.send(to="accounts@nimbus.in", subject="Hi", body="There")
        assert exc.value.permanent is True


# ----------------------------------------------------------------- delivery --


class TestChannelRouting:
    def test_an_email_reminder_goes_to_its_recipient(self, db, smtp):
        firm = make_firm(db)
        client = make_client(db, firm)
        reminder = make_reminder(db, firm, client)
        db.commit()

        outcome = delivery.deliver(reminder, firm)

        assert outcome.delivered is True
        assert outcome.transport == "smtp"
        assert reminder.extra["transport"] == "smtp"
        assert smtp.instances[0].messages[0]["Reply-To"] == "office@sharma.in"

    def test_a_whatsapp_reminder_falls_back_to_email(self, db, smtp):
        """No WhatsApp adapter exists, so mail the client rather than no-op."""
        firm = make_firm(db)
        client = make_client(db, firm, whatsapp="+919876543210")
        reminder = make_reminder(
            db,
            firm,
            client,
            channel=ReminderChannel.WHATSAPP,
            recipient="+919876543210",
        )
        db.commit()

        outcome = delivery.deliver(reminder, firm)

        assert outcome.delivered is True
        assert smtp.instances[0].messages[0]["To"] == "accounts@nimbus.in"
        # The record says what really happened.
        assert reminder.extra["substituted_channel"] == "whatsapp"

    def test_a_whatsapp_only_client_with_no_email_has_no_transport(self, db, smtp):
        firm = make_firm(db)
        client = make_client(db, firm, email=None, whatsapp="+919876543210")
        reminder = make_reminder(
            db,
            firm,
            client,
            channel=ReminderChannel.WHATSAPP,
            recipient="+919876543210",
        )
        db.commit()

        with pytest.raises(delivery.NoTransport):
            delivery.deliver(reminder, firm)


# --------------------------------------------------- dispatcher integration --


class TestDispatcherDelivery:
    def test_a_due_reminder_is_actually_mailed(self, db, smtp):
        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(db, firm, client)
        db.commit()

        result = tasks.dispatch_due_reminders_task()

        assert result == {"sent": 1, "failed": 0, "retrying": 0}
        assert len(smtp.instances[0].messages) == 1
        db.expire_all()
        reminder = db.scalars(select(Reminder)).one()
        assert reminder.status is ReminderStatus.SENT
        assert reminder.sent_at is not None

    def test_a_transient_failure_stays_scheduled_for_the_next_run(self, db, monkeypatch):
        class Broken(FakeSMTP):
            def send_message(self, message):
                raise smtplib.SMTPServerDisconnected("connection reset")

        monkeypatch.setattr(settings, "smtp_host", "smtp.example.in")
        monkeypatch.setattr(settings, "smtp_username", "")
        monkeypatch.setattr(smtplib, "SMTP", Broken)

        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(db, firm, client)
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {
            "sent": 0,
            "failed": 0,
            "retrying": 1,
        }

        db.expire_all()
        reminder = db.scalars(select(Reminder)).one()
        assert reminder.status is ReminderStatus.SCHEDULED
        assert reminder.attempt_count == 1
        assert "connection reset" in reminder.error_message

    def test_retries_stop_at_the_configured_limit(self, db, monkeypatch):
        class Broken(FakeSMTP):
            def send_message(self, message):
                raise smtplib.SMTPServerDisconnected("connection reset")

        monkeypatch.setattr(settings, "smtp_host", "smtp.example.in")
        monkeypatch.setattr(settings, "smtp_username", "")
        monkeypatch.setattr(settings, "reminder_max_attempts", 3)
        monkeypatch.setattr(smtplib, "SMTP", Broken)

        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(db, firm, client)
        db.commit()

        outcomes = [tasks.dispatch_due_reminders_task() for _ in range(3)]

        assert [o["retrying"] for o in outcomes] == [1, 1, 0]
        assert outcomes[-1]["failed"] == 1

        db.expire_all()
        reminder = db.scalars(select(Reminder)).one()
        assert reminder.status is ReminderStatus.FAILED
        assert reminder.attempt_count == 3

    def test_a_permanent_failure_is_not_retried(self, db, monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.in")

        firm = make_firm(db)
        client = make_client(db, firm, email="not-an-address")
        make_reminder(db, firm, client, recipient="not-an-address")
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {
            "sent": 0,
            "failed": 1,
            "retrying": 0,
        }

        db.expire_all()
        reminder = db.scalars(select(Reminder)).one()
        assert reminder.status is ReminderStatus.FAILED
        assert reminder.attempt_count == 1

    def test_a_successful_retry_clears_the_earlier_error(self, db, smtp):
        firm = make_firm(db)
        client = make_client(db, firm)
        make_reminder(db, firm, client, attempt_count=1, error_message="SMTP delivery failed")
        db.commit()

        assert tasks.dispatch_due_reminders_task()["sent"] == 1

        db.expire_all()
        reminder = db.scalars(select(Reminder)).one()
        assert reminder.status is ReminderStatus.SENT
        assert reminder.error_message is None
