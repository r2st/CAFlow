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

        assert result == {"sent": 1, "failed": 0, "retrying": 0, "withdrawn": 0}
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
            "withdrawn": 0,
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
            "withdrawn": 0,
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


class TestAHeaderIsOneLine:
    """A subject or a display name that arrived with a line break in it.

    Inbound sanitising keeps newlines on purpose — a note and an address are
    genuinely multiline — so one reaches here whenever a practitioner pastes a
    subject out of their mail client, or a firm's own name was entered across
    two lines. The standard library refuses to encode a header containing one,
    correctly, and raised a bare ``ValueError`` well outside the two exceptions
    the dispatcher knows how to answer.

    What that cost: the dispatcher reads an unexpected exception as "we do not
    know whether that went out", rolls back and re-raises, so the run stops.
    The offending reminder is still ``SCHEDULED`` holding the oldest time, so
    it is the first row the next run claims, and the one after that. One
    pasted line break stopped every reminder for every firm on the deployment,
    for good, and left nothing behind saying why.
    """

    def test_a_pasted_subject_still_goes_out(self, smtp):
        mailer.send(
            to="accounts@nimbus.in",
            subject="Your GSTR-3B for June\nis due on the 20th",
            body="Please send the bank statement.",
        )

        message = smtp.instances[0].messages[0]
        assert message["Subject"] == "Your GSTR-3B for June is due on the 20th"

    @pytest.mark.parametrize("break_char", ["\n", "\r\n", "\r", "\t", "\x0b", " "])
    def test_every_way_a_line_can_break(self, break_char, smtp):
        mailer.send(to="accounts@nimbus.in", subject=f"One{break_char}Two", body="b")

        assert smtp.instances[0].messages[0]["Subject"] == "One Two"

    def test_a_firm_name_entered_across_two_lines_still_signs_the_mail(self, smtp):
        mailer.send(
            to="accounts@nimbus.in",
            subject="Hi",
            body="There",
            from_name="Sharma & Associates\nChartered Accountants",
        )

        assert (
            "Sharma & Associates Chartered Accountants"
            in smtp.instances[0].messages[0]["From"]
        )

    def test_a_reply_to_is_held_to_the_same_rule(self, smtp):
        mailer.send(
            to="accounts@nimbus.in", subject="Hi", body="There", reply_to="office@sharma.in\n"
        )

        assert smtp.instances[0].messages[0]["Reply-To"] == "office@sharma.in"

    def test_the_body_keeps_its_line_breaks(self, smtp):
        """It is content, not a header. A reminder that reads as one paragraph
        instead of five would be the worse bug."""
        mailer.send(
            to="accounts@nimbus.in",
            subject="Hi",
            body="Dear Nimbus,\n\nPlease send:\n- bank statement\n- sales register",
        )

        assert "\n- bank statement" in smtp.instances[0].messages[0].get_content()

    @pytest.mark.parametrize(
        "address", ["a@b.in\nBcc: someone.else", "a\n@b.in", "a@b .in", "a@b\r.in"]
    )
    def test_a_broken_line_in_an_address_is_refused_not_folded(self, address, smtp):
        """Not the same mistake. An address with a line break in the middle is
        not one a firm mistyped, it is someone writing a second header."""
        with pytest.raises(mailer.DeliveryError) as exc:
            mailer.send(to=address, subject="Hi", body="There")

        assert exc.value.permanent is True
        assert not smtp.instances

    def test_a_message_that_cannot_be_assembled_fails_permanently(self, monkeypatch, smtp):
        """The backstop, for whatever the folding above does not reach.

        Reported as a delivery failure because that is what it is: the message
        certainly did not go out, and nothing about a later attempt would
        assemble it either.
        """
        monkeypatch.setattr(
            mailer,
            "build_message",
            lambda **kwargs: (_ for _ in ()).throw(ValueError("nope")),
        )

        with pytest.raises(mailer.DeliveryError) as exc:
            mailer.send(to="accounts@nimbus.in", subject="Hi", body="There")

        assert exc.value.permanent is True
        assert "could not be assembled" in str(exc.value)


class TestOneBadMessageDoesNotStopTheQueue:
    """The consequence the fix above exists for, checked where it bit."""

    def _due(self, db, firm, client, minutes, **kwargs):
        return make_reminder(
            db,
            firm,
            client,
            scheduled_for=datetime.now(UTC) - timedelta(minutes=minutes),
            **kwargs,
        )

    def test_a_pasted_subject_no_longer_holds_up_the_batch(self, db, smtp):
        firm = make_firm(db)
        client = make_client(db, firm)
        self._due(db, firm, client, 120, subject="Due\non the 20th", body="One")
        self._due(db, firm, client, 60, subject="Ordinary", body="Two")
        db.commit()

        assert tasks.dispatch_due_reminders_task() == {
            "sent": 2,
            "failed": 0,
            "retrying": 0,
            "withdrawn": 0,
        }
        # One connection per send, so count across them.
        assert sum(len(i.messages) for i in smtp.instances) == 2

    def test_an_unsendable_message_fails_alone(self, db, smtp, monkeypatch):
        """Failed, not left scheduled: the run has to be able to move past it.

        Left scheduled it is claimed first again next run — it holds the oldest
        time — and the queue behind it never moves.
        """
        firm = make_firm(db)
        client = make_client(db, firm)
        doomed = self._due(db, firm, client, 120, subject="Hi", body="One")
        healthy = self._due(db, firm, client, 60, subject="Ordinary", body="Two")
        db.commit()

        real_build = mailer.build_message

        def refuse_one(**kwargs):
            if kwargs.get("body") == "One":
                raise ValueError("cannot be assembled")
            return real_build(**kwargs)

        monkeypatch.setattr(mailer, "build_message", refuse_one)

        assert tasks.dispatch_due_reminders_task() == {
            "sent": 1,
            "failed": 1,
            "retrying": 0,
            "withdrawn": 0,
        }

        db.expire_all()
        assert db.get(Reminder, doomed.id).status is ReminderStatus.FAILED
        assert db.get(Reminder, healthy.id).status is ReminderStatus.SENT

    def test_the_reason_is_left_on_the_row(self, db, smtp, monkeypatch):
        firm = make_firm(db)
        client = make_client(db, firm)
        reminder = self._due(db, firm, client, 60, subject="Hi", body="One")
        db.commit()
        monkeypatch.setattr(
            mailer,
            "build_message",
            lambda **kwargs: (_ for _ in ()).throw(ValueError("a bad header")),
        )

        tasks.dispatch_due_reminders_task()

        db.expire_all()
        assert "a bad header" in db.get(Reminder, reminder.id).error_message
