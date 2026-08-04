"""Billing: numbering, GST maths, generation from filed work, and payments."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.core import clock
from app.core.periods import fiscal_year_start
from app.models.base import InvoiceStatus
from app.models.compliance import ComplianceItem
from app.models.invoice import Invoice, InvoiceLine
from app.schemas.common import MAX_AMOUNT_PAISE
from app.schemas.invoice import MAX_ISSUE_DATE_LEAD_DAYS
from app.services import billing, firms
from tests.conftest import make_client_payload


def file_everything(client, auth_headers) -> list[dict]:
    """Mark every generated compliance item as filed, so it becomes billable."""
    calendar = client.get(
        "/api/v1/compliance/calendar",
        params={"from_date": "2020-01-01", "to_date": "2035-12-31", "limit": 1000},
        headers=auth_headers,
    ).json()
    items = calendar["items"]
    client.post(
        "/api/v1/compliance/items/bulk-status",
        json={
            "item_ids": [item["id"] for item in items],
            "status": "filed",
            "filed_on": clock.today().isoformat(),
        },
        headers=auth_headers,
    )
    return items


def make_invoice(client, auth_headers, client_id, **overrides):
    payload = {
        "client_id": client_id,
        "lines": [
            {"description": "GSTR-3B filing", "quantity": 1, "unit_price_paise": 200_000}
        ],
    }
    payload.update(overrides)
    return client.post("/api/v1/invoices", json=payload, headers=auth_headers)


class TestTotals:
    def test_gst_is_added_at_eighteen_percent(self):
        invoice = Invoice(gst_rate_bps=1800, lines=[])
        invoice.lines.append(
            InvoiceLine(description="x", quantity=2, unit_price_paise=150_000)
        )
        billing.recalculate(invoice)

        assert invoice.subtotal_paise == 300_000
        assert invoice.tax_paise == 54_000
        assert invoice.total_paise == 354_000

    def test_tax_rounds_half_up_in_paise(self):
        # 1 paisa at 18% = 0.18 paise, which must not silently truncate to 0.
        invoice = Invoice(gst_rate_bps=1800, lines=[])
        invoice.lines.append(InvoiceLine(description="x", quantity=1, unit_price_paise=3))
        billing.recalculate(invoice)
        assert invoice.tax_paise == 1  # 0.54 rounds to 1
        assert invoice.total_paise == 4

    def test_a_zero_rate_produces_no_tax(self):
        invoice = Invoice(gst_rate_bps=0, lines=[])
        invoice.lines.append(
            InvoiceLine(description="x", quantity=1, unit_price_paise=100_000)
        )
        billing.recalculate(invoice)
        assert invoice.tax_paise == 0
        assert invoice.total_paise == 100_000

    def test_balance_tracks_payments(self):
        invoice = Invoice(total_paise=100_000, amount_paid_paise=40_000)
        assert invoice.balance_paise == 60_000

    def test_a_whole_rupee_fee_routinely_bills_to_a_fraction_of_a_rupee(self):
        # 18% of ₹1,111 is ₹199.98, so the invoice totals ₹1,310.98. Nothing
        # here is unusual — GST lands off a whole rupee for every subtotal that
        # is not a multiple of ₹50 — and it is the reason anything downstream
        # that deals in whole rupees cannot settle an invoice exactly.
        invoice = Invoice(gst_rate_bps=1800, lines=[])
        invoice.lines.append(InvoiceLine(description="Advisory", quantity=1, unit_price_paise=111_100))
        billing.recalculate(invoice)
        assert invoice.tax_paise == 19_998
        assert invoice.total_paise == 131_098
        assert invoice.total_paise % 100 != 0

    def test_paying_the_balance_rounded_up_to_the_rupee_is_refused(self):
        # ₹1,311 against a ₹1,310.98 balance. Two paise over is still over, and
        # record_payment refuses anything over — see the endpoint saying so in
        # TestPayments.test_overpayment_is_refused. That is what makes a
        # rounded-up suggestion unsubmittable rather than merely imprecise.
        invoice = Invoice(total_paise=131_098, amount_paid_paise=0)
        rounded_up_to_whole_rupees = round(invoice.balance_paise / 100) * 100
        assert rounded_up_to_whole_rupees > invoice.balance_paise

    def test_paying_the_balance_rounded_down_leaves_it_unsettled(self):
        # ₹1,310 of a ₹1,310.98 balance: the invoice stays part-paid over 98
        # paise, and keeps appearing on the list of clients to chase.
        invoice = Invoice(
            total_paise=131_098, amount_paid_paise=131_000, status=InvoiceStatus.SENT
        )
        assert invoice.balance_paise == 98
        billing.refresh_status(invoice)
        assert invoice.status is InvoiceStatus.PARTIALLY_PAID


class TestNumbering:
    def test_numbers_are_sequential_within_the_financial_year(
        self, client, auth_headers, client_id
    ):
        numbers = [
            make_invoice(client, auth_headers, client_id).json()["invoice_number"]
            for _ in range(3)
        ]
        assert len({n.rsplit("/", 1)[0] for n in numbers}) == 1
        assert [n.rsplit("/", 1)[1] for n in numbers] == ["0001", "0002", "0003"]

    def test_the_financial_year_is_encoded(self, db, firm_id):
        number = billing.next_invoice_number(db, uuid.UUID(firm_id), date(2026, 7, 15))
        assert number == "INV/FY2026-27/0001"

    def test_january_belongs_to_the_previous_financial_year(self, db, firm_id):
        number = billing.next_invoice_number(db, uuid.UUID(firm_id), date(2027, 1, 15))
        assert number == "INV/FY2026-27/0001"

    def test_a_gap_in_the_sequence_does_not_collide(
        self, client, auth_headers, client_id, db
    ):
        first = make_invoice(client, auth_headers, client_id).json()
        second = make_invoice(client, auth_headers, client_id).json()
        # Cancel-and-delete the first, leaving 0002 in place with a count of 1.
        db.query(Invoice).filter(Invoice.id == uuid.UUID(first["id"])).delete()
        db.commit()

        third = make_invoice(client, auth_headers, client_id).json()
        assert third["invoice_number"] != second["invoice_number"]


class TestRedatingADraft:
    """A number carries its financial year, and a draft's date can still move.

    Raising invoices on 31 March and then dating them 1 April — deferring the
    revenue into the new year — is ordinary at the start of April. It used to
    leave an ``FY2025-26`` number on an invoice dated in FY2026-27, so two
    invoices dated in the same year sat in different series and the firm's
    books disagreed with its own numbering.
    """

    def test_moving_a_draft_into_the_next_year_moves_its_number(
        self, client, auth_headers, client_id
    ):
        draft = make_invoice(
            client, auth_headers, client_id, issue_date="2026-03-31"
        ).json()
        assert draft["invoice_number"] == "INV/FY2025-26/0001"

        updated = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            json={"issue_date": "2026-04-01"},
            headers=auth_headers,
        ).json()
        assert updated["invoice_number"] == "INV/FY2026-27/0001"

    def test_moving_a_draft_back_a_year_moves_its_number_back(
        self, client, auth_headers, client_id
    ):
        draft = make_invoice(
            client, auth_headers, client_id, issue_date="2026-04-05"
        ).json()
        assert draft["invoice_number"].startswith("INV/FY2026-27/")

        updated = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            json={"issue_date": "2026-02-20"},
            headers=auth_headers,
        ).json()
        assert updated["invoice_number"] == "INV/FY2025-26/0001"

    def test_the_new_number_does_not_collide_with_that_years_invoices(
        self, client, auth_headers, client_id
    ):
        """It takes the next free number in the year it is joining, not 0001."""
        existing = make_invoice(
            client, auth_headers, client_id, issue_date="2026-05-02"
        ).json()
        draft = make_invoice(
            client, auth_headers, client_id, issue_date="2026-03-31"
        ).json()

        updated = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            json={"issue_date": "2026-06-10"},
            headers=auth_headers,
        ).json()
        assert updated["invoice_number"] != existing["invoice_number"]
        assert updated["invoice_number"].startswith("INV/FY2026-27/")

    def test_a_date_inside_the_same_year_leaves_the_number_alone(
        self, client, auth_headers, client_id
    ):
        # Both ends derived from today rather than written down, so the pair
        # stays inside one financial year — and inside what
        # ``validate_issue_date`` will accept — whenever this is run.
        today = clock.today()
        year_start = date(fiscal_year_start(today), 4, 1)
        draft = make_invoice(
            client, auth_headers, client_id, issue_date=year_start.isoformat()
        ).json()
        updated = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            json={"issue_date": today.isoformat()},
            headers=auth_headers,
        ).json()
        assert updated["invoice_number"] == draft["invoice_number"]

    def test_an_edit_that_is_not_a_date_leaves_the_number_alone(
        self, client, auth_headers, client_id
    ):
        draft = make_invoice(
            client, auth_headers, client_id, issue_date="2026-03-31"
        ).json()
        updated = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            json={"notes": "Q4 advisory"},
            headers=auth_headers,
        ).json()
        assert updated["invoice_number"] == draft["invoice_number"]

    def test_a_renumbering_is_written_into_the_audit_trail(
        self, client, auth_headers, client_id
    ):
        """The number on a document changed; that is not something to do quietly."""
        draft = make_invoice(
            client, auth_headers, client_id, issue_date="2026-03-31"
        ).json()
        client.patch(
            f"/api/v1/invoices/{draft['id']}",
            json={"issue_date": "2026-04-01"},
            headers=auth_headers,
        )
        entries = client.get(
            "/api/v1/audit", params={"action": "invoice.update"}, headers=auth_headers
        ).json()["items"]
        assert entries
        assert entries[0]["changes"]["invoice_number"] == [
            "INV/FY2025-26/0001",
            "INV/FY2026-27/0001",
        ]

    def test_a_sent_invoice_is_refused_the_edit_before_any_of_this(
        self, client, auth_headers, client_id
    ):
        """The number is a document of record once it has gone out."""
        draft = make_invoice(
            client, auth_headers, client_id, issue_date="2026-03-31"
        ).json()
        client.post(f"/api/v1/invoices/{draft['id']}/send", headers=auth_headers)
        response = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            json={"issue_date": "2026-04-01"},
            headers=auth_headers,
        )
        assert response.status_code == 409
        after = client.get(
            f"/api/v1/invoices/{draft['id']}", headers=auth_headers
        ).json()
        assert after["invoice_number"] == draft["invoice_number"]


class TestTwoInvoicesAtOnce:
    """Numbering is a read followed by a write, and something fits in between.

    Two practitioners in one firm pressing *Create invoice* together both read
    the same set of used numbers and both pick the next one. One of them then
    lost the whole request to ``uq_invoice_firm_number`` — reported as "that
    change conflicts with an existing record", about an invoice number the user
    never chose and cannot see. A batch generate lost every draft in the run,
    not only the one that clashed.

    Two things close it. The firm's own row is held for the duration, which is
    what actually orders the requests on PostgreSQL; and the insert retries on
    a fresh number, which is both the belt to that braces and the whole
    mechanism on a backend without row locks.
    """

    @staticmethod
    def make_line():
        return InvoiceLine(description="Advisory", quantity=1, unit_price_paise=100_000)

    def build_for(self, firm_id, client_id):
        def build(number: str) -> Invoice:
            invoice = Invoice(
                firm_id=uuid.UUID(firm_id),
                client_id=uuid.UUID(client_id),
                invoice_number=number,
                issue_date=date(2026, 7, 1),
                gst_rate_bps=1800,
                status=InvoiceStatus.DRAFT,
            )
            invoice.lines.append(self.make_line())
            return billing.recalculate(invoice)

        return build

    def test_creating_an_invoice_holds_the_firm(
        self, client, auth_headers, client_id, firm_id, monkeypatch
    ):
        """The lock is worth nothing if the write path never takes it.

        That it is a lock the database honours is asserted in ``test_firms``;
        this is only that numbering asks for it.
        """
        locked = []
        monkeypatch.setattr(firms, "lock_firm", lambda db, fid: locked.append(fid))

        assert make_invoice(client, auth_headers, client_id).status_code == 201
        # Asked for, and only ever for this firm. Not *how many times*: the
        # line edit takes the same row for its own read-decide-write, and
        # re-acquiring a lock the transaction already holds costs nothing.
        assert locked
        assert set(locked) == {uuid.UUID(firm_id)}

    @staticmethod
    def committed_by_another_request(firm_id, client_id, number, issue_date=date(2026, 7, 1)):
        """Take ``number`` on a second connection, the way a second request does.

        A different connection is the whole point. A row written inside our own
        transaction goes back out again when the savepoint rolls the losing
        attempt back, so the retry finds the number free and the test proves
        nothing. Committed from outside, it stays taken — which is the
        situation being defended against.
        """
        from app.database import SessionLocal

        other = SessionLocal()
        try:
            other.add(
                Invoice(
                    firm_id=uuid.UUID(firm_id),
                    client_id=uuid.UUID(client_id),
                    invoice_number=number,
                    issue_date=issue_date,
                    gst_rate_bps=1800,
                    status=InvoiceStatus.DRAFT,
                )
            )
            other.commit()
        finally:
            other.close()

    def test_a_number_taken_between_the_read_and_the_write_is_given_up(
        self, db, firm_id, client_id, monkeypatch
    ):
        """The race itself, in the order it happens.

        We read the used numbers and settle on 0001. Another request commits
        0001. We then try to insert it. Before, that was the end of the
        request; now the number is given up and the next free one taken.

        Only the reading is staged — the number handed back is exactly what a
        request that read a moment earlier would have got. The clash at flush
        is a real unique-constraint violation against a row a real second
        connection really committed.
        """
        stale = billing.next_invoice_number(db, uuid.UUID(firm_id), date(2026, 7, 1))
        assert stale == "INV/FY2026-27/0001"
        db.rollback()  # SQLite will not let another connection write past a held read
        self.committed_by_another_request(firm_id, client_id, stale)

        offered = []
        real = billing.next_invoice_number

        def as_read_a_moment_ago(session, fid, issue_date=None):
            offered.append(1)
            return stale if len(offered) == 1 else real(session, fid, issue_date)

        monkeypatch.setattr(billing, "next_invoice_number", as_read_a_moment_ago)
        invoice = billing.insert_numbered(
            db,
            firm_id=uuid.UUID(firm_id),
            issue_date=date(2026, 7, 1),
            build=self.build_for(firm_id, client_id),
        )
        db.commit()

        assert len(offered) == 2
        assert invoice.invoice_number == "INV/FY2026-27/0002"
        # The other request keeps 0001; nothing was overwritten to make room.
        assert db.query(Invoice).count() == 2

    def test_the_abandoned_attempt_leaves_nothing_behind(
        self, db, firm_id, client_id, monkeypatch
    ):
        """A retry that left its lines or its half-written invoice in the
        session would bill the client twice for one piece of work."""
        used = "INV/FY2026-27/0001"
        db.add(
            Invoice(
                firm_id=uuid.UUID(firm_id),
                client_id=uuid.UUID(client_id),
                invoice_number=used,
                issue_date=date(2026, 7, 1),
                status=InvoiceStatus.DRAFT,
            )
        )
        db.commit()

        offered = []
        real = billing.next_invoice_number

        def offer_the_taken_one_first(session, fid, issue_date=None):
            offered.append(1)
            return used if len(offered) == 1 else real(session, fid, issue_date)

        monkeypatch.setattr(billing, "next_invoice_number", offer_the_taken_one_first)
        invoice = billing.insert_numbered(
            db,
            firm_id=uuid.UUID(firm_id),
            issue_date=date(2026, 7, 1),
            build=self.build_for(firm_id, client_id),
        )
        db.commit()

        assert len(offered) == 2
        assert invoice.invoice_number == "INV/FY2026-27/0002"
        # Two invoices, one line each — not three lines shared between two.
        assert db.query(Invoice).count() == 2
        assert db.query(InvoiceLine).count() == 1
        assert len(invoice.lines) == 1

    def test_a_violation_that_is_not_the_number_is_reported_as_it_happened(
        self, db, firm_id
    ):
        """Retrying a foreign-key violation four times would spend the attempts
        on something no new number can fix, then blame the numbering."""
        attempts = []

        def build(number: str) -> Invoice:
            attempts.append(number)
            raise IntegrityError(
                "INSERT INTO invoices ...", {}, Exception("FOREIGN KEY constraint failed")
            )

        with pytest.raises(IntegrityError) as raised:
            billing.insert_numbered(
                db, firm_id=uuid.UUID(firm_id), issue_date=date(2026, 7, 1), build=build
            )

        assert "FOREIGN KEY" in str(raised.value.orig)
        assert len(attempts) == 1

    def test_giving_up_surfaces_the_conflict_rather_than_looping(
        self, db, firm_id, client_id, monkeypatch
    ):
        """Contention this durable is not contention. It stops, and says so."""
        db.add(
            Invoice(
                firm_id=uuid.UUID(firm_id),
                client_id=uuid.UUID(client_id),
                invoice_number="INV/FY2026-27/0001",
                issue_date=date(2026, 7, 1),
                status=InvoiceStatus.DRAFT,
            )
        )
        db.commit()

        offered = []
        monkeypatch.setattr(
            billing,
            "next_invoice_number",
            lambda *a, **k: offered.append(1) or "INV/FY2026-27/0001",
        )

        with pytest.raises(IntegrityError):
            billing.insert_numbered(
                db,
                firm_id=uuid.UUID(firm_id),
                issue_date=date(2026, 7, 1),
                build=self.build_for(firm_id, client_id),
            )

        assert len(offered) == billing.NUMBER_ATTEMPTS

    @pytest.mark.parametrize(
        "message",
        [
            'duplicate key value violates unique constraint "uq_invoice_firm_number"',
            "UNIQUE constraint failed: invoices.firm_id, invoices.invoice_number",
        ],
        ids=["postgresql", "sqlite"],
    )
    def test_both_backends_say_taken_in_their_own_words(self, message):
        """The suite runs on SQLite and production runs on PostgreSQL, so the
        wording that matters most is the one never exercised here."""
        assert billing.number_collision(IntegrityError("...", {}, Exception(message)))

    @pytest.mark.parametrize(
        "message",
        [
            'insert or update on table "invoices" violates foreign key constraint',
            "UNIQUE constraint failed: clients.firm_id, clients.pan",
        ],
        ids=["foreign-key", "another-table"],
    )
    def test_nothing_else_is_mistaken_for_a_taken_number(self, message):
        assert not billing.number_collision(IntegrityError("...", {}, Exception(message)))

    def test_a_batch_generate_keeps_every_draft_it_started(
        self, client, auth_headers, client_id, firm_id, db, monkeypatch
    ):
        """The failure that cost the most: one clash used to roll back the run,
        so a firm that generated forty invoices got none of them."""
        file_everything(client, auth_headers)
        second = client.post(
            "/api/v1/clients", json=make_client_payload(name="Kaveri Foods", pan="AAECK4321P"),
            headers=auth_headers,
        )
        assert second.status_code == 201, second.text
        file_everything(client, auth_headers)

        stale = billing.next_invoice_number(db, uuid.UUID(firm_id))
        db.rollback()
        self.committed_by_another_request(
            firm_id, client_id, stale, issue_date=clock.today()
        )

        offered = []
        real = billing.next_invoice_number

        def as_read_a_moment_ago(session, fid, issue_date=None):
            offered.append(1)
            return stale if len(offered) == 1 else real(session, fid, issue_date)

        monkeypatch.setattr(billing, "next_invoice_number", as_read_a_moment_ago)
        response = client.post("/api/v1/invoices/generate", json={}, headers=auth_headers)

        assert response.status_code == 200, response.text
        body = response.json()
        # Both drafts survive: the clash costs one number, not the run.
        assert body["created"] == 2
        numbers = [inv["invoice_number"] for inv in body["invoices"]]
        assert len(set(numbers)) == 2
        assert stale not in numbers
        # And the work really is billed — a rolled-back run used to leave the
        # filings marked unbilled while the caller was told nothing had failed.
        assert client.get("/api/v1/invoices/billable", headers=auth_headers).json()[
            "total_items"
        ] == 0


class TestBillableWork:
    def test_filed_work_becomes_billable(self, client, auth_headers, client_id):
        file_everything(client, auth_headers)
        response = client.get("/api/v1/invoices/billable", headers=auth_headers)
        assert response.status_code == 200

        body = response.json()
        assert body["total_items"] > 0
        assert body["total_paise"] > 0
        assert body["clients"][0]["client_name"] == "Nimbus Textiles Pvt Ltd"

    def test_unfiled_work_is_not_billable(self, client, auth_headers, client_id):
        body = client.get("/api/v1/invoices/billable", headers=auth_headers).json()
        assert body["total_items"] == 0

    def test_invoicing_removes_work_from_the_billable_pool(
        self, client, auth_headers, client_id
    ):
        file_everything(client, auth_headers)
        client.post("/api/v1/invoices/generate", json={}, headers=auth_headers)

        body = client.get("/api/v1/invoices/billable", headers=auth_headers).json()
        assert body["total_items"] == 0


class TestGeneration:
    def test_drafts_one_invoice_per_client(self, client, auth_headers, client_id):
        file_everything(client, auth_headers)
        response = client.post("/api/v1/invoices/generate", json={}, headers=auth_headers)
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["created"] == 1
        assert body["total_paise"] > 0
        assert body["invoices"][0]["status"] == "draft"

    def test_a_second_run_finds_nothing_left(self, client, auth_headers, client_id):
        file_everything(client, auth_headers)
        client.post("/api/v1/invoices/generate", json={}, headers=auth_headers)
        again = client.post("/api/v1/invoices/generate", json={}, headers=auth_headers)
        assert again.json()["created"] == 0

    def test_lines_describe_the_filings_they_bill(self, client, auth_headers, client_id):
        file_everything(client, auth_headers)
        invoice_id = client.post(
            "/api/v1/invoices/generate", json={}, headers=auth_headers
        ).json()["invoices"][0]["id"]

        detail = client.get(f"/api/v1/invoices/{invoice_id}", headers=auth_headers).json()
        assert detail["lines"]
        assert all(line["compliance_item_id"] for line in detail["lines"])
        assert all("—" in line["description"] for line in detail["lines"])
        assert all(line["sac_code"] == "998222" for line in detail["lines"])

    def test_the_total_matches_the_sum_of_the_fees(self, client, auth_headers, client_id):
        file_everything(client, auth_headers)
        billable = client.get("/api/v1/invoices/billable", headers=auth_headers).json()
        generated = client.post(
            "/api/v1/invoices/generate", json={}, headers=auth_headers
        ).json()["invoices"][0]

        assert generated["subtotal_paise"] == billable["total_paise"]
        assert generated["total_paise"] == (
            generated["subtotal_paise"] + generated["tax_paise"]
        )

    def test_generating_for_an_unknown_client_is_a_404(self, client, auth_headers):
        response = client.post(
            "/api/v1/invoices/generate",
            json={"client_id": str(uuid.uuid4())},
            headers=auth_headers,
        )
        assert response.status_code == 404


class TestTwoGenerateRunsAtOnce:
    """Generation is a read, a decision and a write, and something fits between.

    Two practitioners pressing *Generate invoices* at the end of a month both
    read the same filed-but-unbilled filings. Numbering held the firm's row,
    but only from inside the insert — by then the decision had already been
    taken from a plain SELECT that nothing ordered. Marking a filing billed is
    not a claim the loser has to win: ``is_billed`` is already true and setting
    it again succeeds silently, so both runs completed and the client received
    two invoices for one piece of work.
    """

    def test_the_firm_is_held_before_the_billable_work_is_read(
        self, db, firm_id, monkeypatch
    ):
        """Ordering is the whole fix, so ordering is what is asserted.

        A lock taken after the read orders the writes and nothing else, which
        is exactly the state this replaced.
        """
        order = []
        monkeypatch.setattr(firms, "lock_firm", lambda session, fid: order.append("lock"))
        real_unbilled = billing.unbilled_items
        monkeypatch.setattr(
            billing,
            "unbilled_items",
            lambda *a, **kw: (order.append("read"), real_unbilled(*a, **kw))[1],
        )

        billing.generate_invoices_for_firm(db, uuid.UUID(firm_id))
        assert order[:2] == ["lock", "read"]

    def test_work_billed_while_we_waited_is_not_billed_again(
        self, client, auth_headers, db, firm_id, client_id
    ):
        """The interleaving itself, in the order it happens.

        The competing run is staged on the lock: it commits from a second
        connection at the moment this one takes the firm's row, which is the
        instant a real loser resumes at. Everything after that is the ordinary
        code path deciding what is left to bill.
        """
        file_everything(client, auth_headers)
        db.rollback()  # SQLite will not let another connection write past a held read

        from app.database import SessionLocal

        fired = []

        def winner_commits_first(session, fid):
            if fired:
                return
            fired.append(fid)
            other = SessionLocal()
            try:
                billing.generate_invoices_for_firm(other, uuid.UUID(firm_id))
                other.commit()
            finally:
                other.close()

        import unittest.mock as _mock

        with _mock.patch.object(firms, "lock_firm", winner_commits_first):
            billing.generate_invoices_for_firm(db, uuid.UUID(firm_id))
            db.commit()

        assert fired, "the competing run never happened"

        # Every filing is cited by exactly one live invoice, whichever run
        # raised it. Two lines against one filing is the client paying twice.
        rows = db.execute(
            select(InvoiceLine.compliance_item_id, func.count(InvoiceLine.id))
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .where(
                Invoice.firm_id == uuid.UUID(firm_id),
                Invoice.status != InvoiceStatus.CANCELLED,
                InvoiceLine.compliance_item_id.is_not(None),
            )
            .group_by(InvoiceLine.compliance_item_id)
        ).all()
        assert rows, "nothing was billed at all"
        duplicated = [str(item_id) for item_id, count in rows if count > 1]
        assert not duplicated, f"filings billed twice: {duplicated}"


class TestInvoiceLifecycle:
    def test_a_draft_can_be_edited(self, client, auth_headers, client_id):
        invoice = make_invoice(client, auth_headers, client_id).json()
        response = client.patch(
            f"/api/v1/invoices/{invoice['id']}",
            json={
                "lines": [
                    {"description": "Revised", "quantity": 2, "unit_price_paise": 250_000}
                ]
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["subtotal_paise"] == 500_000
        assert response.json()["lines"][0]["description"] == "Revised"

    def test_sending_sets_a_due_date_and_locks_editing(
        self, client, auth_headers, client_id
    ):
        invoice = make_invoice(client, auth_headers, client_id).json()
        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()
        assert sent["status"] == "sent"
        assert sent["due_date"] is not None

        blocked = client.patch(
            f"/api/v1/invoices/{invoice['id']}",
            json={"notes": "too late"},
            headers=auth_headers,
        )
        assert blocked.status_code == 409

    def test_an_invoice_cannot_be_sent_twice(self, client, auth_headers, client_id):
        invoice = make_invoice(client, auth_headers, client_id).json()
        client.post(f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers)
        again = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        )
        assert again.status_code == 409

    def test_a_past_due_date_shows_as_overdue(self, client, auth_headers, client_id):
        past = (clock.today() - timedelta(days=5)).isoformat()
        invoice = make_invoice(
            client, auth_headers, client_id, issue_date=past, due_date=past
        ).json()
        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()
        assert sent["status"] == "overdue"
        assert sent["days_overdue"] == 5

    def test_cancelling_returns_work_to_the_billable_pool(
        self, client, auth_headers, client_id
    ):
        file_everything(client, auth_headers)
        invoice = client.post(
            "/api/v1/invoices/generate", json={}, headers=auth_headers
        ).json()["invoices"][0]

        cancelled = client.post(
            f"/api/v1/invoices/{invoice['id']}/cancel", headers=auth_headers
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        billable = client.get("/api/v1/invoices/billable", headers=auth_headers).json()
        assert billable["total_items"] > 0


class TestPayments:
    def test_a_full_payment_marks_the_invoice_paid(self, client, auth_headers, client_id):
        invoice = make_invoice(client, auth_headers, client_id).json()
        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()

        response = client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"amount_paise": sent["total_paise"], "reference": "NEFT-9911"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "paid"
        assert response.json()["balance_paise"] == 0
        assert response.json()["payment_reference"] == "NEFT-9911"

    def test_a_part_payment_is_tracked(self, client, auth_headers, client_id):
        invoice = make_invoice(client, auth_headers, client_id).json()
        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()

        response = client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"amount_paise": sent["total_paise"] // 2},
            headers=auth_headers,
        )
        assert response.json()["status"] == "partially_paid"
        assert response.json()["balance_paise"] > 0

    def test_overpayment_is_refused(self, client, auth_headers, client_id):
        invoice = make_invoice(client, auth_headers, client_id).json()
        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()

        response = client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"amount_paise": sent["total_paise"] + 1},
            headers=auth_headers,
        )
        assert response.status_code == 409
        assert "exceeds the outstanding balance" in response.json()["detail"]

    def test_a_draft_cannot_take_a_payment(self, client, auth_headers, client_id):
        invoice = make_invoice(client, auth_headers, client_id).json()
        response = client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"amount_paise": 1000},
            headers=auth_headers,
        )
        assert response.status_code == 409
        assert "Send the invoice" in response.json()["detail"]

    def test_a_paid_invoice_cannot_be_cancelled(self, client, auth_headers, client_id):
        invoice = make_invoice(client, auth_headers, client_id).json()
        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()
        client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"amount_paise": sent["total_paise"]},
            headers=auth_headers,
        )
        response = client.post(
            f"/api/v1/invoices/{invoice['id']}/cancel", headers=auth_headers
        )
        assert response.status_code == 409

    def test_paying_an_overdue_invoice_clears_the_overdue_flag(
        self, client, auth_headers, client_id
    ):
        past = (clock.today() - timedelta(days=5)).isoformat()
        invoice = make_invoice(
            client, auth_headers, client_id, issue_date=past, due_date=past
        ).json()
        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()
        assert sent["status"] == "overdue"

        paid = client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"amount_paise": sent["total_paise"]},
            headers=auth_headers,
        ).json()
        assert paid["status"] == "paid"
        assert paid["days_overdue"] is None


class TestTwoReceiptsAtOnce:
    """A payment is a read, a decision and a write, and money rides on the gap.

    Recording one reads what has been paid so far, checks the new amount fits
    inside the balance, and writes the sum back. Nothing held the row across
    those three steps, so two ₹5,000 receipts against a ₹10,000 invoice —
    the practitioner who took the call and the one reconciling the bank feed,
    within the same minute — both read ``0`` paid, both passed the overpayment
    check, and both wrote ``5,000``. The client has paid in full; the firm's
    books say half, and go on chasing them for a balance already settled.
    """

    def test_the_row_lock_is_one_the_database_can_honour(self, db):
        """Compiled for PostgreSQL: SQLite drops the clause silently."""
        from sqlalchemy.dialects import postgresql

        statements = []
        original = db.scalars

        def record(statement, *args, **kwargs):
            statements.append(statement)
            return original(statement, *args, **kwargs)

        db.scalars = record
        try:
            billing.load_for_update(db, uuid.uuid4())
        finally:
            del db.scalars

        sql = str(statements[0].compile(dialect=postgresql.dialect()))
        assert "FROM invoices" in sql
        assert "FOR UPDATE" in sql
        # Narrowed to the one invoice: without the WHERE, every firm's
        # receipting would queue behind every other firm's.
        assert "WHERE invoices.id" in sql

    def test_the_locked_read_sees_a_receipt_the_session_has_not_heard_about(
        self, client, auth_headers, client_id, db
    ):
        """Holding the row is worth nothing if the balance predates the lock.

        Sessions here do not expire what they have loaded on commit, so a
        second read of a row already in the session is answered from memory —
        with the amounts as they were. That is the same stale balance a
        concurrent transaction reads from its own snapshot, and deciding an
        overpayment from it is how a receipt gets written over. The locked
        read is asked to repopulate what it loads for exactly that reason.
        """
        invoice = make_invoice(client, auth_headers, client_id).json()
        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()
        invoice_id = uuid.UUID(invoice["id"])
        total = sent["total_paise"]

        # Our copy, taken before the competing receipt exists. Held onto, or
        # the session lets go of it and the staleness never arises.
        ours = db.get(Invoice, invoice_id)
        assert ours.amount_paid_paise == 0
        db.commit()  # SQLite will not let another connection write past a read

        from app.database import SessionLocal

        other = SessionLocal()
        try:
            theirs = billing.load_for_update(other, invoice_id)
            billing.record_payment(theirs, amount_paise=total, reference="NEFT-1")
            other.commit()
        finally:
            other.close()

        # The plain read still answers with what we loaded first.
        assert db.get(Invoice, invoice_id).amount_paid_paise == 0
        # The locked one is what the balance is decided from.
        assert billing.load_for_update(db, invoice_id).amount_paid_paise == total

    def test_a_receipt_is_decided_from_the_locked_read(
        self, client, auth_headers, client_id, monkeypatch
    ):
        """The endpoint has to take its invoice through the lock, not past it.

        Asserted on the route rather than left to the service: the guard is a
        keyword on a shared lookup, and dropping it is a one-character
        regression that every other test here would still pass.
        """
        invoice = make_invoice(client, auth_headers, client_id).json()
        client.post(f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers)

        locked = []
        real = billing.load_for_update
        monkeypatch.setattr(
            billing,
            "load_for_update",
            lambda session, invoice_id: (locked.append(invoice_id), real(session, invoice_id))[1],
        )

        response = client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"amount_paise": 1000},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert locked == [uuid.UUID(invoice["id"])]


class TestAnInvoiceWithNothingToPay:
    """A waived fee is still an invoice, and it has to be able to settle.

    Firms raise nil invoices to put no-charge work on the record — a courtesy
    filing, a fee written off, work absorbed under a retainer. The line is
    allowed at zero, so the invoice totals zero, and it used to have no way
    out: ``record_payment`` refuses every amount as an overpayment, so nothing
    could move it, and the due date still dragged it to overdue. The firm was
    left chasing a client for nothing, permanently.
    """

    def test_a_nil_invoice_is_settled_once_it_is_sent(
        self, client, auth_headers, client_id
    ):
        invoice = make_invoice(
            client,
            auth_headers,
            client_id,
            lines=[{"description": "Written off", "quantity": 1, "unit_price_paise": 0}],
        ).json()
        assert invoice["total_paise"] == 0

        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()
        assert sent["status"] == "paid"
        assert sent["balance_paise"] == 0

    def test_a_nil_invoice_does_not_go_overdue(self, client, auth_headers, client_id):
        """There is nothing outstanding, so a past due date changes nothing."""
        past = (clock.today() - timedelta(days=30)).isoformat()
        invoice = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=past,
            due_date=past,
            lines=[{"description": "Courtesy filing", "quantity": 1, "unit_price_paise": 0}],
        ).json()
        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()
        assert sent["status"] == "paid"
        assert sent["days_overdue"] is None

    def test_the_overdue_sweep_leaves_a_nil_invoice_settled(
        self, client, auth_headers, client_id, db
    ):
        """The nightly sweep is the other way a status moves, and it agrees."""
        past = (clock.today() - timedelta(days=30)).isoformat()
        invoice = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=past,
            due_date=past,
            lines=[{"description": "Absorbed", "quantity": 1, "unit_price_paise": 0}],
        ).json()
        client.post(f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers)

        row = db.get(Invoice, uuid.UUID(invoice["id"]))
        billing.refresh_status(row, clock.today())
        assert row.status == InvoiceStatus.PAID

    def test_a_nil_invoice_is_not_chased_as_unpaid(self, client, auth_headers, client_id):
        past = (clock.today() - timedelta(days=30)).isoformat()
        invoice = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=past,
            due_date=past,
            lines=[{"description": "No charge", "quantity": 1, "unit_price_paise": 0}],
        ).json()
        client.post(f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers)

        unpaid = client.get(
            "/api/v1/invoices", params={"unpaid_only": True}, headers=auth_headers
        ).json()["items"]
        assert invoice["id"] not in [row["id"] for row in unpaid]

    def test_an_invoice_that_still_owes_something_is_untouched(
        self, client, auth_headers, client_id
    ):
        """The guard is about a zero total, not about being lenient generally."""
        past = (clock.today() - timedelta(days=5)).isoformat()
        invoice = make_invoice(
            client, auth_headers, client_id, issue_date=past, due_date=past
        ).json()
        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()
        assert sent["status"] == "overdue"

    def test_a_nil_invoice_still_refuses_a_payment(self, client, auth_headers, client_id):
        """Settled is not the same as collectable: there is nothing to receive."""
        invoice = make_invoice(
            client,
            auth_headers,
            client_id,
            lines=[{"description": "Written off", "quantity": 1, "unit_price_paise": 0}],
        ).json()
        client.post(f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers)

        response = client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"amount_paise": 100},
            headers=auth_headers,
        )
        assert response.status_code == 409

    def test_a_nil_invoice_can_still_be_cancelled(self, client, auth_headers, client_id):
        """Nothing was collected, so withdrawing it is still open to the firm."""
        invoice = make_invoice(
            client,
            auth_headers,
            client_id,
            lines=[{"description": "Raised in error", "quantity": 1, "unit_price_paise": 0}],
        ).json()
        client.post(f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers)

        response = client.post(
            f"/api/v1/invoices/{invoice['id']}/cancel", headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"


class TestRevenue:
    def test_reports_invoiced_collected_and_outstanding(
        self, client, auth_headers, client_id
    ):
        invoice = make_invoice(client, auth_headers, client_id).json()
        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()
        client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"amount_paise": 100_000},
            headers=auth_headers,
        )

        summary = client.get("/api/v1/invoices/revenue", headers=auth_headers).json()
        assert summary["invoiced_paise"] == sent["total_paise"]
        assert summary["collected_paise"] == 100_000
        assert summary["outstanding_paise"] == sent["total_paise"] - 100_000
        assert summary["by_client"]["Nimbus Textiles Pvt Ltd"] == sent["total_paise"]

    def test_drafts_are_counted_separately(self, client, auth_headers, client_id):
        draft = make_invoice(client, auth_headers, client_id).json()
        summary = client.get("/api/v1/invoices/revenue", headers=auth_headers).json()
        assert summary["draft_paise"] == draft["total_paise"]
        assert summary["invoiced_paise"] == 0

    def test_unbilled_work_is_surfaced(self, client, auth_headers, client_id):
        file_everything(client, auth_headers)
        summary = client.get("/api/v1/invoices/revenue", headers=auth_headers).json()
        assert summary["unbilled_paise"] > 0
        assert summary["by_category"]

    def test_an_inverted_window_is_rejected(self, client, auth_headers):
        response = client.get(
            "/api/v1/invoices/revenue",
            params={"from_date": "2026-06-01", "to_date": "2026-01-01"},
            headers=auth_headers,
        )
        assert response.status_code == 422


class TestHowFarAheadAnInvoiceMayBeDated:
    """An issue date decides far more than when the bill was written.

    It picks the number series, the due date is counted from it, and the
    revenue window is the current financial year. So a mistyped year produces
    an invoice that is numbered in FY2096-97, never goes overdue, never reaches
    the receivables list, is never chased, and does not appear on the billing
    screen the firm reads to find what is outstanding. The client has been
    billed and the only record of it sits outside every view the firm looks at.

    Bounded rather than closed, because dating a bill a little ahead is real
    work — deferring revenue into the new financial year on 31 March, or
    preparing a monthly retainer on the 28th dated the 1st. Both are days
    ahead; a mistyped year is a year ahead.
    """

    def _horizon(self) -> date:
        return clock.today() + timedelta(days=MAX_ISSUE_DATE_LEAD_DAYS)

    def test_a_mistyped_year_is_refused(self, client, auth_headers, client_id):
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=clock.today().replace(year=clock.today().year + 70).isoformat(),
        )
        assert response.status_code == 422, response.text
        assert "issue_date" in response.text

    def test_next_year_is_refused_too(self, client, auth_headers, client_id):
        """The typo that actually happens is one digit, not seven decades."""
        today = clock.today()
        response = make_invoice(
            client, auth_headers, client_id, issue_date=today.replace(year=today.year + 1).isoformat()
        )
        assert response.status_code == 422, response.text

    def test_the_day_past_the_horizon_is_refused(self, client, auth_headers, client_id):
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=(self._horizon() + timedelta(days=1)).isoformat(),
        )
        assert response.status_code == 422, response.text

    def test_the_horizon_itself_is_allowed(self, client, auth_headers, client_id):
        response = make_invoice(
            client, auth_headers, client_id, issue_date=self._horizon().isoformat()
        )
        assert response.status_code == 201, response.text

    def test_a_retainer_dated_next_month_still_goes_through(
        self, client, auth_headers, client_id
    ):
        """How an advance is billed: prepared now, dated the 1st."""
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=(clock.today() + timedelta(days=10)).isoformat(),
        )
        assert response.status_code == 201, response.text

    def test_back_dating_stays_open_with_no_bound_at_all(
        self, client, auth_headers, client_id
    ):
        """Entering last year's invoices while catching up on the books is
        ordinary, and a past date still lands in a series the firm can see."""
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=(clock.today() - timedelta(days=900)).isoformat(),
        )
        assert response.status_code == 201, response.text

    def test_the_edit_path_is_bounded_too(self, client, auth_headers, client_id):
        """The more dangerous of the two doors: moving a draft's issue date
        re-numbers it into the series that date belongs to, so a mistyped year
        both hides the invoice and burns a number decades out."""
        draft = make_invoice(client, auth_headers, client_id).json()

        response = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            headers=auth_headers,
            json={"issue_date": (self._horizon() + timedelta(days=1)).isoformat()},
        )
        assert response.status_code == 422, response.text

    def test_a_refused_edit_burns_no_number(self, client, auth_headers, client_id):
        draft = make_invoice(client, auth_headers, client_id).json()
        client.patch(
            f"/api/v1/invoices/{draft['id']}",
            headers=auth_headers,
            json={"issue_date": (self._horizon() + timedelta(days=400)).isoformat()},
        )

        after = client.get(f"/api/v1/invoices/{draft['id']}", headers=auth_headers).json()
        assert after["invoice_number"] == draft["invoice_number"]
        assert after["issue_date"] == draft["issue_date"]

    def test_the_generate_path_is_bounded_too(self, client, auth_headers, client_id):
        """The widest door: one mistyped date raises a future-dated invoice for
        every client with unbilled work at once."""
        file_everything(client, auth_headers)

        response = client.post(
            "/api/v1/invoices/generate",
            headers=auth_headers,
            json={"issue_date": (self._horizon() + timedelta(days=1)).isoformat()},
        )
        assert response.status_code == 422, response.text

    def test_the_refusal_names_the_last_date_it_would_accept(
        self, client, auth_headers, client_id
    ):
        """A bare "not allowed" leaves a practitioner guessing at the rule."""
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=(self._horizon() + timedelta(days=5)).isoformat(),
        )
        assert response.status_code == 422
        assert f"{self._horizon():%d %b %Y}" in response.text

    def test_a_refused_invoice_is_not_stored(self, client, auth_headers, client_id):
        make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=(self._horizon() + timedelta(days=200)).isoformat(),
        )

        listing = client.get("/api/v1/invoices", headers=auth_headers).json()
        assert listing["items"] == []


class TestTellingTwoClientsOfTheSameNameApart:
    """``by_client`` was keyed by name and accumulated by name.

    A client's name is not unique and nothing pretends it is — only the PAN is,
    and that is optional. Two entities of one family or group under one trading
    name is ordinary, and so is the same name typed twice by mistake. Either
    way the breakdown showed one of them carrying both revenues and the other
    missing entirely: one client appears to owe twice what they do and another
    to have been billed nothing all year, which is exactly the conclusion that
    gets acted on.
    """

    def _twin(self, client, auth_headers, **overrides) -> str:
        payload = make_client_payload(**overrides)
        response = client.post("/api/v1/clients", json=payload, headers=auth_headers)
        assert response.status_code == 201, response.text
        return response.json()["client"]["id"]

    def _send(self, client, auth_headers, client_id, unit_price_paise) -> dict:
        invoice = make_invoice(
            client,
            auth_headers,
            client_id,
            lines=[
                {"description": "Annual filing", "quantity": 1, "unit_price_paise": unit_price_paise}
            ],
        ).json()
        return client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()

    def _revenue(self, client, auth_headers) -> dict:
        return client.get("/api/v1/invoices/revenue", headers=auth_headers).json()

    def test_one_client_of_a_name_is_not_qualified(self, client, auth_headers, client_id):
        """The ordinary breakdown is untouched — the qualifier appears only
        where a human would otherwise have to guess."""
        sent = self._send(client, auth_headers, client_id, 200_000)

        by_client = self._revenue(client, auth_headers)["by_client"]
        assert by_client == {"Nimbus Textiles Pvt Ltd": sent["total_paise"]}

    def test_two_clients_of_one_name_are_not_added_together(
        self, client, auth_headers, client_id
    ):
        twin = self._twin(
            client, auth_headers, pan="AABCN9876Q", gstin="29AABCN9876Q1Z8", gst_registered=False
        )
        first = self._send(client, auth_headers, client_id, 200_000)
        second = self._send(client, auth_headers, twin, 500_000)
        assert first["total_paise"] != second["total_paise"]

        by_client = self._revenue(client, auth_headers)["by_client"]
        assert len(by_client) == 2
        assert sorted(by_client.values()) == sorted(
            [first["total_paise"], second["total_paise"]]
        )

    def test_the_pan_is_what_tells_them_apart(self, client, auth_headers, client_id):
        """What a practitioner would reach for to separate two clients."""
        twin = self._twin(
            client, auth_headers, pan="AABCN9876Q", gstin="29AABCN9876Q1Z8", gst_registered=False
        )
        self._send(client, auth_headers, client_id, 200_000)
        self._send(client, auth_headers, twin, 500_000)

        by_client = self._revenue(client, auth_headers)["by_client"]
        assert "Nimbus Textiles Pvt Ltd (AABCN2345P)" in by_client
        assert "Nimbus Textiles Pvt Ltd (AABCN9876Q)" in by_client

    def test_a_twin_with_nothing_billed_does_not_qualify_the_other(
        self, client, auth_headers, client_id
    ):
        """The collision is judged over the clients in the report, not over the
        firm's whole book. A namesake with no revenue in the window is not on
        this breakdown at all, so there is nothing for a reader to confuse the
        row with — and qualifying it would put a PAN on a line that never
        needed one."""
        self._twin(
            client, auth_headers, pan="AABCN9876Q", gstin="29AABCN9876Q1Z8", gst_registered=False
        )
        sent = self._send(client, auth_headers, client_id, 200_000)

        by_client = self._revenue(client, auth_headers)["by_client"]
        assert by_client == {"Nimbus Textiles Pvt Ltd": sent["total_paise"]}

    def test_a_client_with_no_pan_still_gets_a_distinct_label(
        self, client, auth_headers, client_id
    ):
        """The PAN is optional, so the fallback has to be stable and distinct
        even when there is nothing to qualify with."""
        twin = self._twin(client, auth_headers, pan=None, gstin=None, gst_registered=False)
        self._send(client, auth_headers, client_id, 200_000)
        self._send(client, auth_headers, twin, 500_000)

        by_client = self._revenue(client, auth_headers)["by_client"]
        assert len(by_client) == 2
        assert f"Nimbus Textiles Pvt Ltd ({twin[:8]})" in by_client

    def test_a_third_of_the_same_name_is_still_separated(
        self, client, auth_headers, client_id
    ):
        second = self._twin(
            client, auth_headers, pan="AABCN9876Q", gstin="29AABCN9876Q1Z8", gst_registered=False
        )
        third = self._twin(
            client, auth_headers, pan="AABCN1111R", gstin="24AABCN1111R1Z3", gst_registered=False
        )
        self._send(client, auth_headers, client_id, 100_000)
        self._send(client, auth_headers, second, 200_000)
        self._send(client, auth_headers, third, 300_000)

        by_client = self._revenue(client, auth_headers)["by_client"]
        assert len(by_client) == 3

    def test_each_label_carries_that_clients_own_revenue(
        self, client, auth_headers, client_id
    ):
        twin = self._twin(
            client, auth_headers, pan="AABCN9876Q", gstin="29AABCN9876Q1Z8", gst_registered=False
        )
        first = self._send(client, auth_headers, client_id, 200_000)
        second = self._send(client, auth_headers, twin, 500_000)

        by_client = self._revenue(client, auth_headers)["by_client"]
        assert by_client["Nimbus Textiles Pvt Ltd (AABCN2345P)"] == first["total_paise"]
        assert by_client["Nimbus Textiles Pvt Ltd (AABCN9876Q)"] == second["total_paise"]

    def test_a_clients_own_invoices_are_still_summed(
        self, client, auth_headers, client_id
    ):
        """Splitting by id must not split one client across its own bills."""
        twin = self._twin(
            client, auth_headers, pan="AABCN9876Q", gstin="29AABCN9876Q1Z8", gst_registered=False
        )
        one = self._send(client, auth_headers, client_id, 200_000)
        two = self._send(client, auth_headers, client_id, 300_000)
        self._send(client, auth_headers, twin, 500_000)

        by_client = self._revenue(client, auth_headers)["by_client"]
        assert by_client["Nimbus Textiles Pvt Ltd (AABCN2345P)"] == (
            one["total_paise"] + two["total_paise"]
        )

    def test_the_total_is_unchanged_by_the_split(self, client, auth_headers, client_id):
        twin = self._twin(
            client, auth_headers, pan="AABCN9876Q", gstin="29AABCN9876Q1Z8", gst_registered=False
        )
        first = self._send(client, auth_headers, client_id, 200_000)
        second = self._send(client, auth_headers, twin, 500_000)

        summary = self._revenue(client, auth_headers)
        assert sum(summary["by_client"].values()) == summary["invoiced_paise"]
        assert summary["invoiced_paise"] == first["total_paise"] + second["total_paise"]


class TestAccessControl:
    def test_invoices_require_authentication(self, client):
        assert client.get("/api/v1/invoices").status_code == 401

    def test_another_firms_invoice_is_a_404(self, client, auth_headers):
        response = client.get(f"/api/v1/invoices/{uuid.uuid4()}", headers=auth_headers)
        assert response.status_code == 404

    def test_a_junior_cannot_create_invoices(self, client, auth_headers, client_id):
        client.post(
            "/api/v1/auth/practitioners",
            json={
                "full_name": "Junior Jain",
                "email": "junior@sharma-ca.in",
                "password": "another-long-password",
                "role": "junior",
            },
            headers=auth_headers,
        )
        token = client.post(
            "/api/v1/auth/login",
            json={"email": "junior@sharma-ca.in", "password": "another-long-password"},
        ).json()["access_token"]

        response = make_invoice(
            client, {"Authorization": f"Bearer {token}"}, client_id
        )
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "status_filter", [InvoiceStatus.DRAFT.value, InvoiceStatus.SENT.value]
    )
    def test_lists_filter_by_status(self, client, auth_headers, client_id, status_filter):
        draft = make_invoice(client, auth_headers, client_id).json()
        second = make_invoice(client, auth_headers, client_id).json()
        client.post(f"/api/v1/invoices/{second['id']}/send", headers=auth_headers)

        body = client.get(
            "/api/v1/invoices",
            params={"invoice_status": status_filter},
            headers=auth_headers,
        ).json()
        assert body["total"] == 1
        assert body["items"][0]["status"] == status_filter
        assert draft["id"] is not None


class TestLinesCitingFilings:
    """A hand-written line may cite a filing, and citing one has consequences.

    Generated lines have always marked the work billed. These cover the other
    way in — an ad-hoc invoice, or an edited draft — where citing a filing has
    to mean the same thing, or the firm bills the same work twice.
    """

    @staticmethod
    def _billable(client, auth_headers) -> dict:
        return client.get("/api/v1/invoices/billable", headers=auth_headers).json()

    @staticmethod
    def _lines_of(invoice: dict) -> list[dict]:
        """The invoice's own lines, in the shape a PATCH sends back."""
        return [
            {
                "description": line["description"],
                "quantity": line["quantity"],
                "unit_price_paise": line["unit_price_paise"],
                "compliance_item_id": line["compliance_item_id"],
            }
            for line in invoice["lines"]
        ]

    def test_citing_a_filing_takes_it_out_of_the_billable_pile(
        self, client, auth_headers, client_id
    ):
        file_everything(client, auth_headers)
        before = self._billable(client, auth_headers)
        cited = before["clients"][0]["items"][0]

        response = make_invoice(
            client,
            auth_headers,
            client_id,
            lines=[
                {
                    "description": "Agreed fee for the year",
                    "quantity": 1,
                    "unit_price_paise": 500_000,
                    "compliance_item_id": cited["compliance_item_id"],
                }
            ],
        )
        assert response.status_code == 201

        after = self._billable(client, auth_headers)
        assert after["total_items"] == before["total_items"] - 1
        assert cited["compliance_item_id"] not in [
            item["compliance_item_id"]
            for group in after["clients"]
            for item in group["items"]
        ]

    def test_two_invoices_cannot_cite_the_same_filing(
        self, client, auth_headers, client_id
    ):
        file_everything(client, auth_headers)
        cited = self._billable(client, auth_headers)["clients"][0]["items"][0]
        line = {
            "description": "Billed once",
            "quantity": 1,
            "unit_price_paise": 100_000,
            "compliance_item_id": cited["compliance_item_id"],
        }

        assert make_invoice(client, auth_headers, client_id, lines=[line]).status_code == 201
        second = make_invoice(client, auth_headers, client_id, lines=[line])
        assert second.status_code == 409
        assert "already on another invoice" in second.json()["detail"]

    def test_resending_a_drafts_own_lines_does_not_release_its_work(
        self, client, auth_headers, client_id
    ):
        """The round-trip an editor makes: read the draft, PATCH it back.

        The draft still bills that work, so it must not reappear as billable —
        that is how the same filing ends up on a second invoice.
        """
        file_everything(client, auth_headers)
        generated = client.post(
            "/api/v1/invoices/generate", json={}, headers=auth_headers
        ).json()["invoices"][0]
        draft = client.get(
            f"/api/v1/invoices/{generated['id']}", headers=auth_headers
        ).json()
        assert self._billable(client, auth_headers)["total_items"] == 0

        response = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            json={"lines": self._lines_of(draft)},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert self._billable(client, auth_headers)["total_items"] == 0

    def test_dropping_a_line_returns_that_filing_to_the_billable_pile(
        self, client, auth_headers, client_id
    ):
        file_everything(client, auth_headers)
        generated = client.post(
            "/api/v1/invoices/generate", json={}, headers=auth_headers
        ).json()["invoices"][0]
        draft = client.get(
            f"/api/v1/invoices/{generated['id']}", headers=auth_headers
        ).json()
        kept = self._lines_of(draft)[:-1]

        client.patch(
            f"/api/v1/invoices/{draft['id']}",
            json={"lines": kept},
            headers=auth_headers,
        )
        assert self._billable(client, auth_headers)["total_items"] == 1

    def test_editing_a_draft_holds_the_firm_while_it_claims_a_filing(
        self, client, auth_headers, client_id, firm_id, monkeypatch
    ):
        """Reading ``is_billed`` and then writing it has to be one step.

        Creating an invoice already ran inside the lock ``insert_numbered``
        takes. Editing a draft did not, so two managers each adding the same
        filed return to their own draft both read it unbilled, both claimed it,
        and both invoices went out citing the one filing.
        """
        file_everything(client, auth_headers)
        cited = self._billable(client, auth_headers)["clients"][0]["items"][0]
        draft = make_invoice(client, auth_headers, client_id).json()

        locked = []
        monkeypatch.setattr(firms, "lock_firm", lambda session, fid: locked.append(fid))

        response = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            json={
                "lines": [
                    {
                        "description": "Agreed fee",
                        "quantity": 1,
                        "unit_price_paise": 500_000,
                        "compliance_item_id": cited["compliance_item_id"],
                    }
                ]
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert uuid.UUID(firm_id) in locked

    def test_a_line_may_cite_no_filing_at_all(self, client, auth_headers, client_id):
        """Ad-hoc work — advisory, a certificate — has no compliance item."""
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            lines=[
                {
                    "description": "Advisory on the new TDS rates",
                    "quantity": 3,
                    "unit_price_paise": 150_000,
                    "sac_code": "998311",
                }
            ],
        )
        assert response.status_code == 201
        body = response.json()
        assert body["subtotal_paise"] == 450_000
        assert body["lines"][0]["compliance_item_id"] is None
        assert body["lines"][0]["sac_code"] == "998311"

    def test_a_filing_belonging_to_another_client_is_a_404(
        self, client, auth_headers, client_id
    ):
        """Same firm, wrong client — the fee would land on the wrong ledger."""
        other = client.post(
            "/api/v1/clients",
            json=make_client_payload(
                name="Meridian Exports LLP", pan="AACCM7788K", gstin="27AACCM7788K1Z9"
            ),
            headers=auth_headers,
        ).json()["client"]
        their_item = client.get(
            "/api/v1/compliance/calendar",
            params={"client_id": other["id"], "limit": 1},
            headers=auth_headers,
        ).json()["items"][0]

        response = make_invoice(
            client,
            auth_headers,
            client_id,
            lines=[
                {
                    "description": "Wrong ledger",
                    "quantity": 1,
                    "unit_price_paise": 100_000,
                    "compliance_item_id": their_item["id"],
                }
            ],
        )
        assert response.status_code == 404

    def test_a_filing_belonging_to_another_firm_is_a_404(
        self, client, auth_headers, client_id
    ):
        """A compliance item is addressable by id alone, so this is the guard
        that stops one firm reaching into another's records — cancelling such
        an invoice would write ``is_billed`` onto their row."""
        outsider = client.post(
            "/api/v1/auth/register",
            json={
                "firm_name": "Meridian & Co",
                "icai_registration_number": "998877W",
                "firm_email": "office@meridian-ca.in",
                "pan": "AAACM7788K",
                "owner_full_name": "Vikram Rao",
                "owner_email": "vikram@meridian-ca.in",
                "owner_password": "another-correct-horse",
            },
        ).json()
        outsider_headers = {"Authorization": f"Bearer {outsider['access_token']}"}
        their_client = client.post(
            "/api/v1/clients",
            json=make_client_payload(pan="AAECM3456L", gstin="27AAECM3456L1Z2"),
            headers=outsider_headers,
        ).json()["client"]
        their_item = client.get(
            "/api/v1/compliance/calendar",
            params={"client_id": their_client["id"], "limit": 1},
            headers=outsider_headers,
        ).json()["items"][0]

        response = make_invoice(
            client,
            auth_headers,
            client_id,
            lines=[
                {
                    "description": "Reaching into another firm",
                    "quantity": 1,
                    "unit_price_paise": 100_000,
                    "compliance_item_id": their_item["id"],
                }
            ],
        )
        assert response.status_code == 404

        # The refusal is total: no half-built invoice is left behind.
        ours = client.get("/api/v1/invoices", headers=auth_headers).json()
        assert ours["total"] == 0

    def test_an_unknown_filing_id_is_a_404(self, client, auth_headers, client_id):
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            lines=[
                {
                    "description": "Nothing there",
                    "quantity": 1,
                    "unit_price_paise": 100_000,
                    "compliance_item_id": str(uuid.uuid4()),
                }
            ],
        )
        assert response.status_code == 404


class TestReleasingWorkACancelledInvoiceNoLongerOwns:
    """``is_billed`` does not record *which* invoice set it.

    So an invoice releasing every filing it happens to name can clear a claim
    that has since moved on: cancel an invoice, put the freed work on a second
    one, cancel the first again, and that filing is billable once more while
    the second invoice is still charging for it. Invoice it again and the
    client pays twice for one filing — the exact leakage the ``is_billed``
    flag exists to prevent, running backwards.
    """

    @staticmethod
    def _billable_ids(client, auth_headers) -> list[str]:
        body = client.get("/api/v1/invoices/billable", headers=auth_headers).json()
        return [item["compliance_item_id"] for g in body["clients"] for item in g["items"]]

    def _one_filing(self, client, auth_headers) -> str:
        file_everything(client, auth_headers)
        return self._billable_ids(client, auth_headers)[0]

    @staticmethod
    def _citing(item_id: str) -> dict:
        return {
            "description": "Annual filing, agreed fee",
            "quantity": 1,
            "unit_price_paise": 100_000,
            "compliance_item_id": item_id,
        }

    def _invoice_citing(self, client, auth_headers, client_id, item_id) -> dict:
        response = make_invoice(
            client, auth_headers, client_id, lines=[self._citing(item_id)]
        )
        assert response.status_code == 201
        return response.json()

    def test_cancelling_an_invoice_twice_is_refused(
        self, client, auth_headers, client_id
    ):
        """Told plainly, the way an already-sent invoice is."""
        invoice = make_invoice(client, auth_headers, client_id).json()
        client.post(f"/api/v1/invoices/{invoice['id']}/cancel", headers=auth_headers)

        second = client.post(
            f"/api/v1/invoices/{invoice['id']}/cancel", headers=auth_headers
        )

        assert second.status_code == 409
        assert "already been cancelled" in second.json()["detail"]

    def test_a_second_cancel_does_not_hand_back_work_another_invoice_bills(
        self, client, auth_headers, client_id
    ):
        item_id = self._one_filing(client, auth_headers)
        first = self._invoice_citing(client, auth_headers, client_id, item_id)
        client.post(f"/api/v1/invoices/{first['id']}/cancel", headers=auth_headers)
        self._invoice_citing(client, auth_headers, client_id, item_id)

        client.post(f"/api/v1/invoices/{first['id']}/cancel", headers=auth_headers)

        assert item_id not in self._billable_ids(client, auth_headers)

    def test_the_work_cannot_then_be_put_on_a_third_invoice(
        self, client, auth_headers, client_id, db
    ):
        """What the leak actually costs: the same filing billed twice.

        The service is asked directly, because the route now refuses the second
        cancel outright — this is the guard underneath that one, which has to
        hold on its own for any caller that reaches it another way.
        """
        item_id = self._one_filing(client, auth_headers)
        first = self._invoice_citing(client, auth_headers, client_id, item_id)
        client.post(f"/api/v1/invoices/{first['id']}/cancel", headers=auth_headers)
        self._invoice_citing(client, auth_headers, client_id, item_id)

        stale = db.get(Invoice, uuid.UUID(first["id"]))
        released = billing.release_items(db, stale)
        db.commit()

        assert released == 0
        third = make_invoice(
            client, auth_headers, client_id, lines=[self._citing(item_id)]
        )
        assert third.status_code == 409
        assert "already on another invoice" in third.json()["detail"]

    def test_cancelling_still_frees_the_work_it_was_the_last_to_bill(
        self, client, auth_headers, client_id
    ):
        """The guard withholds a claim that moved on, not every claim."""
        item_id = self._one_filing(client, auth_headers)
        invoice = self._invoice_citing(client, auth_headers, client_id, item_id)
        assert item_id not in self._billable_ids(client, auth_headers)

        response = client.post(
            f"/api/v1/invoices/{invoice['id']}/cancel", headers=auth_headers
        )

        assert response.status_code == 200
        assert item_id in self._billable_ids(client, auth_headers)

    def test_a_cancelled_invoice_does_not_hold_work_hostage(
        self, client, auth_headers, client_id, db
    ):
        """Cancelled invoices keep their lines, and must not count as claimants.

        Otherwise the first cancel frees the filing and every later one is
        blocked by the corpse of the first — work no one bills, that no one
        can bill again.
        """
        item_id = self._one_filing(client, auth_headers)
        first = self._invoice_citing(client, auth_headers, client_id, item_id)
        client.post(f"/api/v1/invoices/{first['id']}/cancel", headers=auth_headers)
        second = self._invoice_citing(client, auth_headers, client_id, item_id)

        client.post(f"/api/v1/invoices/{second['id']}/cancel", headers=auth_headers)

        assert item_id in self._billable_ids(client, auth_headers)

    def test_the_audit_note_counts_only_the_filings_actually_freed(
        self, client, auth_headers, client_id
    ):
        """A practitioner reads that line to know what went back on the pile."""
        file_everything(client, auth_headers)
        generated = client.post(
            "/api/v1/invoices/generate", json={}, headers=auth_headers
        ).json()["invoices"][0]
        expected = len(
            client.get(f"/api/v1/invoices/{generated['id']}", headers=auth_headers)
            .json()["lines"]
        )

        client.post(f"/api/v1/invoices/{generated['id']}/cancel", headers=auth_headers)

        entries = client.get(
            "/api/v1/audit", params={"action": "invoice.cancel"}, headers=auth_headers
        ).json()["items"]
        assert f"{expected} filing(s) returned to unbilled" in entries[0]["summary"]

    def test_a_release_cannot_reach_another_firms_filing(
        self, client, auth_headers, client_id, db
    ):
        """The same guard ``referenced_items`` puts on the way in.

        Nothing routable builds such a line today; this is the store refusing
        to write ``is_billed`` onto a row that is not ours even if one appears.
        """
        outsider = client.post(
            "/api/v1/auth/register",
            json={
                "firm_name": "Meridian & Co",
                "icai_registration_number": "998877W",
                "firm_email": "office@meridian-ca.in",
                "pan": "AAACM7788K",
                "owner_full_name": "Vikram Rao",
                "owner_email": "vikram@meridian-ca.in",
                "owner_password": "another-correct-horse",
            },
        ).json()
        outsider_headers = {"Authorization": f"Bearer {outsider['access_token']}"}
        their_client = client.post(
            "/api/v1/clients",
            json=make_client_payload(pan="AAECM3456L", gstin="27AAECM3456L1Z2"),
            headers=outsider_headers,
        ).json()["client"]
        their_item_id = uuid.UUID(
            client.get(
                "/api/v1/compliance/calendar",
                params={"client_id": their_client["id"], "limit": 1},
                headers=outsider_headers,
            ).json()["items"][0]["id"]
        )
        their_item = db.get(ComplianceItem, their_item_id)
        their_item.is_billed = True
        db.commit()

        ours = db.get(Invoice, uuid.UUID(make_invoice(client, auth_headers, client_id).json()["id"]))
        ours.lines[0].compliance_item_id = their_item_id
        db.flush()

        assert billing.release_items(db, ours) == 0
        db.rollback()
        assert db.get(ComplianceItem, their_item_id).is_billed is True


class TestAmountBounds:
    """Money columns are 64-bit; an absurd amount must be a 422, not a 500.

    Without an upper bound the arithmetic — amount × quantity × lines, plus
    GST — overflows BIGINT, and the caller gets a database error instead of a
    message naming the field they got wrong.
    """

    OVER = MAX_AMOUNT_PAISE + 1

    def test_a_line_priced_beyond_the_ceiling_is_refused(
        self, client, auth_headers, client_id
    ):
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            lines=[
                {"description": "Absurd", "quantity": 1, "unit_price_paise": self.OVER}
            ],
        )
        assert response.status_code == 422
        assert "unit_price_paise" in response.text

    def test_the_ceiling_itself_is_accepted(self, client, auth_headers, client_id):
        """The bound is a guard rail, so the largest allowed amount still works."""
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            lines=[
                {
                    "description": "At the ceiling",
                    "quantity": 1,
                    "unit_price_paise": MAX_AMOUNT_PAISE,
                }
            ],
        )
        assert response.status_code == 201
        assert response.json()["subtotal_paise"] == MAX_AMOUNT_PAISE

    def test_the_worst_case_invoice_still_fits_in_the_column(self):
        """200 lines, 10,000 each, at the ceiling, plus GST — inside BIGINT."""
        subtotal = 200 * 10_000 * MAX_AMOUNT_PAISE
        total = subtotal + (subtotal * 10_000 + 5_000) // 10_000
        assert total < 2**63 - 1

    def test_a_payment_beyond_the_ceiling_is_refused(
        self, client, auth_headers, client_id
    ):
        invoice = make_invoice(client, auth_headers, client_id).json()
        client.post(f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers)

        response = client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"amount_paise": self.OVER},
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_a_filing_fee_beyond_the_ceiling_is_refused(
        self, client, auth_headers, client_id
    ):
        """A fee reaches an invoice line through generation, so it needs the
        same ceiling — otherwise the overflow just arrives one step later."""
        item = client.get(
            "/api/v1/compliance/calendar", params={"limit": 1}, headers=auth_headers
        ).json()["items"][0]

        response = client.patch(
            f"/api/v1/compliance/items/{item['id']}",
            json={"fee_paise": self.OVER},
            headers=auth_headers,
        )
        assert response.status_code == 422


class TestOnlyABillThatIsOwedCanBeLate:
    """``days_overdue`` is what the billing table renders as "N days late".

    A draft and a cancelled invoice both keep a due date and an unpaid
    balance, so lateness derived from those two fields alone reported one
    against a bill the client was never asked to pay. The same pair
    ``refresh_status`` refuses to touch, and for the same reason.
    """

    def _overdue_draft(self, client, auth_headers, client_id: str) -> dict:
        long_ago = (clock.today() - timedelta(days=90)).isoformat()
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=long_ago,
            due_date=long_ago,
        )
        assert response.status_code == 201, response.text
        return response.json()

    def test_an_unsent_draft_is_not_late(self, client, auth_headers, client_id: str):
        draft = self._overdue_draft(client, auth_headers, client_id)

        assert draft["status"] == "draft"
        assert draft["days_overdue"] is None

    def test_a_cancelled_invoice_is_not_late(self, client, auth_headers, client_id: str):
        draft = self._overdue_draft(client, auth_headers, client_id)
        client.post(f"/api/v1/invoices/{draft['id']}/send", headers=auth_headers)

        cancelled = client.post(
            f"/api/v1/invoices/{draft['id']}/cancel", headers=auth_headers
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        # Still carries the balance and the passed due date it was cancelled with.
        assert cancelled.json()["balance_paise"] > 0
        assert cancelled.json()["days_overdue"] is None

    def test_a_sent_invoice_past_its_due_date_still_is(
        self, client, auth_headers, client_id: str
    ):
        draft = self._overdue_draft(client, auth_headers, client_id)

        sent = client.post(f"/api/v1/invoices/{draft['id']}/send", headers=auth_headers)
        assert sent.status_code == 200, sent.text
        assert sent.json()["days_overdue"] == 90

    def test_the_list_agrees_with_the_detail(self, client, auth_headers, client_id: str):
        draft = self._overdue_draft(client, auth_headers, client_id)

        listed = client.get("/api/v1/invoices", headers=auth_headers).json()["items"]
        row = next(inv for inv in listed if inv["id"] == draft["id"])

        assert row["days_overdue"] is None


class TestAPaymentTermThatRunsBackwards:
    """A due date is when the client was asked to pay by, counted from the day
    the bill was raised. Nothing checked the two were in that order.

    The consequence did not wait for anyone to notice: sending such an invoice
    ran it through ``refresh_status``, which read a due date already past and
    marked it overdue the same second it was issued — on the receivables list,
    counted in ``overdue_paise``, red in the billing table, and chased by the
    payment sweep at whichever offset the gap happened to match. A demand for
    late payment on a bill the client had not had one day to settle.
    """

    def test_creating_one_due_before_it_is_raised_is_refused(
        self, client, auth_headers, client_id: str
    ):
        today = clock.today()
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=today.isoformat(),
            due_date=(today - timedelta(days=30)).isoformat(),
        )
        assert response.status_code == 422, response.text
        assert "cannot be due before it is raised" in response.json()["detail"]

    def test_the_same_day_is_allowed(self, client, auth_headers, client_id: str):
        """Payable on receipt is an ordinary term, not a contradiction."""
        today = clock.today()
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=today.isoformat(),
            due_date=today.isoformat(),
        )
        assert response.status_code == 201, response.text

    def test_a_backdated_invoice_may_still_be_genuinely_overdue(
        self, client, auth_headers, client_id: str
    ):
        """The refusal is about the order of the two dates, not about lateness.
        A bill raised in March and due in April is late today, and correctly so.
        """
        today = clock.today()
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=(today - timedelta(days=120)).isoformat(),
            due_date=(today - timedelta(days=90)).isoformat(),
        )
        assert response.status_code == 201, response.text

        sent = client.post(
            f"/api/v1/invoices/{response.json()['id']}/send", headers=auth_headers
        ).json()
        assert sent["status"] == "overdue"
        assert sent["days_overdue"] == 90

    def test_moving_a_draft_issue_date_past_its_due_date_is_refused(
        self, client, auth_headers, client_id: str
    ):
        """The other side of the same edit — and the one a fat-fingered year
        reaches, since only the issue date is in the request."""
        today = clock.today()
        draft = make_invoice(
            client,
            auth_headers,
            client_id,
            due_date=(today + timedelta(days=15)).isoformat(),
        ).json()

        response = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            headers=auth_headers,
            json={"issue_date": (today + timedelta(days=25)).isoformat()},
        )
        assert response.status_code == 422, response.text
        assert "cannot be due before it is raised" in response.json()["detail"]

    def test_moving_a_draft_due_date_behind_its_issue_date_is_refused(
        self, client, auth_headers, client_id: str
    ):
        today = clock.today()
        draft = make_invoice(
            client, auth_headers, client_id, issue_date=today.isoformat()
        ).json()

        response = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            headers=auth_headers,
            json={"due_date": (today - timedelta(days=1)).isoformat()},
        )
        assert response.status_code == 422, response.text

    def test_a_refused_edit_leaves_the_draft_as_it_was(
        self, client, auth_headers, client_id: str
    ):
        """Nothing half-applied: the dates and the number series are untouched."""
        today = clock.today()
        draft = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=today.isoformat(),
            due_date=(today + timedelta(days=15)).isoformat(),
        ).json()

        client.patch(
            f"/api/v1/invoices/{draft['id']}",
            headers=auth_headers,
            json={"issue_date": (today + timedelta(days=25)).isoformat()},
        )

        after = client.get(f"/api/v1/invoices/{draft['id']}", headers=auth_headers).json()
        assert after["issue_date"] == draft["issue_date"]
        assert after["due_date"] == draft["due_date"]
        assert after["invoice_number"] == draft["invoice_number"]

    def test_moving_both_dates_together_is_judged_on_the_pair(
        self, client, auth_headers, client_id: str
    ):
        """Deferring a whole draft forward moves both, and the two stay in
        order — so the edit goes through."""
        today = clock.today()
        draft = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=today.isoformat(),
            due_date=(today + timedelta(days=15)).isoformat(),
        ).json()

        moved_to = today + timedelta(days=20)
        response = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            headers=auth_headers,
            json={
                "issue_date": moved_to.isoformat(),
                "due_date": (moved_to + timedelta(days=15)).isoformat(),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["issue_date"] == moved_to.isoformat()

    def test_an_invoice_left_without_a_due_date_is_unaffected(
        self, client, auth_headers, client_id: str
    ):
        """Sending fills it in from the issue date and the firm's terms, which
        cannot produce the contradiction — so nothing here refuses it."""
        response = make_invoice(client, auth_headers, client_id)
        assert response.status_code == 201, response.text

        sent = client.post(
            f"/api/v1/invoices/{response.json()['id']}/send", headers=auth_headers
        ).json()
        assert sent["due_date"] > sent["issue_date"]
        assert sent["status"] == "sent"

    def test_the_payment_sweep_no_longer_chases_a_brand_new_bill(
        self, client, auth_headers, client_id: str, db
    ):
        """What the refusal is protecting: an invoice due thirty days before it
        was raised matched the 30-day payment offset on its first day."""
        from app.services import reminders as reminder_service

        today = clock.today()
        refused = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=today.isoformat(),
            due_date=(today - timedelta(days=30)).isoformat(),
        )
        assert refused.status_code == 422

        queued = reminder_service.queue_payment_reminders(db, today=today)
        assert queued == []


class TestClearingAFieldOnADraft:
    """A field a caller emptied was answered 200 and left as it was.

    ``exclude_unset`` already separates a field the caller named from one they
    left out, so "is it in the patch?" is the whole of that question. Requiring
    the value to be non-null on top of it threw away the other half: naming a
    nullable field *in order to clear it* looked exactly like not sending it.

    The due date is the half that costs something. It is what starts the
    payment clock on send, what turns the invoice overdue, what the payment
    sweep counts its offsets from, and what the client is shown in the portal —
    and clearing it is the one way back to the firm's standard terms, since
    sending an invoice with none fills one in from the payment terms. A
    practitioner who mistyped a date onto a draft could replace it with another
    wrong date but could not take it off, so the bill went out demanding
    payment by whatever they had typed. The editor closed on a success message
    either way.
    """

    @pytest.fixture
    def draft(self, client, auth_headers, client_id: str) -> dict:
        today = clock.today()
        response = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=today.isoformat(),
            due_date=(today + timedelta(days=3)).isoformat(),
            notes="Payment by NEFT to the Kotak account.",
        )
        assert response.status_code == 201, response.text
        return response.json()

    def _patch(self, client, auth_headers, draft, body):
        response = client.patch(
            f"/api/v1/invoices/{draft['id']}", json=body, headers=auth_headers
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_the_due_date_can_be_taken_off(self, client, auth_headers, draft):
        after = self._patch(client, auth_headers, draft, {"due_date": None})

        assert after["due_date"] is None

    def test_it_stays_off_when_the_invoice_is_read_back(
        self, client, auth_headers, draft
    ):
        """The silence was the bug: the save reported success and the editor
        closed, so nothing said the old date was still on the record."""
        self._patch(client, auth_headers, draft, {"due_date": None})

        again = client.get(f"/api/v1/invoices/{draft['id']}", headers=auth_headers).json()
        assert again["due_date"] is None

    def test_sending_it_then_falls_back_to_the_firms_terms(
        self, client, auth_headers, draft
    ):
        """Which is the point of clearing it — a mistyped date replaced by the
        standard payment terms rather than by another guess."""
        self._patch(client, auth_headers, draft, {"due_date": None})

        sent = client.post(
            f"/api/v1/invoices/{draft['id']}/send", headers=auth_headers
        ).json()
        assert sent["due_date"] == (
            clock.today() + timedelta(days=settings.invoice_payment_terms_days)
        ).isoformat()
        assert sent["status"] == "sent"
        assert sent["days_overdue"] is None

    def test_a_mistyped_date_is_no_longer_what_the_client_is_held_to(
        self, client, auth_headers, client_id: str
    ):
        """What it cost. A due date typed one day after the issue date puts the
        client twenty-nine days late on a draft raised a month ago — chased at
        whichever offset that gap matches, and shown that date in the portal.
        Taking it off hands the bill back to the firm's own terms.

        Still late, and correctly so: a bill raised a month ago on fifteen-day
        terms is a fortnight overdue. The point is that the lateness is the
        firm's terms rather than a typo.
        """
        today = clock.today()
        issued = today - timedelta(days=30)
        draft = make_invoice(
            client,
            auth_headers,
            client_id,
            issue_date=issued.isoformat(),
            due_date=(issued + timedelta(days=1)).isoformat(),
        ).json()

        self._patch(client, auth_headers, draft, {"due_date": None})
        sent = client.post(
            f"/api/v1/invoices/{draft['id']}/send", headers=auth_headers
        ).json()

        assert sent["due_date"] == (
            issued + timedelta(days=settings.invoice_payment_terms_days)
        ).isoformat()
        assert sent["days_overdue"] == 30 - settings.invoice_payment_terms_days

    def test_the_notes_can_be_cleared_too(self, client, auth_headers, draft):
        after = self._patch(client, auth_headers, draft, {"notes": None})

        assert after["notes"] is None

    def test_an_empty_note_is_a_cleared_note(self, client, auth_headers, draft):
        """A textarea a practitioner emptied arrives as "" rather than null,
        and sanitising trims it to the same nothing."""
        after = self._patch(client, auth_headers, draft, {"notes": "   "})

        assert after["notes"] == ""

    def test_leaving_a_field_out_still_leaves_it_alone(
        self, client, auth_headers, draft
    ):
        """The other half of the distinction. A patch touching only the notes
        must not disturb the due date."""
        after = self._patch(client, auth_headers, draft, {"notes": "Revised terms."})

        assert after["due_date"] == draft["due_date"]

    def test_a_column_that_cannot_be_null_is_not_cleared(
        self, client, auth_headers, draft
    ):
        """An issue date is not a field an invoice can be without, so a null
        there is the caller not sending it rather than an instruction."""
        after = self._patch(
            client, auth_headers, draft, {"issue_date": None, "gst_rate_bps": None}
        )

        assert after["issue_date"] == draft["issue_date"]
        assert after["gst_rate_bps"] == draft["gst_rate_bps"]

    def test_clearing_the_due_date_is_never_refused_as_backwards(
        self, client, auth_headers, draft
    ):
        """There is no pair left to be out of order, and the order check reads
        the patch's own value rather than falling back to the old one."""
        response = client.patch(
            f"/api/v1/invoices/{draft['id']}",
            json={
                "due_date": None,
                "issue_date": (clock.today() + timedelta(days=20)).isoformat(),
            },
            headers=auth_headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["due_date"] is None


class TestAListingWindowThatRunsBackwards:
    """``GET /invoices`` took a date window and never checked its order.

    A reversed window matches nothing, so the list came back ``200`` with an
    empty page — which is exactly what a firm with no invoices in that window
    sees, and nothing in the response tells the two apart. The year is the
    digit that gets mistyped in a date field, and an empty billing table reads
    as "this work was never invoiced": the practitioner raises the invoice
    again, and the client is billed twice for work already on a bill the
    filter was hiding.

    Every other endpoint in the API that takes a window already refuses this —
    ``/invoices/revenue``, ``/compliance/calendar``, ``/documents/outstanding``
    and ``/audit``. This was the one that did not.
    """

    def _list(self, client, auth_headers, **params):
        return client.get("/api/v1/invoices", params=params, headers=auth_headers)

    def test_a_reversed_window_is_refused_rather_than_answered_empty(
        self, client, auth_headers, client_id: str
    ):
        assert make_invoice(client, auth_headers, client_id).status_code == 201

        response = self._list(
            client, auth_headers, from_date="2026-12-01", to_date="2026-01-01"
        )

        assert response.status_code == 422, response.text
        assert response.json()["detail"] == "to_date must not be before from_date"

    def test_the_same_day_at_both_ends_is_a_window(
        self, client, auth_headers, client_id: str
    ):
        """One day is the narrowest real window, not a contradiction."""
        today = clock.today()
        assert (
            make_invoice(
                client, auth_headers, client_id, issue_date=today.isoformat()
            ).status_code
            == 201
        )

        response = self._list(
            client, auth_headers, from_date=today.isoformat(), to_date=today.isoformat()
        )

        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1

    def test_one_end_alone_is_never_out_of_order(
        self, client, auth_headers, client_id: str
    ):
        """A half-open window is a window; there is no pair to compare."""
        assert make_invoice(client, auth_headers, client_id).status_code == 201

        assert self._list(client, auth_headers, from_date="2035-01-01").status_code == 200
        assert self._list(client, auth_headers, to_date="2000-01-01").status_code == 200

    def test_a_window_in_the_right_order_still_filters(
        self, client, auth_headers, client_id: str
    ):
        """The refusal is about the order of the two dates, not about matching
        nothing: a well-formed window that happens to be empty is still 200."""
        assert make_invoice(client, auth_headers, client_id).status_code == 201

        response = self._list(
            client, auth_headers, from_date="2000-01-01", to_date="2000-12-31"
        )

        assert response.status_code == 200, response.text
        assert response.json()["total"] == 0


class TestAReceiptDatedWhenItCannotHaveArrived:
    """``payment_date`` is the firm's record of the day money arrived, and it
    is the one field on a receipt a practitioner types rather than derives.
    Nothing checked it against anything.

    Both impossible directions went onto the record. A future date says money
    has arrived that has not — what a mistyped year produces — and a date
    before the invoice was raised says the client paid a bill that did not
    exist, which is the everyday slip of booking last year's receipt against
    this year's invoice while reconciling. Neither is recoverable: there is
    one ``payment_date`` per invoice and nothing keeps what it was before.
    """

    def _sent(self, client, auth_headers, client_id, **overrides):
        invoice = make_invoice(client, auth_headers, client_id, **overrides).json()
        return client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()

    def _pay(self, client, auth_headers, invoice, **payload):
        return client.post(
            f"/api/v1/invoices/{invoice['id']}/payments",
            json={"amount_paise": 1000, **payload},
            headers=auth_headers,
        )

    def test_a_payment_dated_in_the_future_is_refused(
        self, client, auth_headers, client_id: str
    ):
        sent = self._sent(client, auth_headers, client_id)
        ahead = clock.today() + timedelta(days=1)

        response = self._pay(client, auth_headers, sent, payment_date=ahead.isoformat())

        assert response.status_code == 422, response.text
        assert "is in the future" in response.json()["detail"]

    def test_a_mistyped_year_is_the_case_this_catches(
        self, client, auth_headers, client_id: str
    ):
        sent = self._sent(client, auth_headers, client_id)

        response = self._pay(client, auth_headers, sent, payment_date="2099-04-01")

        assert response.status_code == 422, response.text
        assert "01 Apr 2099" in response.json()["detail"]

    def test_a_payment_dated_before_the_invoice_was_raised_is_refused(
        self, client, auth_headers, client_id: str
    ):
        raised = clock.today() - timedelta(days=10)
        sent = self._sent(
            client, auth_headers, client_id, issue_date=raised.isoformat()
        )

        response = self._pay(
            client,
            auth_headers,
            sent,
            payment_date=(raised - timedelta(days=1)).isoformat(),
        )

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "falls before invoice" in detail
        assert sent["invoice_number"] in detail

    def test_the_day_the_invoice_was_raised_is_allowed(
        self, client, auth_headers, client_id: str
    ):
        """Payment on receipt is an ordinary term, not a contradiction."""
        raised = clock.today() - timedelta(days=10)
        sent = self._sent(
            client, auth_headers, client_id, issue_date=raised.isoformat()
        )

        response = self._pay(
            client, auth_headers, sent, payment_date=raised.isoformat()
        )

        assert response.status_code == 200, response.text
        assert response.json()["payment_date"] == raised.isoformat()

    def test_today_is_allowed(self, client, auth_headers, client_id: str):
        sent = self._sent(client, auth_headers, client_id)
        today = clock.today()

        response = self._pay(client, auth_headers, sent, payment_date=today.isoformat())

        assert response.status_code == 200, response.text
        assert response.json()["payment_date"] == today.isoformat()

    def test_omitting_the_date_still_books_it_today(
        self, client, auth_headers, client_id: str
    ):
        """The default cannot produce either failure, so it is left alone."""
        sent = self._sent(client, auth_headers, client_id)

        response = self._pay(client, auth_headers, sent)

        assert response.status_code == 200, response.text
        assert response.json()["payment_date"] == clock.today().isoformat()

    def test_a_refused_date_leaves_the_invoice_untouched(
        self, client, auth_headers, client_id: str
    ):
        """Checked before anything is applied, so the balance does not move."""
        sent = self._sent(client, auth_headers, client_id)

        self._pay(client, auth_headers, sent, payment_date="2099-04-01")

        after = client.get(
            f"/api/v1/invoices/{sent['id']}", headers=auth_headers
        ).json()
        assert after["amount_paid_paise"] == 0
        assert after["payment_date"] is None
        assert after["status"] == sent["status"]


class TestAnInvoiceChangedFromAStaleCopy:
    """Only receipting held the invoice's row while it decided from it.

    The other three transitions read it plainly, and the sessions here do not
    expire what they have loaded on commit — so each of them could decide from
    a copy taken before a concurrent request had already committed something
    that changes the answer.

    Both directions cost real money. Cancelling releases the filings an invoice
    covered back into the billable pool, so cancelling one the client has just
    settled bills them a second time for work already paid for. Editing is
    refused on a sent invoice because it is a document of record; against a
    stale ``draft`` the edit lands on the copy the client is holding.

    The competing write is made through a second session and committed, which
    is exactly what another worker or another practitioner's request looks
    like from here.
    """

    def _draft(self, client, auth_headers, client_id, db):
        """A draft, plus this session's copy of it — taken before the race.

        The copy is *returned* rather than merely loaded, and every caller keeps
        it in a local: the identity map holds weak references, so a copy nobody
        is holding is collected and the next read goes to the database, which is
        the one thing that would make the staleness disappear.

        What it stands in for is the snapshot a request takes when it reads the
        invoice and then decides from it a moment later. SQLite cannot run two
        writers at once, so the read that predates a competing commit is
        expressed this way rather than by racing two connections.
        """
        invoice = make_invoice(client, auth_headers, client_id).json()
        invoice_id = uuid.UUID(invoice["id"])
        ours = db.get(Invoice, invoice_id)
        assert ours.status is InvoiceStatus.DRAFT
        # SQLite will not let another connection write past an open read.
        db.commit()
        return invoice, invoice_id, ours

    def _in_another_session(self, invoice_id, change):
        from app.database import SessionLocal

        other = SessionLocal()
        try:
            change(other, billing.load_for_update(other, invoice_id))
            other.commit()
        finally:
            other.close()

    def _reread(self, invoice_id):
        from app.database import SessionLocal

        fresh = SessionLocal()
        try:
            invoice = fresh.get(Invoice, invoice_id)
            return invoice.status, invoice.amount_paid_paise, invoice.notes
        finally:
            fresh.close()

    def test_a_settled_invoice_cannot_be_cancelled_from_a_stale_balance(
        self, client, auth_headers, client_id, db
    ):
        invoice, invoice_id, ours = self._draft(client, auth_headers, client_id, db)
        sent = client.post(
            f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers
        ).json()
        db.commit()

        self._in_another_session(
            invoice_id,
            lambda session, inv: billing.record_payment(
                inv, amount_paise=sent["total_paise"], reference="NEFT-1"
            ),
        )

        response = client.post(
            f"/api/v1/invoices/{invoice_id}/cancel", headers=auth_headers
        )
        assert response.status_code == 409, response.text
        status_after, paid_after, _ = self._reread(invoice_id)
        assert status_after is InvoiceStatus.PAID
        assert paid_after == sent["total_paise"]
        assert ours.id == invoice_id  # the stale copy, still held

    def test_the_released_filings_stay_billed_when_the_cancel_is_refused(
        self, client, auth_headers, client_id, db
    ):
        """The half that costs the client money.

        Cancelling un-bills the filings the invoice cited so they can be
        re-invoiced. Against a stale balance that ran on a paid invoice, and
        the work then reappeared on the billable list.
        """
        items = file_everything(client, auth_headers)
        billable = client.get("/api/v1/invoices/billable", headers=auth_headers).json()
        assert billable["total_items"] > 0
        generated = client.post(
            "/api/v1/invoices/generate", json={}, headers=auth_headers
        ).json()["invoices"][0]
        invoice_id = uuid.UUID(generated["id"])
        sent = client.post(
            f"/api/v1/invoices/{invoice_id}/send", headers=auth_headers
        ).json()
        ours = db.get(Invoice, invoice_id)
        assert ours.amount_paid_paise == 0
        db.commit()

        self._in_another_session(
            invoice_id,
            lambda session, inv: billing.record_payment(
                inv, amount_paise=sent["total_paise"], reference="NEFT-2"
            ),
        )

        assert (
            client.post(
                f"/api/v1/invoices/{invoice_id}/cancel", headers=auth_headers
            ).status_code
            == 409
        )
        still_billed = client.get(
            "/api/v1/invoices/billable", headers=auth_headers
        ).json()
        assert still_billed["total_items"] == 0, "paid work went back on the billable list"
        assert len(items) > 0
        assert ours.id == invoice_id  # the stale copy, still held

    def test_a_sent_invoice_cannot_be_edited_from_a_stale_status(
        self, client, auth_headers, client_id, db
    ):
        invoice, invoice_id, ours = self._draft(client, auth_headers, client_id, db)

        def send(session, inv):
            inv.status = InvoiceStatus.SENT

        self._in_another_session(invoice_id, send)

        response = client.patch(
            f"/api/v1/invoices/{invoice_id}",
            json={"notes": "edited after it went out"},
            headers=auth_headers,
        )
        assert response.status_code == 409, response.text
        _, _, notes_after = self._reread(invoice_id)
        assert notes_after != "edited after it went out"
        assert ours.id == invoice_id  # the stale copy, still held

    def test_an_invoice_cannot_be_sent_twice_from_a_stale_status(
        self, client, auth_headers, client_id, db
    ):
        invoice, invoice_id, ours = self._draft(client, auth_headers, client_id, db)

        def send(session, inv):
            inv.status = InvoiceStatus.SENT

        self._in_another_session(invoice_id, send)

        response = client.post(
            f"/api/v1/invoices/{invoice_id}/send", headers=auth_headers
        )
        assert response.status_code == 409, response.text
        assert ours.id == invoice_id  # the stale copy, still held

    def test_each_transition_takes_its_invoice_through_the_lock(
        self, client, auth_headers, client_id, monkeypatch
    ):
        """Asserted on the routes, the way receipting already is.

        The guard is a keyword on a shared lookup, and dropping it from any one
        of these is a one-character regression every other test here survives.
        """
        invoice = make_invoice(client, auth_headers, client_id).json()
        locked = []
        real = billing.load_for_update
        monkeypatch.setattr(
            billing,
            "load_for_update",
            lambda session, invoice_id: (
                locked.append(invoice_id),
                real(session, invoice_id),
            )[1],
        )

        client.patch(
            f"/api/v1/invoices/{invoice['id']}",
            json={"notes": "still a draft"},
            headers=auth_headers,
        )
        client.post(f"/api/v1/invoices/{invoice['id']}/send", headers=auth_headers)
        client.post(f"/api/v1/invoices/{invoice['id']}/cancel", headers=auth_headers)

        assert locked == [uuid.UUID(invoice["id"])] * 3
