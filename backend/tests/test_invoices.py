"""Billing: numbering, GST maths, generation from filed work, and payments."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.models.base import InvoiceStatus
from app.models.invoice import Invoice, InvoiceLine
from app.schemas.common import MAX_AMOUNT_PAISE
from app.services import billing
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
            "filed_on": date.today().isoformat(),
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
        past = (date.today() - timedelta(days=5)).isoformat()
        invoice = make_invoice(client, auth_headers, client_id, due_date=past).json()
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
        past = (date.today() - timedelta(days=5)).isoformat()
        invoice = make_invoice(client, auth_headers, client_id, due_date=past).json()
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
