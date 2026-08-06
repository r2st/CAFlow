"""Automated document-collection and payment reminders, plus manual sends."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core import clock
from app.models.base import ReminderChannel, ReminderStatus, ReminderType
from app.models.client import Client
from app.models.reminder import Reminder
from app.services import reminders as reminder_service
from app.worker import tasks as worker_tasks
from tests.conftest import first_item_of_type, paged_order_by

PDF_BYTES = b"%PDF-1.4\n% ledger\n"


def days_before_due(client, auth_headers, item_id: str, days: int) -> date:
    """The run date that puts ``item_id`` exactly ``days`` from its deadline."""
    item = client.get(
        f"/api/v1/compliance/items/{item_id}", headers=auth_headers
    ).json()
    return date.fromisoformat(item["due_date"]) - timedelta(days=days)


class TestChannelSelection:
    def test_whatsapp_wins_when_available(self):
        client = Client(whatsapp="+919900112233", email="a@b.in", phone="+919000000000")
        assert reminder_service.preferred_channel(client) == ReminderChannel.WHATSAPP
        assert reminder_service.recipient_for(client, ReminderChannel.WHATSAPP) == (
            "+919900112233"
        )

    def test_email_is_the_fallback(self):
        client = Client(whatsapp=None, email="a@b.in", phone="+919000000000")
        assert reminder_service.preferred_channel(client) == ReminderChannel.EMAIL

    def test_sms_when_only_a_phone_is_on_file(self):
        client = Client(whatsapp=None, email=None, phone="+919000000000")
        assert reminder_service.preferred_channel(client) == ReminderChannel.SMS
        # And the number is what an SMS is addressed to — the channel choosing
        # itself is worth nothing if the recipient does not follow it.
        assert reminder_service.recipient_for(client, ReminderChannel.SMS) == (
            "+919000000000"
        )

    def test_nine_am_ist_converts_to_utc(self):
        # IST is UTC+5:30, so 09:00 IST is 03:30 UTC the same morning.
        moment = reminder_service.ist_morning(date(2026, 7, 1))
        assert moment == datetime(2026, 7, 1, 3, 30, tzinfo=UTC)


class TestDocumentReminders:
    def test_queues_a_reminder_when_documents_are_missing(
        self, client, auth_headers, client_id, db, firm_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        run_date = days_before_due(client, auth_headers, item["id"], 10)

        queued = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )
        db.commit()

        assert len(queued) >= 1
        reminder = next(r for r in queued if str(r.compliance_item_id) == item["id"])
        assert reminder.reminder_type == ReminderType.DOCUMENT
        assert reminder.status == ReminderStatus.SCHEDULED
        assert reminder.extra["offset_days"] == 10
        assert set(reminder.extra["missing"]) == {
            "sales_invoice",
            "purchase_invoice",
            "bank_statement",
        }

    def test_the_body_names_the_missing_documents(
        self, client, auth_headers, client_id, db, firm_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        run_date = days_before_due(client, auth_headers, item["id"], 10)

        queued = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )
        reminder = next(r for r in queued if str(r.compliance_item_id) == item["id"])
        assert "Bank statement" in reminder.body
        assert "Sales invoice" in reminder.body

    def test_nothing_is_queued_once_every_document_has_arrived(
        self, client, auth_headers, client_id, db, firm_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        for requirement in ("sales_invoice", "purchase_invoice", "bank_statement"):
            client.post(
                "/api/v1/documents/upload",
                files={"file": (f"{requirement}.pdf", io.BytesIO(PDF_BYTES),
                                "application/pdf")},
                data={
                    "client_id": client_id,
                    "compliance_item_id": item["id"],
                    "requirement": requirement,
                },
                headers=auth_headers,
            )
        run_date = days_before_due(client, auth_headers, item["id"], 10)

        queued = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )
        assert item["id"] not in [str(r.compliance_item_id) for r in queued]

    def test_a_partial_upload_still_triggers_a_chase(
        self, client, auth_headers, client_id, db, firm_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        client.post(
            "/api/v1/documents/upload",
            files={"file": ("bank.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
            data={
                "client_id": client_id,
                "compliance_item_id": item["id"],
                "requirement": "bank_statement",
            },
            headers=auth_headers,
        )
        run_date = days_before_due(client, auth_headers, item["id"], 10)

        queued = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )
        reminder = next(r for r in queued if str(r.compliance_item_id) == item["id"])
        assert set(reminder.extra["missing"]) == {"sales_invoice", "purchase_invoice"}

    def test_running_twice_on_the_same_offset_does_not_double_chase(
        self, client, auth_headers, client_id, db, firm_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        run_date = days_before_due(client, auth_headers, item["id"], 10)

        first = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )
        db.commit()
        second = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )
        db.commit()
        assert first
        assert second == []

    def test_each_offset_fires_once_as_the_deadline_approaches(
        self, client, auth_headers, client_id, db, firm_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        offsets = [15, 5]
        for offset in offsets:
            run_date = days_before_due(client, auth_headers, item["id"], offset)
            reminder_service.queue_document_reminders(
                db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=offsets
            )
            db.commit()

        fired = db.scalars(
            select(Reminder).where(
                Reminder.compliance_item_id == uuid.UUID(item["id"]),
                Reminder.reminder_type == ReminderType.DOCUMENT,
            )
        ).all()
        assert sorted(r.extra["offset_days"] for r in fired) == [5, 15]

    def test_a_day_that_is_not_an_offset_queues_nothing(
        self, client, auth_headers, client_id, db, firm_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        run_date = days_before_due(client, auth_headers, item["id"], 9)
        queued = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10, 5]
        )
        assert item["id"] not in [str(r.compliance_item_id) for r in queued]

    def test_a_filed_item_is_never_chased(
        self, client, auth_headers, client_id, db, firm_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        client.patch(
            f"/api/v1/compliance/items/{item['id']}",
            json={"status": "filed"},
            headers=auth_headers,
        )
        run_date = days_before_due(client, auth_headers, item["id"], 10)

        queued = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )
        assert item["id"] not in [str(r.compliance_item_id) for r in queued]

    def test_a_deactivated_client_is_skipped(
        self, client, auth_headers, client_id, db, firm_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        run_date = days_before_due(client, auth_headers, item["id"], 10)
        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)

        queued = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )
        assert queued == []

    def test_whatsapp_is_used_when_the_client_has_a_number(
        self, client, auth_headers, client_id, db, firm_id
    ):
        client.patch(
            f"/api/v1/clients/{client_id}",
            json={"whatsapp": "+919900112233"},
            headers=auth_headers,
        )
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        run_date = days_before_due(client, auth_headers, item["id"], 10)

        queued = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )
        assert queued[0].channel == ReminderChannel.WHATSAPP
        assert queued[0].recipient == "+919900112233"

    def test_an_empty_offset_list_is_a_no_op(self, db, firm_id):
        assert reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[]
        ) == []

    def test_a_client_switched_off_with_an_open_filing_is_still_skipped(
        self, client, auth_headers, client_id, db, firm_id
    ):
        """The guard on the client, not the one on the filing's status.

        Deactivating through the API also closes the client's open items, so
        the sweep never reaches a live filing belonging to an inactive client
        and the check that would skip them is never the reason nothing is
        queued. The two are worth separating: a filing reopened afterwards, or
        a client switched off by any route that leaves its work alone, puts a
        deactivated client back in front of this loop.
        """
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        run_date = days_before_due(client, auth_headers, item["id"], 10)

        db.get(Client, uuid.UUID(client_id)).is_active = False
        db.commit()

        queued = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )
        assert queued == []


class TestPaymentReminders:
    @pytest.fixture
    def sent_invoice(self, client, auth_headers, client_id) -> dict:
        invoice = client.post(
            "/api/v1/invoices",
            json={
                "client_id": client_id,
                # Raised a month ago on thirty-day terms, so it is genuinely a
                # week late rather than due before it was raised.
                "issue_date": (clock.today() - timedelta(days=37)).isoformat(),
                "due_date": (clock.today() - timedelta(days=7)).isoformat(),
                "lines": [
                    {"description": "GSTR-3B", "quantity": 1, "unit_price_paise": 200_000}
                ],
            },
            headers=auth_headers,
        ).json()
        return client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()

    def test_chases_an_overdue_invoice(self, db, firm_id, sent_invoice):
        queued = reminder_service.queue_payment_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[7]
        )
        db.commit()

        assert len(queued) == 1
        assert queued[0].reminder_type == ReminderType.PAYMENT
        assert queued[0].extra["offset_days"] == 7
        assert sent_invoice["invoice_number"] in queued[0].subject

    def test_the_body_quotes_the_outstanding_balance(self, db, firm_id, sent_invoice):
        queued = reminder_service.queue_payment_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[7]
        )
        assert "2,360.00" in queued[0].body

    def test_running_twice_does_not_double_chase(self, db, firm_id, sent_invoice):
        reminder_service.queue_payment_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[7]
        )
        db.commit()
        again = reminder_service.queue_payment_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[7]
        )
        assert again == []

    def test_a_paid_invoice_is_not_chased(
        self, client, auth_headers, db, firm_id, sent_invoice
    ):
        client.post(
            f"/api/v1/invoices/{sent_invoice['id']}/payments",
            json={"amount_paise": sent_invoice["total_paise"]},
            headers=auth_headers,
        )
        queued = reminder_service.queue_payment_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[7]
        )
        assert queued == []

    def test_a_draft_invoice_is_not_chased(self, client, auth_headers, client_id, db, firm_id):
        client.post(
            "/api/v1/invoices",
            json={
                "client_id": client_id,
                # Raised a month ago on thirty-day terms, so it is genuinely a
                # week late rather than due before it was raised.
                "issue_date": (clock.today() - timedelta(days=37)).isoformat(),
                "due_date": (clock.today() - timedelta(days=7)).isoformat(),
                "lines": [{"description": "x", "quantity": 1, "unit_price_paise": 1000}],
            },
            headers=auth_headers,
        )
        queued = reminder_service.queue_payment_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[7]
        )
        assert queued == []

    def test_a_non_offset_day_queues_nothing(self, db, firm_id, sent_invoice):
        queued = reminder_service.queue_payment_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[15, 30]
        )
        assert queued == []

    def test_a_client_who_has_left_is_not_chased_for_the_debt(
        self, db, firm_id, client_id, sent_invoice
    ):
        """The debt survives the relationship; chasing them for it does not.

        Unlike the document sweep, nothing upstream filters this one — an
        unpaid invoice stays unpaid whatever the client's status, so a
        deactivated client goes on receiving fee reminders on a schedule
        nobody is watching. The firm stopped acting for them; a WhatsApp
        message every seven days in the firm's name is not what that means.
        """
        db.get(Client, uuid.UUID(client_id)).is_active = False
        db.commit()

        queued = reminder_service.queue_payment_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[7]
        )
        assert queued == []

    def test_the_subject_quotes_the_balance_in_rupees(self, db, firm_id, sent_invoice):
        """The line the client reads first, and the one nothing checked.

        The body's figure was pinned; the subject carries the same number
        through its own conversion from paise, and got it wrong unnoticed. A
        fee reminder whose subject says a different amount from its body is
        one the firm has to explain.
        """
        queued = reminder_service.queue_payment_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[7]
        )
        assert sent_invoice["total_paise"] == 236_000  # 2,000.00 plus 18% GST
        assert "₹2,360.00 outstanding" in queued[0].subject


class TestManualReminders:
    """A practitioner composing one by hand, where the choices are theirs.

    ``preferred_channel`` is a default, not a policy: the whole point of the
    parameter is that a practitioner can send by email to a client who would
    ordinarily be reached on WhatsApp — a fee dispute they want in writing, a
    document the client asked for by mail. Nothing had ever passed one, so
    ignoring the choice and falling back to the default went unnoticed, and
    the recipient follows the channel.
    """

    def test_an_explicit_channel_beats_the_clients_usual_one(self, db, client_id):
        on_whatsapp = db.get(Client, uuid.UUID(client_id))
        on_whatsapp.whatsapp = "+919900112233"
        db.flush()
        assert reminder_service.preferred_channel(on_whatsapp) == ReminderChannel.WHATSAPP

        reminder = reminder_service.build_manual_reminder(
            db,
            client=on_whatsapp,
            reminder_type=ReminderType.CUSTOM,
            subject="Confirming in writing",
            body="As discussed.",
            channel=ReminderChannel.EMAIL,
        )
        assert reminder.channel == ReminderChannel.EMAIL
        assert reminder.recipient == on_whatsapp.email

    def test_no_channel_falls_back_to_the_clients_usual_one(self, db, client_id):
        on_whatsapp = db.get(Client, uuid.UUID(client_id))
        on_whatsapp.whatsapp = "+919900112233"
        db.flush()

        reminder = reminder_service.build_manual_reminder(
            db,
            client=on_whatsapp,
            reminder_type=ReminderType.CUSTOM,
            subject="s",
            body="b",
        )
        assert reminder.channel == ReminderChannel.WHATSAPP
        assert reminder.recipient == "+919900112233"


class TestReminderApi:
    def test_drafts_a_document_request(self, client, auth_headers, client_id):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        response = client.post(
            "/api/v1/reminders/draft",
            json={
                "client_id": client_id,
                "purpose": "document_request",
                "compliance_item_id": item["id"],
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["subject"].startswith("GSTR-3B")
        assert "Nimbus Textiles Pvt Ltd" in body["body"]
        assert "Bank statement" in body["body"]
        assert body["recipient"] == "accounts@nimbustextiles.in"

    def test_drafting_does_not_queue_anything(self, client, auth_headers, client_id, db):
        client.post(
            "/api/v1/reminders/draft",
            json={"client_id": client_id, "purpose": "document_request"},
            headers=auth_headers,
        )
        assert db.query(Reminder).count() == 0

    def test_queues_a_manual_reminder(self, client, auth_headers, client_id):
        response = client.post(
            "/api/v1/reminders",
            json={
                "client_id": client_id,
                "reminder_type": "custom",
                "subject": "Please call us",
                "body": "We need to discuss your audit.",
            },
            headers=auth_headers,
        )
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "scheduled"
        assert response.json()["client_name"] == "Nimbus Textiles Pvt Ltd"

    def test_a_client_with_no_address_cannot_be_reminded(
        self, client, auth_headers, client_id
    ):
        client.patch(
            f"/api/v1/clients/{client_id}",
            json={"email": None, "phone": None, "whatsapp": None},
            headers=auth_headers,
        )
        response = client.post(
            "/api/v1/reminders",
            json={"client_id": client_id, "subject": "Hello", "body": "Hi"},
            headers=auth_headers,
        )
        assert response.status_code == 422
        assert "no email address on file" in response.json()["detail"]

    def test_runs_the_document_sweep_on_demand(self, client, auth_headers, client_id):
        response = client.post(
            "/api/v1/reminders/queue", json={"kind": "document"}, headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json()["kind"] == "document"
        assert response.json()["queued"] >= 0

    def test_an_unknown_sweep_kind_is_rejected(self, client, auth_headers):
        response = client.post(
            "/api/v1/reminders/queue", json={"kind": "carrier-pigeon"}, headers=auth_headers
        )
        assert response.status_code == 422

    def test_cancels_a_scheduled_reminder(self, client, auth_headers, client_id):
        reminder = client.post(
            "/api/v1/reminders",
            json={"client_id": client_id, "subject": "s", "body": "b"},
            headers=auth_headers,
        ).json()

        response = client.post(
            f"/api/v1/reminders/{reminder['id']}/cancel", headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

    def test_a_cancelled_reminder_cannot_be_cancelled_again(
        self, client, auth_headers, client_id
    ):
        reminder = client.post(
            "/api/v1/reminders",
            json={"client_id": client_id, "subject": "s", "body": "b"},
            headers=auth_headers,
        ).json()
        client.post(f"/api/v1/reminders/{reminder['id']}/cancel", headers=auth_headers)
        again = client.post(
            f"/api/v1/reminders/{reminder['id']}/cancel", headers=auth_headers
        )
        assert again.status_code == 409

    def test_stops_chasing_a_client_entirely(self, client, auth_headers, client_id):
        for subject in ("one", "two"):
            client.post(
                "/api/v1/reminders",
                json={"client_id": client_id, "subject": subject, "body": "b"},
                headers=auth_headers,
            )
        response = client.post(
            "/api/v1/reminders/cancel-scheduled",
            params={"client_id": client_id},
            headers=auth_headers,
        )
        assert response.json() == {"cancelled": 2}

    def test_pending_counts_drive_the_nav_badge(self, client, auth_headers, client_id):
        client.post(
            "/api/v1/reminders",
            json={
                "client_id": client_id,
                "subject": "later",
                "body": "b",
                "scheduled_for": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            },
            headers=auth_headers,
        )
        client.post(
            "/api/v1/reminders",
            json={"client_id": client_id, "subject": "now", "body": "b"},
            headers=auth_headers,
        )
        counts = client.get("/api/v1/reminders/pending-count", headers=auth_headers).json()
        assert counts["scheduled"] == 2
        assert counts["due_now"] == 1

    def test_reminders_require_authentication(self, client):
        assert client.get("/api/v1/reminders").status_code == 401


class TestWorkerTasks:
    def test_the_document_sweep_task_runs(self, client, auth_headers, client_id, db):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        run_date = days_before_due(client, auth_headers, item["id"], 15)
        result = worker_tasks.queue_document_reminders_task(run_date.isoformat())
        assert result["queued"] >= 1

    def test_the_task_generation_task_runs(self, client, auth_headers, client_id, db):
        result = worker_tasks.generate_tasks_task(horizon_days=30)
        assert result["firms"] == 1
        assert result["created"] > 0

    def test_the_invoice_refresh_task_flips_overdue(
        self, client, auth_headers, client_id, db
    ):
        invoice = client.post(
            "/api/v1/invoices",
            json={
                "client_id": client_id,
                "due_date": (clock.today() + timedelta(days=1)).isoformat(),
                "lines": [{"description": "x", "quantity": 1, "unit_price_paise": 1000}],
            },
            headers=auth_headers,
        ).json()
        client.post(f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers)

        result = worker_tasks.refresh_invoice_statuses_task(
            (clock.today() + timedelta(days=5)).isoformat()
        )
        assert result["updated"] == 1

        refreshed = client.get(
            f"/api/v1/invoices/{invoice['id']}", headers=auth_headers
        ).json()
        assert refreshed["status"] == "overdue"

    def test_document_reminders_dispatch_like_any_other(
        self, client, auth_headers, client_id, db
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        run_date = days_before_due(client, auth_headers, item["id"], 15)
        worker_tasks.queue_document_reminders_task(run_date.isoformat())

        # Reminders are queued for 09:00 IST on the run date, which is past.
        db.query(Reminder).update({Reminder.scheduled_for: datetime(2020, 1, 1)})
        db.commit()

        result = worker_tasks.dispatch_due_reminders_task()
        assert result["sent"] >= 1

    def test_compliance_items_still_top_up(self, db):
        result = worker_tasks.generate_compliance_items_task()
        assert "created" in result


class TestWhatAMessageMayCite:
    """A reminder names a client and, optionally, the filing or invoice it is
    about — and the two were never checked against each other.

    Both are addressable by id alone, so a message could cite any record in the
    deployment: another client's, or another firm's entirely. The row then held
    a foreign key across a tenancy boundary, which is a cascade as well as an
    untidiness — the far firm deleting that client takes this firm's queued
    message with it, and neither firm can see why. Within one firm it is the
    everyday version: a stale id from the wrong screen, putting one client's
    filing on the record of a message sent to another, and a fee chase quoting
    what somebody else owes.
    """

    OUTSIDER = {
        "firm_name": "Meridian & Co",
        "icai_registration_number": "998877W",
        "firm_email": "office@meridian-ca.in",
        "pan": "AAACM7788K",
        "owner_full_name": "Vikram Rao",
        "owner_email": "vikram@meridian-ca.in",
        "owner_password": "another-correct-horse",
    }

    def _outsider(self, client) -> tuple[dict, str]:
        from tests.conftest import make_client_payload

        registered = client.post("/api/v1/auth/register", json=self.OUTSIDER).json()
        headers = {"Authorization": f"Bearer {registered['access_token']}"}
        their_client = client.post(
            "/api/v1/clients",
            json=make_client_payload(name="Meridian Client", pan="ZZZPC9999Z"),
            headers=headers,
        ).json()["client"]["id"]
        return headers, their_client

    def _second_client(self, client, auth_headers) -> str:
        from tests.conftest import make_client_payload

        return client.post(
            "/api/v1/clients",
            json=make_client_payload(name="Second Client", pan="BBBPC1234D"),
            headers=auth_headers,
        ).json()["client"]["id"]

    def _queue(self, client, auth_headers, **body):
        payload = {"subject": "About your return", "body": "Please send it over."}
        payload.update(body)
        return client.post("/api/v1/reminders", json=payload, headers=auth_headers)

    def test_a_filing_from_another_firm_is_a_404(self, client, auth_headers, client_id):
        their_headers, _ = self._outsider(client)
        their_item = first_item_of_type(client, their_headers, "GSTR3B_MONTHLY")

        response = self._queue(
            client, auth_headers, client_id=client_id, compliance_item_id=their_item["id"]
        )
        assert response.status_code == 404, response.text

    def test_an_invoice_from_another_firm_is_a_404(self, client, auth_headers, client_id):
        their_headers, their_client = self._outsider(client)
        their_item = first_item_of_type(client, their_headers, "GSTR3B_MONTHLY")
        client.patch(
            f"/api/v1/compliance/items/{their_item['id']}",
            json={"status": "filed", "fee_paise": 250_000},
            headers=their_headers,
        )
        their_invoice = client.post(
            "/api/v1/invoices/generate", json={}, headers=their_headers
        ).json()["invoices"][0]

        response = self._queue(
            client, auth_headers, client_id=client_id, invoice_id=their_invoice["id"]
        )
        assert response.status_code == 404, response.text

    def test_another_clients_filing_in_the_same_firm_is_a_400(
        self, client, auth_headers, client_id
    ):
        """The caller can see both records, so this is a pairing mistake rather
        than something hidden from them, and it is named as one."""
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        other = self._second_client(client, auth_headers)

        response = self._queue(
            client, auth_headers, client_id=other, compliance_item_id=item["id"]
        )
        assert response.status_code == 400, response.text
        assert "different client" in response.json()["detail"]

    def test_another_clients_invoice_in_the_same_firm_is_a_400(
        self, client, auth_headers, client_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        client.patch(
            f"/api/v1/compliance/items/{item['id']}",
            json={"status": "filed", "fee_paise": 250_000},
            headers=auth_headers,
        )
        invoice = client.post(
            "/api/v1/invoices/generate", json={}, headers=auth_headers
        ).json()["invoices"][0]
        other = self._second_client(client, auth_headers)

        response = self._queue(client, auth_headers, client_id=other, invoice_id=invoice["id"])
        assert response.status_code == 400, response.text

    def test_nothing_is_queued_when_the_link_is_refused(
        self, client, auth_headers, client_id, db
    ):
        """Checked before the row is built, not after — a refused reminder that
        is nonetheless sitting in the queue is the worse of the two failures."""
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        other = self._second_client(client, auth_headers)
        before = db.scalar(select(func.count(Reminder.id)))

        self._queue(client, auth_headers, client_id=other, compliance_item_id=item["id"])

        assert db.scalar(select(func.count(Reminder.id))) == before

    def test_the_clients_own_filing_is_accepted(self, client, auth_headers, client_id):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        response = self._queue(
            client, auth_headers, client_id=client_id, compliance_item_id=item["id"]
        )
        assert response.status_code == 201, response.text
        assert response.json()["compliance_item_id"] == item["id"]

    def test_a_message_citing_nothing_is_still_fine(self, client, auth_headers, client_id):
        assert self._queue(client, auth_headers, client_id=client_id).status_code == 201

    def test_drafting_is_held_to_the_same_pairing(self, client, auth_headers, client_id):
        """The draft quotes the filing back, so citing another client's would
        put their period and deadline into a message addressed to this one."""
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        other = self._second_client(client, auth_headers)

        response = client.post(
            "/api/v1/reminders/draft",
            json={"client_id": other, "compliance_item_id": item["id"]},
            headers=auth_headers,
        )
        assert response.status_code == 400, response.text


class TestOneSweepAtATime:
    """Queueing a chase is a read-decide-write, and it was not ordered.

    Read what is already queued for a filing, decide there is nothing, add one.
    Neither sweep commits until it has been through every firm it was given,
    and a run spends up to ``ai_draft_budget_seconds`` on the wording — so the
    gap between the read and the commit is a minute or two wide, not an
    instant.

    Two runs inside that gap both read an empty queue and both add. The nightly
    beat is one; *Queue reminders now* on the reminders screen is the other,
    and it is the same code reachable by any manager at any moment — including
    twice, from one double-clicked button, landing on two workers. What comes
    out is a client emailed the identical document chase or fee reminder twice
    on the same morning, over the firm's own name.

    Ordering is the whole fix, so ordering is what is asserted: a lock taken
    after the decision orders the writes and nothing else, which is exactly the
    state this replaced.
    """

    @pytest.fixture
    def overdue_invoice(self, client, auth_headers, client_id) -> dict:
        invoice = client.post(
            "/api/v1/invoices",
            json={
                "client_id": client_id,
                "issue_date": (clock.today() - timedelta(days=37)).isoformat(),
                "due_date": (clock.today() - timedelta(days=7)).isoformat(),
                "lines": [
                    {"description": "GSTR-3B", "quantity": 1, "unit_price_paise": 200_000}
                ],
            },
            headers=auth_headers,
        ).json()
        return client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()

    def _record_order(self, monkeypatch) -> list[str]:
        order: list[str] = []
        monkeypatch.setattr(
            reminder_service.firms, "lock_firm", lambda session, fid: order.append("lock")
        )
        return order

    def test_the_firm_is_held_before_its_document_queue_is_read(
        self, client, auth_headers, client_id, db, firm_id, monkeypatch
    ):
        order = self._record_order(monkeypatch)
        real = reminder_service.documents.items_awaiting_documents
        monkeypatch.setattr(
            reminder_service.documents,
            "items_awaiting_documents",
            lambda *a, **kw: (order.append("read"), real(*a, **kw))[1],
        )

        reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[10]
        )

        assert order[:2] == ["lock", "read"]

    def test_the_firm_is_held_before_its_receivables_are_read(
        self, db, firm_id, overdue_invoice, monkeypatch
    ):
        order = self._record_order(monkeypatch)
        real = reminder_service.billing.unpaid_invoices
        monkeypatch.setattr(
            reminder_service.billing,
            "unpaid_invoices",
            lambda *a, **kw: (order.append("read"), real(*a, **kw))[1],
        )

        reminder_service.queue_payment_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[7]
        )

        assert order[:2] == ["lock", "read"]

    def test_a_chase_queued_while_we_waited_is_not_queued_again(
        self, client, auth_headers, client_id, db, firm_id, monkeypatch
    ):
        """The interleaving itself, in the order it happens.

        The competing run is staged on the lock: it commits from a second
        connection at the moment this one takes the firm's row, which is the
        instant a real loser resumes at. Everything after that is the ordinary
        code path deciding what is left to chase.
        """
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        run_date = days_before_due(client, auth_headers, item["id"], 10)
        db.rollback()  # SQLite will not let another connection write past a held read

        from app.database import SessionLocal

        fired = []

        def winner_commits_first(session, fid):
            if fired:
                return
            fired.append(fid)
            other = SessionLocal()
            try:
                reminder_service.queue_document_reminders(
                    other, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
                )
                other.commit()
            finally:
                other.close()

        monkeypatch.setattr(reminder_service.firms, "lock_firm", winner_commits_first)

        reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )
        db.commit()

        assert fired, "the competing run never happened"
        doubled = [
            str(item_id)
            for item_id, count in db.execute(
                select(Reminder.compliance_item_id, func.count(Reminder.id))
                .where(Reminder.firm_id == uuid.UUID(firm_id))
                .group_by(Reminder.compliance_item_id)
            ).all()
            if count > 1
        ]
        assert not doubled, f"clients chased twice about filings: {doubled}"

    def test_a_fee_chase_queued_while_we_waited_is_not_queued_again(
        self, db, firm_id, overdue_invoice, monkeypatch
    ):
        db.rollback()
        from app.database import SessionLocal

        fired = []

        def winner_commits_first(session, fid):
            if fired:
                return
            fired.append(fid)
            other = SessionLocal()
            try:
                reminder_service.queue_payment_reminders(
                    other, firm_id=uuid.UUID(firm_id), offsets=[7]
                )
                other.commit()
            finally:
                other.close()

        monkeypatch.setattr(reminder_service.firms, "lock_firm", winner_commits_first)

        reminder_service.queue_payment_reminders(
            db, firm_id=uuid.UUID(firm_id), offsets=[7]
        )
        db.commit()

        assert fired, "the competing run never happened"
        assert (
            db.scalar(
                select(func.count(Reminder.id)).where(
                    Reminder.invoice_id == uuid.UUID(overdue_invoice["id"])
                )
            )
            == 1
        )

    def test_a_firm_that_is_not_served_is_never_held(self, db, firm_id, monkeypatch):
        """Nothing is queued for it, so there is nothing to order — and taking
        its row would block the firm's own requests for a sweep that does no
        work on it."""
        order = self._record_order(monkeypatch)

        reminder_service.queue_payment_reminders(db, firm_id=uuid.uuid4(), offsets=[7])
        reminder_service.queue_document_reminders(db, firm_id=uuid.uuid4(), offsets=[10])

        assert order == []

    def test_the_sweeps_still_span_every_firm_they_are_given(
        self, client, auth_headers, db, monkeypatch
    ):
        """Locking per firm must not have narrowed what a beat run covers."""
        held = []
        monkeypatch.setattr(
            reminder_service.firms, "lock_firm", lambda session, fid: held.append(fid)
        )

        reminder_service.queue_payment_reminders(db, offsets=[7])

        servable = reminder_service.firms.servable_firm_ids(db)
        assert set(held) == servable
        assert held == sorted(held), "an undefined lock order lets two sweeps cross"


class TestTheQueueIsReadOncePerFirm:
    """``already_queued`` asked the database one filing at a time.

    The read sat inside the loop over the firm's whole due list, inside the
    transaction that holds the firm's row for the length of the run. A firm's
    deadlines cluster on the same offset day — every GST client is due on the
    20th — so the loop is as long as the client list, not a handful, and the
    quiet run where everything is already queued costs exactly as much as the
    busy one.
    """

    @pytest.fixture
    def counted_sql(self):
        """Every statement the engine executes inside the block."""
        from sqlalchemy import event

        from app.database import engine

        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()))

        event.listen(engine, "before_cursor_execute", record)
        try:
            yield statements
        finally:
            event.remove(engine, "before_cursor_execute", record)

    def _reminder_reads(self, statements) -> int:
        return sum(
            1
            for statement in statements
            if statement.startswith("SELECT") and " FROM reminders" in statement
        )

    def _many_filings_on_one_day(self, client, auth_headers, db, run_date, count=6):
        """Put ``count`` clients' filings all on the same offset day."""
        from app.models.compliance import ComplianceItem
        from tests.conftest import make_client_payload

        for index in range(count):
            payload = make_client_payload()
            payload["name"] = f"Batch Client {index}"
            payload.pop("pan")
            payload.pop("gstin")
            assert (
                client.post("/api/v1/clients", json=payload, headers=auth_headers).status_code
                == 201
            )
        items = db.scalars(select(ComplianceItem)).all()
        for item in items:
            item.due_date = run_date + timedelta(days=10)
        db.commit()
        return items

    def test_the_document_sweep_reads_the_queue_once(
        self, client, auth_headers, db, firm_id, counted_sql
    ):
        run_date = clock.today()
        items = self._many_filings_on_one_day(client, auth_headers, db, run_date)
        assert len(items) > 20, "not enough filings for the count to mean anything"

        counted_sql.clear()
        queued = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )

        assert len(queued) > 20
        assert self._reminder_reads(counted_sql) == 1

    def test_the_document_sweep_still_skips_what_is_already_queued(
        self, client, auth_headers, db, firm_id
    ):
        """The batched read has to answer the same question the loop did."""
        run_date = clock.today()
        self._many_filings_on_one_day(client, auth_headers, db, run_date)

        first = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )
        db.commit()
        second = reminder_service.queue_document_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[10]
        )

        assert len(first) > 20
        assert second == []

    def test_the_payment_sweep_reads_the_queue_once(
        self, client, auth_headers, db, firm_id, client_id, counted_sql
    ):
        from app.models.base import InvoiceStatus
        from app.models.invoice import Invoice
        from tests.conftest import make_client_payload
        from tests.test_invoices import file_everything

        # One invoice per client, so the count can tell a batched read from a
        # per-invoice one.
        for index in range(6):
            payload = make_client_payload()
            payload["name"] = f"Receivable Client {index}"
            payload.pop("pan")
            payload.pop("gstin")
            assert (
                client.post("/api/v1/clients", json=payload, headers=auth_headers).status_code
                == 201
            )
        file_everything(client, auth_headers)
        client.post("/api/v1/invoices/generate", json={}, headers=auth_headers)
        drafts = db.scalars(select(Invoice)).all()
        run_date = clock.today()
        for invoice in drafts:
            invoice.status = InvoiceStatus.SENT
            invoice.due_date = run_date - timedelta(days=7)
        db.commit()
        assert len(drafts) >= 6

        counted_sql.clear()
        queued = reminder_service.queue_payment_reminders(
            db, firm_id=uuid.UUID(firm_id), today=run_date, offsets=[7]
        )

        assert len(queued) == len(drafts)
        assert self._reminder_reads(counted_sql) == 1

    def test_the_filing_sweep_reads_the_queue_once(
        self, client, auth_headers, db, counted_sql, monkeypatch
    ):
        """The third sweep, which lives in the worker."""
        run_date = clock.today()
        items = self._many_filings_on_one_day(client, auth_headers, db, run_date)
        assert len(items) > 20

        monkeypatch.setattr(worker_tasks, "SessionLocal", lambda: _NonClosing(db))
        counted_sql.clear()
        result = worker_tasks.schedule_compliance_reminders_task(run_date.isoformat())

        assert result["queued"] > 0
        assert self._reminder_reads(counted_sql) == 1


class _NonClosing:
    """The test's own session, handed to a worker task that owns its lifetime."""

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc):
        return False


class TestCancellingAChaseThatIsGoingOut:
    """A cancel decides from the row, not from a copy taken before the send.

    The dispatcher claims a due reminder with ``FOR UPDATE SKIP LOCKED``, sends
    it, stamps ``SENT`` and commits. A plain read is not blocked by that lock,
    so cancelling read a status from before the message went out and the
    ``UPDATE`` merely queued behind the send — landing a row that says
    ``cancelled`` with ``sent_at`` set beside it. The firm's own record of who
    it has written to then says this client was not contacted, on a morning
    they were.
    """

    def _queued(self, client, auth_headers, client_id, subject="chase"):
        return client.post(
            "/api/v1/reminders",
            json={"client_id": client_id, "subject": subject, "body": "b"},
            headers=auth_headers,
        ).json()

    def _send_from_another_connection(self, db, reminder_id: uuid.UUID) -> None:
        """What the dispatcher commits while this request is deciding."""
        db.commit()  # SQLite will not let another connection write past a read

        from app.database import SessionLocal

        other = SessionLocal()
        try:
            theirs = reminder_service.load_for_update(other, reminder_id)
            theirs.status = ReminderStatus.SENT
            theirs.sent_at = datetime.now(UTC)
            other.commit()
        finally:
            other.close()

    def test_the_locked_read_sees_a_send_a_plain_one_misses(
        self, client, auth_headers, client_id, db
    ):
        reminder = self._queued(client, auth_headers, client_id)
        reminder_id = uuid.UUID(reminder["id"])

        # Our copy, taken before the dispatcher's send exists. Held onto, or
        # the session lets go of it and the staleness never arises.
        ours = db.get(Reminder, reminder_id)
        assert ours.status == ReminderStatus.SCHEDULED

        self._send_from_another_connection(db, reminder_id)

        # The plain read still answers with what we loaded first.
        assert db.get(Reminder, reminder_id).status == ReminderStatus.SCHEDULED
        # The locked one is what the cancel has to be decided from.
        assert reminder_service.load_for_update(db, reminder_id).status == (
            ReminderStatus.SENT
        )

    def test_a_reminder_already_sent_is_refused_rather_than_overwritten(
        self, client, auth_headers, client_id, db
    ):
        reminder = self._queued(client, auth_headers, client_id)
        reminder_id = uuid.UUID(reminder["id"])
        # Into the identity map and *kept* there: the map holds weak
        # references, so a copy nothing is holding is collected and the next
        # plain read goes to the database after all. A request that has already
        # touched the row — the ``POST`` above did — is holding one.
        ours = db.get(Reminder, reminder_id)

        self._send_from_another_connection(db, reminder_id)
        assert ours.status == ReminderStatus.SCHEDULED, "the stale copy went away"

        response = client.post(
            f"/api/v1/reminders/{reminder['id']}/cancel", headers=auth_headers
        )
        assert response.status_code == 409, response.text
        assert "already sent" in response.json()["detail"]
        assert reminder_service.load_for_update(db, reminder_id).status == (
            ReminderStatus.SENT
        )

    def test_stopping_every_chase_holds_the_rows_it_decides_from(
        self, client, auth_headers, client_id, monkeypatch
    ):
        """"Stop chasing this client" is the same race in bulk.

        And it is the one a practitioner reaches for when a client rings in —
        which is to say in the morning, while the dispatcher is working through
        exactly these rows. SQLite renders no locking clause, so the ordering
        itself cannot be reproduced here; what can be asserted is that the
        endpoint asks for the rows through the lock rather than past it.
        """
        for subject in ("one", "two"):
            self._queued(client, auth_headers, client_id, subject)

        locked = []
        real = reminder_service.scheduled_for_client_for_update
        monkeypatch.setattr(
            reminder_service,
            "scheduled_for_client_for_update",
            lambda session, **kw: (locked.append(kw), real(session, **kw))[1],
        )

        response = client.post(
            "/api/v1/reminders/cancel-scheduled",
            params={"client_id": client_id},
            headers=auth_headers,
        )
        assert response.json() == {"cancelled": 2}
        assert [kw["client_id"] for kw in locked] == [uuid.UUID(client_id)]

    def test_stopping_every_chase_leaves_the_one_already_sent_alone(
        self, client, auth_headers, client_id, db
    ):
        sent = self._queued(client, auth_headers, client_id, "going out")
        still_queued = self._queued(client, auth_headers, client_id, "waiting")
        # Held, for the reason above: an identity-map entry nothing references
        # is collected, and the staleness this exists to reproduce never
        # arises.
        ours = [db.get(Reminder, uuid.UUID(row["id"])) for row in (sent, still_queued)]

        self._send_from_another_connection(db, uuid.UUID(sent["id"]))
        assert [r.status for r in ours] == [ReminderStatus.SCHEDULED] * 2

        response = client.post(
            "/api/v1/reminders/cancel-scheduled",
            params={"client_id": client_id},
            headers=auth_headers,
        )
        # One of the two was still waiting; the other had gone out.
        assert response.json() == {"cancelled": 1}
        assert reminder_service.load_for_update(db, uuid.UUID(sent["id"])).status == (
            ReminderStatus.SENT
        )
        assert reminder_service.load_for_update(
            db, uuid.UUID(still_queued["id"])
        ).status == ReminderStatus.CANCELLED

    def test_the_cancel_endpoint_takes_its_reminder_through_the_lock(
        self, client, auth_headers, client_id, monkeypatch
    ):
        """Asserted on the route, not left to the service.

        The guard is one call on a two-line handler, and going back to
        ``db.get`` is a change every other test in this class would still pass
        on SQLite — where the lock renders as nothing and only the re-read is
        observable.
        """
        reminder = self._queued(client, auth_headers, client_id)

        locked = []
        real = reminder_service.load_for_update
        monkeypatch.setattr(
            reminder_service,
            "load_for_update",
            lambda session, rid: (locked.append(rid), real(session, rid))[1],
        )

        response = client.post(
            f"/api/v1/reminders/{reminder['id']}/cancel", headers=auth_headers
        )
        assert response.status_code == 200, response.text
        assert locked == [uuid.UUID(reminder["id"])]

    def test_a_reminder_of_another_firm_is_still_not_found(
        self, client, auth_headers, client_id, db
    ):
        """The lock comes before the tenancy check, so it has to not leak one."""
        reminder = self._queued(client, auth_headers, client_id)
        other_firm = uuid.uuid4()
        row = db.get(Reminder, uuid.UUID(reminder["id"]))
        row.firm_id = other_firm
        db.commit()

        response = client.post(
            f"/api/v1/reminders/{reminder['id']}/cancel", headers=auth_headers
        )
        assert response.status_code == 404


class TestWhatAPractitionerMaySendUs:
    """The two free-form fields on this router, both of which were unbounded.

    ``extra_context`` is the sharper one: every entry is rendered into the
    prompt posted to OpenRouter, so an unbounded dict is an unbounded outbound
    request on a paid API, reachable by any authenticated practitioner.
    """

    def test_a_context_entry_longer_than_a_fact_is_refused(
        self, client, auth_headers, client_id
    ):
        response = client.post(
            "/api/v1/reminders/draft",
            json={
                "client_id": client_id,
                "extra_context": {"note": "x" * 5_000},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422, response.text

    def test_a_context_of_thousands_of_entries_is_refused(
        self, client, auth_headers, client_id
    ):
        response = client.post(
            "/api/v1/reminders/draft",
            json={
                "client_id": client_id,
                "extra_context": {f"k{n}": "v" for n in range(2_000)},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422, response.text

    def test_a_nested_structure_is_not_a_fact_about_a_filing(
        self, client, auth_headers, client_id
    ):
        response = client.post(
            "/api/v1/reminders/draft",
            json={
                "client_id": client_id,
                "extra_context": {"payload": {"nested": ["and", "unbounded"]}},
            },
            headers=auth_headers,
        )
        assert response.status_code == 422, response.text

    def test_the_ordinary_context_a_practitioner_adds_still_works(
        self, client, auth_headers, client_id
    ):
        response = client.post(
            "/api/v1/reminders/draft",
            json={
                "client_id": client_id,
                "purpose": "fee_reminder",
                "extra_context": {
                    "invoice_number": "INV/FY2026-27/0004",
                    "amount_inr": 12_500.50,
                    "urgent": True,
                    "chased_before": None,
                },
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["body"]

    def test_a_message_body_larger_than_a_message_is_refused(
        self, client, auth_headers, client_id
    ):
        response = client.post(
            "/api/v1/reminders",
            json={
                "client_id": client_id,
                "subject": "Statement of account",
                "body": "x" * 50_000,
            },
            headers=auth_headers,
        )
        assert response.status_code == 422, response.text

    def test_a_long_but_reasonable_body_is_still_accepted(
        self, client, auth_headers, client_id
    ):
        response = client.post(
            "/api/v1/reminders",
            json={
                "client_id": client_id,
                "subject": "Statement of account",
                "body": "Dear client,\n\n" + ("line of the statement\n" * 500),
            },
            headers=auth_headers,
        )
        assert response.status_code == 201, response.text


class TestPagingAListWhoseRowsShareAnInstant:
    """``scheduled_for`` is not a tie-break here; it is the tie.

    A sweep stamps every reminder it queues with ``ist_morning(run_date)`` —
    09:00 IST — so a firm's whole morning queue carries one identical instant.
    Ordering on that column alone leaves the row order unspecified, and an
    unspecified order re-evaluated per page is what makes ``LIMIT``/``OFFSET``
    hand the same reminder back on two pages while dropping another entirely.

    This is the screen a practitioner checks to see what went out to whom, so a
    row that vanishes between pages is a client who looks unchased.
    """

    def _queue(self, db, client, auth_headers, count: int = 12) -> int:
        """A morning's worth of reminders, all sharing one scheduled instant."""
        firm_id = db.scalar(select(Client.firm_id))
        client_id = db.scalar(select(Client.id))
        moment = reminder_service.ist_morning(clock.today())
        for index in range(count):
            db.add(
                Reminder(
                    firm_id=firm_id,
                    client_id=client_id,
                    reminder_type=ReminderType.FILING,
                    channel=ReminderChannel.EMAIL,
                    status=ReminderStatus.SCHEDULED,
                    subject=f"Chase {index:02d}",
                    body="…",
                    recipient="accounts@nimbustextiles.in",
                    scheduled_for=moment,
                    extra={"kind": "filing", "offset_days": 10},
                )
            )
        db.commit()
        return count

    def _walk(self, client, auth_headers, *, limit: int) -> list[str]:
        seen: list[str] = []
        offset = 0
        while True:
            page = client.get(
                "/api/v1/reminders",
                params={"limit": limit, "offset": offset},
                headers=auth_headers,
            ).json()
            seen.extend(row["id"] for row in page["items"])
            offset += limit
            if offset >= page["total"]:
                return seen

    def test_paging_the_queue_shows_every_reminder_exactly_once(
        self, client, auth_headers, client_id, db
    ):
        queued = self._queue(db, client, auth_headers)

        seen = self._walk(client, auth_headers, limit=3)

        assert len(seen) == queued
        assert len(set(seen)) == queued, "a reminder appeared on two pages"

    def test_the_page_boundary_does_not_move_between_reads(
        self, client, auth_headers, client_id, db
    ):
        """Same query, same rows — the property ``OFFSET`` paging rests on."""
        self._queue(db, client, auth_headers)

        first = self._walk(client, auth_headers, limit=5)
        again = self._walk(client, auth_headers, limit=5)

        assert first == again

    def test_one_page_of_them_matches_the_head_of_the_whole_list(
        self, client, auth_headers, client_id, db
    ):
        queued = self._queue(db, client, auth_headers)

        whole = self._walk(client, auth_headers, limit=queued)
        page = client.get(
            "/api/v1/reminders", params={"limit": 4}, headers=auth_headers
        ).json()

        assert [row["id"] for row in page["items"]] == whole[:4]

    def test_the_order_the_page_is_taken_in_settles_every_pair_of_rows(
        self, client, auth_headers, client_id, db, recorded_sql
    ):
        """The invariant itself, read off the SQL — see :func:`paged_order_by`.

        SQLite returns these in rowid order whatever the clause says, so
        walking the pages there cannot tell a total order from an accidental
        one. The deployment runs PostgreSQL, which is under no such obligation.
        """
        self._queue(db, client, auth_headers, count=3)

        recorded_sql.clear()
        assert client.get("/api/v1/reminders", headers=auth_headers).status_code == 200

        assert paged_order_by(recorded_sql, "reminders").endswith("reminders.id DESC")


class TestWhenAReminderTheFirmScheduledActuallyGoesOut:
    """``scheduled_for`` is the only datetime this API takes in.

    A timestamp with no offset names a wall clock rather than a moment, and
    nothing required one — so what the value meant was settled by whichever
    reader got to it. PostgreSQL casts a naive value using the session's
    ``TimeZone``, which nothing here sets; SQLAlchemy's SQLite dialect drops
    ``tzinfo`` on the way in, and ``clock.to_ist`` reads a stored naive
    timestamp as UTC.

    Read as IST, which is the clock the product runs on and the one the person
    filling in the field is looking at — the same reading ``ist_morning``,
    ``audit._day_bounds`` and ``tasks._month_start_instant`` already take.
    """

    def _queue(self, client, auth_headers, client_id, scheduled_for: str) -> str:
        response = client.post(
            "/api/v1/reminders",
            json={
                "client_id": client_id,
                "subject": "Please send the bank statement",
                "body": "b",
                "scheduled_for": scheduled_for,
            },
            headers=auth_headers,
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def _stored(self, db, reminder_id: str) -> datetime:
        db.expire_all()
        return db.get(Reminder, uuid.UUID(reminder_id)).scheduled_for

    def test_a_bare_timestamp_is_the_indian_morning_the_firm_meant(
        self, client, auth_headers, client_id, db
    ):
        """09:00 in the office is 03:30 UTC, not 09:00 UTC.

        Read as UTC it became 14:30 IST: a chase set for first thing went out
        in the middle of the afternoon.
        """
        reminder_id = self._queue(client, auth_headers, client_id, "2026-09-01T09:00:00")

        assert clock.to_ist(self._stored(db, reminder_id)).replace(tzinfo=None) == datetime(
            2026, 9, 1, 9, 0
        )

    def test_an_offset_the_caller_sent_is_believed(
        self, client, auth_headers, client_id, db
    ):
        reminder_id = self._queue(
            client, auth_headers, client_id, "2026-09-01T09:00:00+00:00"
        )

        assert clock.to_ist(self._stored(db, reminder_id)).replace(tzinfo=None) == datetime(
            2026, 9, 1, 14, 30
        )

    def test_a_non_utc_offset_is_re_expressed_rather_than_dropped(
        self, client, auth_headers, client_id, db
    ):
        """Both backends have to store the same moment, whatever arrived.

        SQLite's dialect writes the naive part and discards the offset, so an
        aware value that is not already UTC would otherwise be stored as its
        own local wall clock.
        """
        reminder_id = self._queue(
            client, auth_headers, client_id, "2026-09-01T09:00:00+05:30"
        )

        assert clock.to_ist(self._stored(db, reminder_id)).replace(tzinfo=None) == datetime(
            2026, 9, 1, 9, 0
        )

    def test_the_evening_of_a_deadline_is_not_pushed_past_it(
        self, client, auth_headers, client_id, db
    ):
        """The direction that costs the client the filing.

        20:00 IST on the 20th read as UTC is 01:30 IST on the *21st* — after
        the deadline the reminder exists to beat.
        """
        reminder_id = self._queue(client, auth_headers, client_id, "2026-09-20T20:00:00")

        assert clock.date_of(self._stored(db, reminder_id)) == date(2026, 9, 20)

    def test_a_reminder_with_no_time_on_it_still_goes_out_now(
        self, client, auth_headers, client_id, db
    ):
        """Omitting the field is unchanged: the row is queued for this instant."""
        response = client.post(
            "/api/v1/reminders",
            json={"client_id": client_id, "subject": "now", "body": "b"},
            headers=auth_headers,
        )
        assert response.status_code == 201, response.text

        queued = self._stored(db, response.json()["id"])
        assert abs((datetime.now(UTC) - clock.to_ist(queued).astimezone(UTC)).total_seconds()) < 60

    def test_the_dispatcher_agrees_about_which_ones_are_due(
        self, client, auth_headers, client_id, db
    ):
        """The whole point of pinning it: a queue read against ``now``.

        A bare morning timestamp from a day already past is due; one from a day
        still ahead is not, and neither answer may depend on how the database
        was configured.
        """
        yesterday = clock.today() - timedelta(days=1)
        tomorrow = clock.today() + timedelta(days=1)
        self._queue(client, auth_headers, client_id, f"{yesterday.isoformat()}T09:00:00")
        self._queue(client, auth_headers, client_id, f"{tomorrow.isoformat()}T09:00:00")

        counts = client.get(
            "/api/v1/reminders/pending-count", headers=auth_headers
        ).json()

        assert counts == {"scheduled": 2, "due_now": 1}


class TestDraftingAgainstAnInvoice:
    """A fee chase is drafted from the invoice, so the invoice has to be read.

    ``POST /reminders/draft`` fills the context from whichever record the
    caller cites, and the amount it quotes is the *balance* rather than the
    total — a client who has part-paid is asked for what is left, not for what
    the bill said before their money arrived.
    """

    @pytest.fixture
    def sent_invoice(self, client, auth_headers, client_id) -> dict:
        invoice = client.post(
            "/api/v1/invoices",
            json={
                "client_id": client_id,
                "issue_date": (clock.today() - timedelta(days=37)).isoformat(),
                "due_date": (clock.today() - timedelta(days=7)).isoformat(),
                "lines": [
                    {"description": "GSTR-3B", "quantity": 1, "unit_price_paise": 200_000}
                ],
            },
            headers=auth_headers,
        ).json()
        return client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()

    def test_the_draft_is_about_the_invoice_that_was_cited(
        self, client, auth_headers, client_id, sent_invoice
    ):
        response = client.post(
            "/api/v1/reminders/draft",
            json={
                "client_id": client_id,
                "purpose": "fee_reminder",
                "invoice_id": sent_invoice["id"],
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["subject"] == f"Invoice {sent_invoice['invoice_number']}"
        assert sent_invoice["invoice_number"] in body["body"]
        assert "2,360.00" in body["body"]

    def test_a_part_paid_invoice_is_chased_for_the_balance(
        self, client, auth_headers, client_id, sent_invoice
    ):
        """Quoting the total would ask for money the client has already sent."""
        client.post(
            f"/api/v1/invoices/{sent_invoice['id']}/payments",
            json={"amount_paise": 100_000},
            headers=auth_headers,
        )

        drafted = client.post(
            "/api/v1/reminders/draft",
            json={
                "client_id": client_id,
                "purpose": "fee_reminder",
                "invoice_id": sent_invoice["id"],
            },
            headers=auth_headers,
        ).json()
        assert "1,360.00" in drafted["body"]
        assert "2,360.00" not in drafted["body"]

    def test_drafting_against_another_clients_invoice_is_a_400(
        self, client, auth_headers, client_id, sent_invoice
    ):
        """The caller can see both, so the pair being wrong is theirs to fix."""
        from tests.conftest import make_client_payload

        other_id = client.post(
            "/api/v1/clients",
            json=make_client_payload(name="Second Client", pan="BBBPC1234D"),
            headers=auth_headers,
        ).json()["client"]["id"]

        response = client.post(
            "/api/v1/reminders/draft",
            json={
                "client_id": other_id,
                "purpose": "fee_reminder",
                "invoice_id": sent_invoice["id"],
            },
            headers=auth_headers,
        )
        assert response.status_code == 400, response.text
        assert "different client" in response.json()["detail"]

    def test_drafting_for_a_client_this_firm_cannot_reach_is_a_404(
        self, client, auth_headers
    ):
        response = client.post(
            "/api/v1/reminders/draft",
            json={"client_id": str(uuid.uuid4())},
            headers=auth_headers,
        )
        assert response.status_code == 404, response.text


class TestRunningTheSweepsOnDemand:
    """*Queue reminders now* on the reminders screen, both kinds of it.

    The nightly beat is one caller of these sweeps; a manager pressing the
    button is the other, and the payment half of it had no test at all — which
    is the half that quotes a client an amount.
    """

    def test_the_payment_sweep_can_be_run_from_the_screen(
        self, client, auth_headers, client_id
    ):
        invoice = client.post(
            "/api/v1/invoices",
            json={
                "client_id": client_id,
                "issue_date": (clock.today() - timedelta(days=37)).isoformat(),
                # Today, so it matches the first configured offset whatever the
                # firm has set the rest of them to.
                "due_date": clock.today().isoformat(),
                "lines": [
                    {"description": "GSTR-3B", "quantity": 1, "unit_price_paise": 200_000}
                ],
            },
            headers=auth_headers,
        ).json()
        client.post(f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers)

        response = client.post(
            "/api/v1/reminders/queue", json={"kind": "payment"}, headers=auth_headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["kind"] == "payment"
        assert body["queued"] == 1
        assert body["reminders"][0]["invoice_id"] == invoice["id"]
        assert body["reminders"][0]["reminder_type"] == "payment"

    def test_an_unrecognised_kind_is_refused_rather_than_guessed_at(
        self, client, auth_headers
    ):
        response = client.post(
            "/api/v1/reminders/queue", json={"kind": "filing"}, headers=auth_headers
        )
        assert response.status_code == 422, response.text


class TestNarrowingTheReminderList:
    """The filters the reminders screen sends, asserted through the endpoint."""

    @pytest.fixture
    def queued(self, client, auth_headers, client_id) -> dict:
        return client.post(
            "/api/v1/reminders",
            json={
                "client_id": client_id,
                "reminder_type": "custom",
                "channel": "email",
                "subject": "About your return",
                "body": "Please send it over.",
            },
            headers=auth_headers,
        ).json()

    def listed(self, client, auth_headers, **params):
        return client.get(
            "/api/v1/reminders", params=params, headers=auth_headers
        ).json()

    def test_by_client(self, client, auth_headers, client_id, queued):
        assert self.listed(client, auth_headers, client_id=client_id)["total"] == 1
        assert self.listed(client, auth_headers, client_id=str(uuid.uuid4()))["total"] == 0

    def test_by_type(self, client, auth_headers, queued):
        assert self.listed(client, auth_headers, reminder_type="custom")["total"] == 1
        assert self.listed(client, auth_headers, reminder_type="payment")["total"] == 0

    def test_by_status(self, client, auth_headers, queued):
        assert self.listed(client, auth_headers, reminder_status="scheduled")["total"] == 1
        assert self.listed(client, auth_headers, reminder_status="sent")["total"] == 0

    def test_by_channel(self, client, auth_headers, queued):
        assert self.listed(client, auth_headers, channel="email")["total"] == 1
        assert self.listed(client, auth_headers, channel="sms")["total"] == 0


class TestWhatTheSweepsLeaveAlone:
    """The cases where there is nothing to chase, and nothing is queued."""

    def test_a_firm_with_no_configured_offsets_is_not_swept(self, db, firm_id):
        """An empty offsets list is a firm that has switched the chase off.

        Read as "no days to fire on" rather than as "use the defaults", so
        turning it off is something a firm can actually do.
        """
        assert (
            reminder_service.queue_payment_reminders(
                db, firm_id=uuid.UUID(firm_id), offsets=[]
            )
            == []
        )
        assert (
            reminder_service.queue_document_reminders(
                db, firm_id=uuid.UUID(firm_id), offsets=[]
            )
            == []
        )

    def test_an_invoice_with_no_due_date_is_never_late(
        self, client, auth_headers, client_id, db, firm_id
    ):
        """Lateness is counted from the due date, so there is nothing to count.

        A sent invoice always has one — ``send_invoice`` fills it in from the
        firm's terms — but the column is nullable and the sweep reads rows, not
        the endpoint that wrote them.
        """
        from app.models.invoice import Invoice

        invoice = client.post(
            "/api/v1/invoices",
            json={
                "client_id": client_id,
                "lines": [
                    {"description": "GSTR-3B", "quantity": 1, "unit_price_paise": 200_000}
                ],
            },
            headers=auth_headers,
        ).json()
        client.post(f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers)

        row = db.get(Invoice, uuid.UUID(invoice["id"]))
        row.due_date = None
        db.commit()

        assert (
            reminder_service.queue_payment_reminders(
                db, firm_id=uuid.UUID(firm_id), offsets=[0, 7, 15, 30]
            )
            == []
        )


class TestWhenTheThingBeingChasedHasGone:
    """The foreign keys cascade, so the row a queued chase points at may not be there.

    Being unable to check is not grounds for withholding a message the firm
    asked for — the practitioner composed it, and a missing filing or invoice
    says nothing about whether it should go out. Only a reason that can be read
    withdraws a chase.
    """

    def test_a_filing_that_no_longer_exists_is_not_a_reason_to_withhold(self, db):
        reminder = Reminder(
            compliance_item_id=uuid.uuid4(), extra={"kind": "document"}
        )
        assert reminder_service.withdrawn_reason(db, reminder) is None

    def test_an_invoice_that_no_longer_exists_is_not_a_reason_either(self, db):
        reminder = Reminder(invoice_id=uuid.uuid4(), extra={"kind": "payment"})
        assert reminder_service.withdrawn_reason(db, reminder) is None


class TestChasingAClientTheFirmHasStoppedActingFor:
    """A queued chase to an off-boarded client is one that can never go out.

    ``worker_tasks._deliverable`` joins the client row and requires
    ``is_active``, which is what stops a fortnight of already-queued mail going
    out in the name of a firm — or to a client — that has since been switched
    off. Nothing said so at the point of queueing, so the message was accepted
    with a 201 and an audit line saying the client had been written to, and then
    sat SCHEDULED for good: never sent, never failed, never withdrawn, and
    counted in the pending badge for ever.
    """

    def test_a_manual_reminder_for_an_off_boarded_client_is_refused(
        self, client, auth_headers, client_id
    ):
        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)

        response = client.post(
            "/api/v1/reminders",
            json={
                "client_id": client_id,
                "reminder_type": "custom",
                "subject": "Your outstanding fee",
                "body": "A quick note about the balance.",
            },
            headers=auth_headers,
        )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "off-boarded" in detail
        # The refusal names the way to do it, because chasing a departed client
        # for a last unpaid invoice is ordinary work.
        assert "reactivate" in detail.lower()

    def test_nothing_is_left_queued_by_the_refusal(
        self, client, auth_headers, client_id, db
    ):
        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)
        client.post(
            "/api/v1/reminders",
            json={
                "client_id": client_id,
                "reminder_type": "custom",
                "subject": "Your outstanding fee",
                "body": "A quick note about the balance.",
            },
            headers=auth_headers,
        )
        assert (
            db.scalar(
                select(func.count(Reminder.id)).where(
                    Reminder.client_id == uuid.UUID(client_id)
                )
            )
            == 0
        )

    def test_the_pending_badge_is_not_inflated_by_it(
        self, client, auth_headers, client_id
    ):
        """The badge is what a practitioner reads to know work is waiting.

        An undeliverable reminder counted in it permanently, so the number never
        came down and stopped meaning anything.
        """
        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)
        client.post(
            "/api/v1/reminders",
            json={
                "client_id": client_id,
                "reminder_type": "custom",
                "subject": "Your outstanding fee",
                "body": "A quick note about the balance.",
            },
            headers=auth_headers,
        )
        counts = client.get("/api/v1/reminders/pending-count", headers=auth_headers).json()
        assert counts["scheduled"] == 0

    def test_an_active_client_is_still_reachable(self, client, auth_headers, client_id):
        response = client.post(
            "/api/v1/reminders",
            json={
                "client_id": client_id,
                "reminder_type": "custom",
                "subject": "Your outstanding fee",
                "body": "A quick note about the balance.",
            },
            headers=auth_headers,
        )
        assert response.status_code == 201, response.text

    def test_taking_the_client_back_on_reopens_the_route(
        self, client, auth_headers, client_id
    ):
        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)
        client.patch(
            f"/api/v1/clients/{client_id}",
            json={"is_active": True},
            headers=auth_headers,
        )
        response = client.post(
            "/api/v1/reminders",
            json={
                "client_id": client_id,
                "reminder_type": "custom",
                "subject": "Your outstanding fee",
                "body": "A quick note about the balance.",
            },
            headers=auth_headers,
        )
        assert response.status_code == 201, response.text
