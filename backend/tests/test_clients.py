"""Client CRUD and the compliance items generated from a client's registrations."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core import clock
from app.core.periods import add_months
from app.models.base import (
    ComplianceCategory,
    ComplianceStatus,
    EntityType,
    Frequency,
    GSTFilingFrequency,
    ReminderStatus,
    TaskStatus,
)
from app.models.client import Client as ClientModel
from app.models.compliance import ComplianceItem, ComplianceType
from app.models.reminder import Reminder
from app.models.task import Task
from app.schemas.common import MAX_AMOUNT_PAISE
from app.services import compliance_generator
from app.services.applicability import applies_to
from app.services.compliance_generator import (
    applicable_types,
    default_window,
    generate_compliance_items,
    max_lookback_months,
    regenerate_for_firm,
)
from tests.conftest import make_client_payload, paged_order_by

API = "/api/v1"


def codes_generated(db: Session, client_id: str) -> set[str]:
    rows = db.execute(
        select(ComplianceType.code)
        .join(ComplianceItem, ComplianceItem.compliance_type_id == ComplianceType.id)
        .where(ComplianceItem.client_id == uuid.UUID(client_id))
        .distinct()
    ).all()
    return {row[0] for row in rows}


def items_of(db: Session, client_id: uuid.UUID) -> list[ComplianceItem]:
    return list(
        db.scalars(select(ComplianceItem).where(ComplianceItem.client_id == client_id)).all()
    )


SECOND_FIRM = {
    "firm_name": "Iyer & Co",
    "firm_email": "office@iyer-ca.in",
    "owner_full_name": "Suresh Iyer",
    "owner_email": "suresh@iyer-ca.in",
    "owner_password": "another-strong-password",
}


def make_model_client(**overrides) -> ClientModel:
    """An unsaved client used to exercise the applicability predicates."""
    defaults = {
        "name": "Test Co",
        "entity_type": EntityType.INDIVIDUAL,
        "gst_registered": False,
        "gst_filing_frequency": GSTFilingFrequency.MONTHLY,
        "tds_applicable": False,
        "income_tax_applicable": True,
        "tax_audit_applicable": False,
        "roc_applicable": False,
        "payroll_applicable": False,
    }
    return ClientModel(**(defaults | overrides))


class TestApplicabilityRules:
    def test_gst_rules_follow_the_filing_frequency(self):
        monthly = make_model_client(gst_registered=True)
        quarterly = make_model_client(
            gst_registered=True, gst_filing_frequency=GSTFilingFrequency.QUARTERLY
        )
        unregistered = make_model_client()

        assert applies_to("gst_monthly", monthly)
        assert not applies_to("gst_quarterly", monthly)
        assert applies_to("gst_quarterly", quarterly)
        assert not applies_to("gst_monthly", quarterly)
        assert not applies_to("gst_registered", unregistered)

    def test_audit_and_non_audit_are_mutually_exclusive(self):
        audited = make_model_client(tax_audit_applicable=True)
        plain = make_model_client()
        assert applies_to("income_tax_audit", audited)
        assert not applies_to("income_tax_non_audit", audited)
        assert applies_to("income_tax_non_audit", plain)

    def test_roc_is_implied_by_entity_type(self):
        company = make_model_client(entity_type=EntityType.PRIVATE_LIMITED)
        llp = make_model_client(entity_type=EntityType.LLP)
        individual = make_model_client()

        assert applies_to("roc_company", company)
        assert not applies_to("roc_company", llp)
        assert applies_to("roc_llp", llp)
        assert not applies_to("roc", individual)

    def test_unknown_rule_is_not_applicable(self):
        assert not applies_to("no_such_rule", make_model_client())

    def test_income_tax_audit_needs_both_flags(self):
        """Not just the audit flag, which on its own says nothing about a return.

        The two income-tax rules split one population in two, and the pair only
        adds up if each turns on both flags. Reading `income_tax_audit` as
        "audited *or* liable" hands the audit-season ITR — a 35,000 fee and a
        different deadline — to every ordinary client the firm has.
        """
        liable_not_audited = make_model_client()
        not_liable = make_model_client(income_tax_applicable=False, tax_audit_applicable=True)

        assert not applies_to("income_tax_audit", liable_not_audited)
        assert not applies_to("income_tax_audit", not_liable)
        assert not applies_to("income_tax_non_audit", not_liable)


class TestAdvanceTaxApplicability:
    """Sec 211 instalments: who owes them, and who the code excuses.

    Advance tax is the one rule with a shape of its own — a gate on income-tax
    liability, then an exemption that an audit overrides — and none of it was
    covered. The predicate survived being inverted at the gate, at the
    exemption, and at the join, so each of those is checked here on its own.

    It matters in both directions. Not generating the instalment leaves a
    client to find out at assessment, with 234B/234C interest running from
    April. Generating it for an exempt individual puts four deadlines and four
    fees on a return that never owed them.
    """

    def test_a_company_owes_instalments(self):
        company = make_model_client(entity_type=EntityType.PRIVATE_LIMITED)
        assert applies_to("advance_tax", company)

    def test_an_individual_without_a_tax_audit_is_exempt(self):
        assert not applies_to("advance_tax", make_model_client())
        assert not applies_to("advance_tax", make_model_client(entity_type=EntityType.HUF))

    def test_a_tax_audit_overrides_the_exemption(self):
        # An individual carrying on business past the audit threshold is back
        # in: the exemption is for the salaried case, not for the entity type.
        audited = make_model_client(tax_audit_applicable=True)
        assert applies_to("advance_tax", audited)

    def test_nothing_is_owed_where_income_tax_does_not_apply(self):
        # The gate comes first. A client the firm does not file a return for
        # owes no instalments against it, whatever else is flagged.
        exempt_company = make_model_client(
            entity_type=EntityType.PRIVATE_LIMITED, income_tax_applicable=False
        )
        assert not applies_to("advance_tax", exempt_company)


class TestFixedApplicabilityRules:
    """``always`` and ``never``, which no seeded type uses but a firm can.

    They are in the table for firm-defined compliance types, which is exactly
    why they go untested by anything that only exercises the seeds — and why
    both survived being swapped for their opposite. A firm's own quarterly
    review attached to `always` would silently apply to nobody.
    """

    def test_always_applies_to_a_client_that_qualifies_for_nothing_else(self):
        bare = make_model_client(income_tax_applicable=False)
        assert applies_to("always", bare) is True

    def test_never_applies_to_a_client_that_qualifies_for_everything(self):
        qualifies_for_everything = make_model_client(
            entity_type=EntityType.PRIVATE_LIMITED,
            gst_registered=True,
            tds_applicable=True,
            tax_audit_applicable=True,
            roc_applicable=True,
            payroll_applicable=True,
        )
        assert applies_to("never", qualifies_for_everything) is False


class TestClientCreation:
    def test_create_returns_client_and_item_count(self, client: TestClient, auth_headers: dict):
        response = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["client"]["name"] == "Nimbus Textiles Pvt Ltd"
        assert body["client"]["gstin"] == "27AABCN2345P1Z5"
        assert body["compliance_items_created"] > 0

    def test_pan_and_gstin_are_normalised_and_validated(
        self, client: TestClient, auth_headers: dict
    ):
        ok = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(pan="aabcn2345p", gstin="27aabcn2345p1z5"),
        )
        assert ok.status_code == 201
        assert ok.json()["client"]["pan"] == "AABCN2345P"

        bad = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(pan="INVALID", name="Other Co"),
        )
        assert bad.status_code == 422

    def test_duplicate_pan_in_firm_conflicts(self, client: TestClient, auth_headers: dict):
        client.post(f"{API}/clients", headers=auth_headers, json=make_client_payload())
        response = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(name="Different Name", gstin=None),
        )
        assert response.status_code == 409

    def test_generation_can_be_skipped(self, client: TestClient, auth_headers: dict):
        response = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(generate_compliance_items=False),
        )
        assert response.status_code == 201
        assert response.json()["compliance_items_created"] == 0

    def test_assignee_must_belong_to_the_firm(self, client: TestClient, auth_headers: dict):
        response = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                assigned_practitioner_id="99999999-9999-9999-9999-999999999999"
            ),
        )
        assert response.status_code == 400

    def test_creation_requires_authentication(self, client: TestClient):
        assert client.post(f"{API}/clients", json=make_client_payload()).status_code == 401


class TestComplianceGeneration:
    """The core rule: registrations in, the right filings out."""

    def test_gst_monthly_client_gets_gstr1_and_gstr3b(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        created = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                gst_registered=True,
                gst_filing_frequency="monthly",
                tds_applicable=False,
                tax_audit_applicable=False,
                roc_applicable=False,
                entity_type="proprietorship",
            ),
        ).json()

        codes = codes_generated(db, created["client"]["id"])
        assert "GSTR1_MONTHLY" in codes
        assert "GSTR3B_MONTHLY" in codes
        assert "GSTR1_QUARTERLY" not in codes
        assert "TDS_RETURN_24Q" not in codes

    def test_qrmp_client_gets_quarterly_gst_only(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        created = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                gst_filing_frequency="quarterly", tds_applicable=False, roc_applicable=False
            ),
        ).json()

        codes = codes_generated(db, created["client"]["id"])
        assert "GSTR1_QUARTERLY" in codes
        assert "GSTR3B_QUARTERLY" in codes
        assert "GSTR1_MONTHLY" not in codes

    def test_non_gst_client_gets_no_gst_items(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        created = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                name="Salaried Individual",
                entity_type="individual",
                pan="AAAPI1234Q",
                gstin=None,
                gst_registered=False,
                tds_applicable=False,
                tax_audit_applicable=False,
                roc_applicable=False,
            ),
        ).json()

        codes = codes_generated(db, created["client"]["id"])
        assert not any(code.startswith("GSTR") for code in codes)
        assert "ITR_NON_AUDIT" in codes

    def test_company_gets_roc_filings(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()
        codes = codes_generated(db, created["client"]["id"])
        assert "ROC_AOC4" in codes
        assert "ROC_MGT7" in codes
        assert "ROC_LLP_FORM11" not in codes

    def test_llp_gets_llp_forms_not_company_forms(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        created = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                name="Meridian Consulting LLP", entity_type="llp", pan="AABFM5678L", gstin=None
            ),
        ).json()
        codes = codes_generated(db, created["client"]["id"])
        assert "ROC_LLP_FORM11" in codes
        assert "ROC_LLP_FORM8" in codes
        assert "ROC_AOC4" not in codes

    def test_tds_deductor_gets_quarterly_returns(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload(tds_applicable=True)
        ).json()
        codes = codes_generated(db, created["client"]["id"])
        assert "TDS_RETURN_24Q" in codes
        assert "TDS_RETURN_26Q" in codes
        assert "TDS_PAYMENT_MONTHLY" in codes

    def test_audit_client_gets_audit_dated_itr(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()
        codes = codes_generated(db, created["client"]["id"])
        assert "ITR_AUDIT" in codes
        assert "TAX_AUDIT_3CD" in codes
        assert "ITR_NON_AUDIT" not in codes

    def test_generated_items_carry_the_default_fee(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()
        item = db.scalars(
            select(ComplianceItem)
            .join(ComplianceType)
            .where(
                ComplianceItem.client_id == uuid.UUID(created["client"]["id"]),
                ComplianceType.code == "GSTR3B_MONTHLY",
            )
        ).first()
        assert item is not None
        assert item.fee_paise == 200_000
        assert item.status == ComplianceStatus.PENDING

    def test_service_fee_override_wins(self, client: TestClient, auth_headers: dict, db: Session):
        created = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(service_fees={"GSTR3B_MONTHLY": 999_00}),
        ).json()
        item = db.scalars(
            select(ComplianceItem)
            .join(ComplianceType)
            .where(
                ComplianceItem.client_id == uuid.UUID(created["client"]["id"]),
                ComplianceType.code == "GSTR3B_MONTHLY",
            )
        ).first()
        assert item.fee_paise == 99_900

    def test_generation_is_idempotent(self, client: TestClient, auth_headers: dict, db: Session):
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()
        first_count = created["compliance_items_created"]

        again = client.post(
            f"{API}/clients/{created['client']['id']}/compliance-items",
            headers=auth_headers,
            json={},
        ).json()
        assert again["created"] == 0
        assert again["skipped_existing"] == first_count

    def test_explicit_window_generates_known_periods(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        created = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(generate_compliance_items=False),
        ).json()
        client_id = created["client"]["id"]

        response = client.post(
            f"{API}/clients/{client_id}/compliance-items",
            headers=auth_headers,
            json={"window_start": "2026-04-01", "window_end": "2026-06-30"},
        )
        assert response.status_code == 200
        assert response.json()["created"] > 0

        gstr3b = db.scalars(
            select(ComplianceItem)
            .join(ComplianceType)
            .where(
                ComplianceItem.client_id == uuid.UUID(client_id),
                ComplianceType.code == "GSTR3B_MONTHLY",
            )
            .order_by(ComplianceItem.due_date)
        ).all()
        # Periods 2026-03..2026-05 have due dates 20 Apr / 20 May / 20 Jun.
        assert [i.due_date for i in gstr3b] == [
            date(2026, 4, 20),
            date(2026, 5, 20),
            date(2026, 6, 20),
        ]

    def test_enabling_gst_later_backfills_items(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        created = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                name="Late Registrant",
                pan="AAACL9876R",
                gstin=None,
                gst_registered=False,
                tds_applicable=False,
                tax_audit_applicable=False,
                roc_applicable=False,
                entity_type="proprietorship",
            ),
        ).json()
        client_id = created["client"]["id"]
        assert "GSTR3B_MONTHLY" not in codes_generated(db, client_id)

        patched = client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"gst_registered": True, "gstin": "27AAACL9876R1ZE"},
        )
        assert patched.status_code == 200
        assert patched.json()["compliance_items_created"] > 0
        assert "GSTR3B_MONTHLY" in codes_generated(db, client_id)

    def test_applicable_types_respects_flags(self, db: Session, registered_firm: dict):
        firm_id = registered_firm["firm"]["id"]
        model_client = make_model_client(gst_registered=True, firm_id=uuid.UUID(firm_id))
        rules = {ct.applicability_rule for ct in applicable_types(db, model_client)}
        assert "gst_monthly" in rules
        assert "tds_applicable" not in rules

    def test_generator_skips_periods_outside_the_window(self, db: Session, registered_firm: dict):
        firm_id = registered_firm["firm"]["id"]
        model_client = make_model_client(
            firm_id=uuid.UUID(firm_id), gst_registered=True, onboarded_on=date(2026, 4, 1)
        )
        db.add(model_client)
        db.flush()

        result = generate_compliance_items(
            db,
            model_client,
            window_start=date(2026, 4, 1),
            window_end=date(2026, 4, 30),
        )
        due_dates = [item.due_date for item in result.created]
        assert due_dates, "expected at least one item in April"
        assert all(date(2026, 4, 1) <= d <= date(2026, 4, 30) for d in due_dates)


class TestTheDefaultGenerationWindow:
    """What gets generated when nobody names a window — which is every real run.

    The API passes a window only on the explicit top-up endpoint. Onboarding, a
    registration change and the nightly job all take the default, and none of
    it was covered: the run date could be ignored in favour of the wall clock
    and the back-fill could reach two months or four instead of three, with the
    suite none the wiser. The span decides which deadlines a firm is shown at
    all, so it is pinned to exact dates here rather than to a shape.
    """

    def test_the_window_reaches_three_months_back_and_the_configured_span_forward(self):
        # Three months of back-fill is what puts the quarter a client onboarded
        # mid-way through on their calendar instead of only the next one.
        established = make_model_client(onboarded_on=date(2020, 1, 1))
        start, end = default_window(established, today=date(2026, 7, 15))

        assert start == date(2026, 4, 15)
        assert end == add_months(date(2026, 7, 15), settings.compliance_generation_months)

    def test_a_client_onboarded_inside_that_reach_starts_at_onboarding(self):
        # The firm was not acting for them before this date, so the deadlines
        # that passed beforehand are not theirs to have missed.
        recent = make_model_client(onboarded_on=date(2026, 6, 1))
        start, _ = default_window(recent, today=date(2026, 7, 15))
        assert start == date(2026, 6, 1)

    def test_the_run_date_decides_the_window_rather_than_the_wall_clock(
        self, db: Session, registered_firm: dict
    ):
        # The nightly job passes its run date down. Reading the clock instead
        # makes the job untestable and a replay of a missed night wrong.
        model_client = make_model_client(
            firm_id=uuid.UUID(registered_firm["firm"]["id"]),
            gst_registered=True,
            onboarded_on=date(2020, 1, 1),
        )
        db.add(model_client)
        db.flush()

        result = generate_compliance_items(db, model_client, today=date(2026, 7, 15))
        assert result.window_start == date(2026, 4, 15)


class TestLookingBackFarEnoughForLateDeadlines:
    """A period can end long before the window and still fall due inside it.

    The generator searches back ``max_lookback_months`` before the window and
    lets the due-date filter trim the excess. Reading only ``due_month_offset``
    and ignoring the per-period overrides survived the suite — and the override
    is precisely where the long gaps live. The TDS return for Q4 is the case:
    the quarter ends 31 March and the return is due 31 May, two months out
    rather than one, so a lookback built from the default alone stops a month
    short and the filing is never generated at all.
    """

    def test_the_lookback_takes_the_longest_override_not_the_default(self):
        quarterly_with_a_long_q4 = ComplianceType(
            due_month_offset=1, due_overrides={"Q4": {"month_offset": 2, "day": 31}}
        )
        assert max_lookback_months(quarterly_with_a_long_q4) == 2

    def test_an_override_that_shortens_the_gap_does_not_shorten_the_lookback(self):
        # max over every offset, not the last one read: a short override must
        # not pull the reach in for the periods that still need the long one.
        mixed = ComplianceType(
            due_month_offset=3, due_overrides={"Q1": {"month_offset": 0}}
        )
        assert max_lookback_months(mixed) == 3

    def test_a_type_with_no_overrides_looks_back_by_its_own_offset(self):
        assert max_lookback_months(ComplianceType(due_month_offset=4, due_overrides={})) == 4

    def test_the_q4_tds_return_is_generated_from_a_window_after_its_quarter(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """End to end: the filing the short lookback would have lost.

        Q4 of FY2025-26 ends 31 March 2026 and its 24Q is due 31 May 2026. A
        window opening in May is after the period, after the ordinary one-month
        deadline, and inside the overridden one.
        """
        created = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(generate_compliance_items=False),
        ).json()
        client_id = created["client"]["id"]

        response = client.post(
            f"{API}/clients/{client_id}/compliance-items",
            headers=auth_headers,
            json={"window_start": "2026-05-01", "window_end": "2026-05-31"},
        )
        assert response.status_code == 200, response.text

        q4 = db.scalars(
            select(ComplianceItem)
            .join(ComplianceType)
            .where(
                ComplianceItem.client_id == uuid.UUID(client_id),
                ComplianceType.code == "TDS_RETURN_24Q",
                ComplianceItem.period_label == "FY2025-26-Q4",
            )
        ).all()
        assert len(q4) == 1, "the Q4 TDS return fell outside the lookback"
        assert q4[0].due_date == date(2026, 5, 31)


class TestTheEdgesOfTheGenerationWindow:
    def test_a_filing_due_on_the_first_day_of_the_window_is_generated(
        self, db: Session, registered_firm: dict
    ):
        """The window is inclusive at both ends, and the start is the risky one.

        A top-up run picks up where the last one stopped, so the first day is a
        day some filing falls due on eventually. Excluding it drops that return
        from the calendar with nothing to say it happened — and the next run,
        starting later still, never looks back at it either.
        """
        model_client = make_model_client(
            firm_id=uuid.UUID(registered_firm["firm"]["id"]),
            gst_registered=True,
            onboarded_on=date(2026, 1, 1),
        )
        db.add(model_client)
        db.flush()

        # GSTR-3B for 2026-03 is due on the 20th of the following month.
        result = generate_compliance_items(
            db, model_client, window_start=date(2026, 4, 20), window_end=date(2026, 4, 20)
        )
        assert result.created, "a filing due on the opening day was dropped"
        assert {item.due_date for item in result.created} == {date(2026, 4, 20)}

    def test_a_filing_due_on_the_last_day_of_the_window_is_generated(
        self, db: Session, registered_firm: dict
    ):
        model_client = make_model_client(
            firm_id=uuid.UUID(registered_firm["firm"]["id"]),
            gst_registered=True,
            onboarded_on=date(2026, 1, 1),
        )
        db.add(model_client)
        db.flush()

        result = generate_compliance_items(
            db, model_client, window_start=date(2026, 4, 11), window_end=date(2026, 4, 11)
        )
        assert {item.due_date for item in result.created} == {date(2026, 4, 11)}

    def test_a_second_run_in_the_same_transaction_creates_nothing(
        self, db: Session, registered_firm: dict
    ):
        """Idempotency has to hold before the commit, not only across two of them.

        The already-generated lookup is a query, and the session runs with
        ``autoflush=False``, so what the first run only added to the session is
        invisible to the second unless it was flushed. Unflushed, the second run
        creates every filing again: the client's calendar shows each statutory
        deadline twice, and each duplicate carries its own fee onto the billable
        pile. The endpoints commit between runs and would never have shown it.
        """
        model_client = make_model_client(
            firm_id=uuid.UUID(registered_firm["firm"]["id"]),
            gst_registered=True,
            onboarded_on=date(2026, 1, 1),
        )
        db.add(model_client)
        db.flush()

        window = {"window_start": date(2026, 4, 1), "window_end": date(2026, 4, 30)}
        first = generate_compliance_items(db, model_client, **window)
        second = generate_compliance_items(db, model_client, **window)

        assert first.created
        assert second.created == []
        assert second.skipped_existing == len(first.created)

    def test_a_fee_the_firm_has_zeroed_is_generated_at_zero(
        self, db: Session, registered_firm: dict
    ):
        """Not one paise, which would put no-charge work back on the bill.

        A firm zero-rates a filing when it is covered by a retainer or done as
        a courtesy. The billable pile is "filed, unbilled, fee above zero", so
        a fee that rounds up to a stray paise anywhere puts that filing on an
        invoice the client was told they would not get.
        """
        model_client = make_model_client(
            firm_id=uuid.UUID(registered_firm["firm"]["id"]),
            gst_registered=True,
            onboarded_on=date(2026, 1, 1),
            service_fees={"GSTR3B_MONTHLY": 0},
        )
        db.add(model_client)
        db.flush()

        result = generate_compliance_items(
            db, model_client, window_start=date(2026, 4, 20), window_end=date(2026, 4, 20)
        )
        zeroed = [
            item
            for item in result.created
            if item.compliance_type_id
            == db.scalar(
                select(ComplianceType.id).where(ComplianceType.code == "GSTR3B_MONTHLY")
            )
        ]
        assert zeroed, "expected the zero-rated GSTR-3B in this window"
        assert all(item.fee_paise == 0 for item in zeroed)


class TestAGenerationWindowNobodyMeantToAskFor:
    """Two silences on the one endpoint that takes a window from a caller.

    ``POST /clients/{id}/compliance-items`` is where a practitioner backfills a
    calendar, and it accepted any pair of dates at all.

    Backwards, it created nothing and reported success — ``created: 0``, which
    is exactly what a client whose calendar is already complete returns. The
    practitioner reads "already done" and the filings they came for are still
    missing. Every other date-window endpoint answers a reversed pair with a
    422 naming both dates.

    Too wide, it is the expensive direction: the row count is periods ×
    applicable types, five of the seeded types are monthly, and a mistyped
    year — the digit that gets mistyped in a date field — turns one request
    into tens of thousands of filings. Each one lands on the calendar, is
    counted on the dashboard, goes overdue on its own due date, raises a task
    and queues mail to the client about a period decades away. There is no
    delete for a filing to undo it with.
    """

    @pytest.fixture
    def a_client(self, client: TestClient, auth_headers: dict) -> str:
        response = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        )
        assert response.status_code == 201, response.text
        return response.json()["client"]["id"]

    def _generate(self, client, auth_headers, client_id, **window):
        return client.post(
            f"{API}/clients/{client_id}/compliance-items",
            headers=auth_headers,
            json=window,
        )

    def test_a_backwards_window_is_refused_rather_than_reported_as_done(
        self, client: TestClient, auth_headers: dict, a_client: str
    ):
        response = self._generate(
            client,
            auth_headers,
            a_client,
            window_start="2026-12-31",
            window_end="2026-01-01",
        )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "31 Dec 2026" in detail and "01 Jan 2026" in detail

    def test_a_window_wider_than_the_cap_is_refused(
        self, client: TestClient, auth_headers: dict, a_client: str, db: Session
    ):
        before = db.scalar(select(func.count(ComplianceItem.id)))

        response = self._generate(
            client,
            auth_headers,
            a_client,
            window_start="1990-01-01",
            window_end="2190-01-01",
        )

        assert response.status_code == 422, response.text
        assert "5 years" in response.json()["detail"]
        assert db.scalar(select(func.count(ComplianceItem.id))) == before

    def test_a_half_given_window_is_measured_against_the_default_other_half(
        self, client: TestClient, auth_headers: dict, a_client: str
    ):
        """Naming only the start still names a span — the end fills in from
        the firm's forward default, and a start in 1990 is just as wide."""
        response = self._generate(
            client, auth_headers, a_client, window_start="1990-01-01"
        )
        assert response.status_code == 422, response.text

    def test_a_backfill_inside_the_cap_still_works(
        self, client: TestClient, auth_headers: dict, a_client: str
    ):
        """The case the cap must not break: picking up a client whose returns
        the firm is taking over partway through."""
        start = clock.today() - timedelta(days=730)
        response = self._generate(
            client,
            auth_headers,
            a_client,
            window_start=start.isoformat(),
            window_end=clock.today().isoformat(),
        )
        assert response.status_code == 200, response.text
        assert response.json()["created"] > 0

    def test_a_window_exactly_at_the_cap_is_allowed(
        self, db: Session, registered_firm: dict
    ):
        """The boundary is inclusive, so the documented five years is five
        years rather than a day less."""
        start = date(2026, 1, 1)
        compliance_generator.check_window(
            start, add_months(start, compliance_generator.MAX_GENERATION_WINDOW_MONTHS)
        )
        with pytest.raises(compliance_generator.InvalidWindow):
            compliance_generator.check_window(
                start,
                add_months(start, compliance_generator.MAX_GENERATION_WINDOW_MONTHS)
                + timedelta(days=1),
            )

    def test_the_default_window_is_never_second_guessed(
        self, db: Session, registered_firm: dict
    ):
        """Only a window a caller named is checked. The default is ours and is
        already bounded — and a client onboarded with a future date produces a
        backwards default, which has always quietly generated nothing and is
        not this endpoint's business to start refusing client creation over.
        """
        model_client = make_model_client(
            firm_id=uuid.UUID(registered_firm["firm"]["id"]),
            gst_registered=True,
            onboarded_on=date(2099, 1, 1),
        )
        db.add(model_client)
        db.flush()

        result = generate_compliance_items(db, model_client, today=date(2026, 7, 1))

        assert result.created == []


class TestAFirmsOwnComplianceTypes:
    """A firm may add types of its own, and only it should ever see them.

    Every seeded type is system-wide, so a suite exercising only the seeds
    never puts a row with a ``firm_id`` in front of the query — and the
    firm-matching half of that filter survived being inverted. Inverted, a
    firm's own type is withheld from the firm that defined it and handed to
    everyone else, which is both a missing filing and a leak of what a
    competitor files for their clients.
    """

    def make_custom_type(self, db: Session, firm_id: uuid.UUID) -> ComplianceType:
        custom = ComplianceType(
            firm_id=firm_id,
            code="RETAINER_REVIEW",
            name="Quarterly retainer review",
            category=ComplianceCategory.OTHER,
            frequency=Frequency.QUARTERLY,
            due_day=15,
            due_month_offset=1,
            due_overrides={},
            applicability_rule="always",
            default_fee_paise=500_000,
            reminder_offsets_days=[7],
            required_documents=[],
            is_system=False,
        )
        db.add(custom)
        db.flush()
        return custom

    def test_the_defining_firms_client_gets_it(self, db: Session, registered_firm: dict):
        firm_id = uuid.UUID(registered_firm["firm"]["id"])
        self.make_custom_type(db, firm_id)

        model_client = make_model_client(firm_id=firm_id)
        codes = {ct.code for ct in applicable_types(db, model_client)}
        assert "RETAINER_REVIEW" in codes

    def test_another_firms_client_does_not(
        self, client: TestClient, registered_firm: dict, db: Session
    ):
        self.make_custom_type(db, uuid.UUID(registered_firm["firm"]["id"]))

        other = client.post(f"{API}/auth/register", json=SECOND_FIRM).json()

        theirs = make_model_client(firm_id=uuid.UUID(other["firm"]["id"]))
        codes = {ct.code for ct in applicable_types(db, theirs)}
        assert "RETAINER_REVIEW" not in codes
        assert "GSTR3B_MONTHLY" not in codes  # not GST-registered
        assert codes, "the system-wide calendar should still reach them"

    def test_a_type_the_firm_switched_off_is_not_generated(
        self, db: Session, registered_firm: dict
    ):
        firm_id = uuid.UUID(registered_firm["firm"]["id"])
        custom = self.make_custom_type(db, firm_id)
        custom.is_active = False
        db.flush()

        model_client = make_model_client(firm_id=firm_id)
        assert "RETAINER_REVIEW" not in {ct.code for ct in applicable_types(db, model_client)}


class TestToppingUpAWholeFirm:
    """``regenerate_for_firm``, which the nightly job and the CLI both run.

    It selects the clients to top up, and both halves of that selection went
    untested: the firm it belongs to and whether the client is still active.
    Either one inverted and the job writes compliance items for clients that
    are not the caller's — across a tenant boundary, unattended, every night.
    """

    def add_client(self, db: Session, firm_id: uuid.UUID, name: str, **overrides) -> ClientModel:
        model_client = make_model_client(
            firm_id=firm_id, name=name, gst_registered=True, onboarded_on=date(2026, 4, 1), **overrides
        )
        db.add(model_client)
        db.flush()
        return model_client

    def two_firms(
        self, client: TestClient, registered_firm: dict
    ) -> tuple[uuid.UUID, uuid.UUID]:
        other = client.post(f"{API}/auth/register", json=SECOND_FIRM).json()
        return uuid.UUID(registered_firm["firm"]["id"]), uuid.UUID(other["firm"]["id"])

    def test_only_the_named_firms_clients_are_topped_up(
        self, client: TestClient, registered_firm: dict, db: Session
    ):
        mine, theirs = self.two_firms(client, registered_firm)
        ours = self.add_client(db, mine, "Ours Ltd")
        not_ours = self.add_client(db, theirs, "Theirs Ltd")

        created = regenerate_for_firm(db, mine, today=date(2026, 7, 1))
        db.flush()

        assert created > 0
        assert items_of(db, ours.id), "the firm's own client was not topped up"
        assert not items_of(db, not_ours.id), "another firm's client was written to"

    def test_a_deactivated_client_is_left_alone(
        self, client: TestClient, registered_firm: dict, db: Session
    ):
        """Deactivation is how a firm records that a client has left.

        Generating for them puts deadlines back on the calendar the firm just
        cleared, and each one carries a fee onto the billable pile.
        """
        mine, _ = self.two_firms(client, registered_firm)
        active = self.add_client(db, mine, "Still With Us Ltd")
        departed = self.add_client(db, mine, "Moved On Ltd", is_active=False)

        regenerate_for_firm(db, mine, today=date(2026, 7, 1))
        db.flush()

        assert items_of(db, active.id)
        assert not items_of(db, departed.id)

    def test_a_firm_with_no_clients_creates_nothing_and_says_so(
        self, db: Session, registered_firm: dict
    ):
        firm_id = uuid.UUID(registered_firm["firm"]["id"])
        assert regenerate_for_firm(db, firm_id, today=date(2026, 7, 1)) == 0

    def test_the_count_is_the_rows_actually_written(
        self, client: TestClient, registered_firm: dict, db: Session
    ):
        mine, _ = self.two_firms(client, registered_firm)
        first = self.add_client(db, mine, "One Ltd")
        second = self.add_client(db, mine, "Two Ltd")

        created = regenerate_for_firm(db, mine, today=date(2026, 7, 1))
        db.flush()

        assert created == len(items_of(db, first.id)) + len(items_of(db, second.id))

    def test_a_second_run_on_the_same_date_adds_nothing(
        self, client: TestClient, registered_firm: dict, db: Session
    ):
        mine, _ = self.two_firms(client, registered_firm)
        self.add_client(db, mine, "One Ltd")

        assert regenerate_for_firm(db, mine, today=date(2026, 7, 1)) > 0
        db.commit()
        assert regenerate_for_firm(db, mine, today=date(2026, 7, 1)) == 0


class TestClientListingAndDetail:
    def test_list_is_scoped_paginated_and_searchable(
        self, client: TestClient, auth_headers: dict
    ):
        client.post(f"{API}/clients", headers=auth_headers, json=make_client_payload())
        client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                name="Aurora Foods LLP", entity_type="llp", pan="AABFA1111K", gstin=None
            ),
        )

        listing = client.get(f"{API}/clients", headers=auth_headers).json()
        assert listing["total"] == 2
        assert [c["name"] for c in listing["items"]] == ["Aurora Foods LLP", "Nimbus Textiles Pvt Ltd"]

        found = client.get(f"{API}/clients?search=Aurora", headers=auth_headers).json()
        assert found["total"] == 1

        page = client.get(f"{API}/clients?limit=1&offset=1", headers=auth_headers).json()
        assert len(page["items"]) == 1
        assert page["total"] == 2

    def test_filter_by_gst_registration(self, client: TestClient, auth_headers: dict):
        client.post(f"{API}/clients", headers=auth_headers, json=make_client_payload())
        client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                name="No GST Co", pan="AAAPN2222B", gstin=None, gst_registered=False
            ),
        )
        result = client.get(f"{API}/clients?gst_registered=false", headers=auth_headers).json()
        assert result["total"] == 1
        assert result["items"][0]["name"] == "No GST Co"

    def test_detail_includes_compliance_summary(self, client: TestClient, auth_headers: dict):
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()
        detail = client.get(
            f"{API}/clients/{created['client']['id']}", headers=auth_headers
        ).json()
        assert detail["name"] == "Nimbus Textiles Pvt Ltd"
        summary = detail["compliance_summary"]
        assert summary["total"] == created["compliance_items_created"]
        assert summary["filed"] == 0

    def test_missing_client_is_404(self, client: TestClient, auth_headers: dict):
        response = client.get(
            f"{API}/clients/99999999-9999-9999-9999-999999999999", headers=auth_headers
        )
        assert response.status_code == 404

    def test_deactivate_marks_open_items_not_applicable(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()
        client_id = created["client"]["id"]

        assert client.delete(f"{API}/clients/{client_id}", headers=auth_headers).status_code == 204

        detail = client.get(f"{API}/clients/{client_id}", headers=auth_headers).json()
        assert detail["is_active"] is False

        statuses = {
            item.status
            for item in db.scalars(
                select(ComplianceItem).where(ComplianceItem.client_id == uuid.UUID(client_id))
            ).all()
        }
        assert statuses == {ComplianceStatus.NOT_APPLICABLE}


class TestOffBoardingMeansTheSameThingByEitherDoor:
    """``DELETE /clients/{id}`` and ``PATCH {"is_active": false}`` both off-board.

    They are the same transition — the firm has stopped acting for this client
    — and the UI reaches for whichever is nearer. Only the delete closed the
    open filings, so a client off-boarded through the patch kept every
    obligation: they stayed on the firm's dashboard and calendar, went overdue
    there, and no amount of editing the client would clear them.
    """

    @staticmethod
    def onboard(client: TestClient, auth_headers: dict) -> str:
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()
        assert created["compliance_items_created"] > 0
        return created["client"]["id"]

    @staticmethod
    def statuses(db: Session, client_id: str) -> set[ComplianceStatus]:
        return {
            item.status
            for item in db.scalars(
                select(ComplianceItem).where(ComplianceItem.client_id == uuid.UUID(client_id))
            ).all()
        }

    def test_patching_a_client_inactive_closes_their_open_filings(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = self.onboard(client, auth_headers)

        response = client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": False}
        )
        assert response.status_code == 200
        assert response.json()["client"]["is_active"] is False

        assert self.statuses(db, client_id) == {ComplianceStatus.NOT_APPLICABLE}

    def test_the_dashboard_stops_counting_a_client_the_firm_has_let_go(
        self, client: TestClient, auth_headers: dict
    ):
        client_id = self.onboard(client, auth_headers)
        before = client.get(f"{API}/compliance/dashboard", headers=auth_headers).json()
        assert before["overdue"] + before["due_soon"] + before["upcoming"] > 0

        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": False}
        )

        after = client.get(f"{API}/compliance/dashboard", headers=auth_headers).json()
        assert after["active_clients"] == 0
        assert (after["overdue"], after["due_soon"], after["upcoming"]) == (0, 0, 0)

    def test_the_close_is_recorded_so_taking_them_back_on_undoes_it(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The same reversibility the delete has — the patch must not lose it."""
        client_id = self.onboard(client, auth_headers)
        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": False}
        )

        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": True}
        )
        assert ComplianceStatus.PENDING in self.statuses(db, client_id)
        assert ComplianceStatus.NOT_APPLICABLE not in self.statuses(db, client_id)

    def test_an_edit_that_does_not_touch_is_active_leaves_the_filings_alone(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = self.onboard(client, auth_headers)

        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"notes": "Moved office"}
        )
        assert self.statuses(db, client_id) == {ComplianceStatus.PENDING}

    def test_re_saving_an_already_inactive_client_is_not_a_second_off_boarding(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A practitioner marks the item not-applicable; the re-save must not claim it.

        Only the transition shelves. Off-boarding a client who is already off
        would stamp ``offboarded_from_status`` onto rows it never closed, and
        reactivation would then "restore" a judgement someone made by hand.
        """
        client_id = self.onboard(client, auth_headers)
        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": False}
        )
        item = db.scalars(
            select(ComplianceItem).where(ComplianceItem.client_id == uuid.UUID(client_id))
        ).first()
        item.offboarded_from_status = None  # a hand-made not-applicable
        db.commit()

        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": False}
        )
        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": True}
        )

        db.refresh(item)
        assert item.status == ComplianceStatus.NOT_APPLICABLE

    # ------------------------------------------------------------------ #
    # The queued chases, which are the other half of the same instruction.

    @staticmethod
    def queue_a_chase(client: TestClient, auth_headers: dict, client_id: str) -> str:
        """A reminder still waiting to go out for this client."""
        response = client.post(
            f"{API}/reminders",
            headers=auth_headers,
            json={
                "client_id": client_id,
                "reminder_type": "custom",
                "subject": "Your GSTR-3B",
                "body": "A quick note about the return due this month.",
                "scheduled_for": (
                    clock.today() + timedelta(days=3)
                ).isoformat() + "T09:00:00Z",
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    @staticmethod
    def reminder_status(db: Session, reminder_id: str) -> ReminderStatus:
        db.expire_all()
        return db.get(Reminder, uuid.UUID(reminder_id)).status

    def test_patching_a_client_inactive_cancels_their_queued_reminders(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The delete door cancelled them; the patch door left them stuck.

        Nothing is sent to an off-boarded client — the dispatcher joins the
        client row and requires ``is_active`` — so a reminder left ``scheduled``
        can never be claimed: never sent, never failed, never withdrawn, and
        counted in the pending badge for ever.
        """
        client_id = self.onboard(client, auth_headers)
        reminder_id = self.queue_a_chase(client, auth_headers, client_id)

        response = client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": False}
        )
        assert response.status_code == 200

        assert self.reminder_status(db, reminder_id) == ReminderStatus.CANCELLED

    def test_both_doors_leave_the_queue_in_the_same_state(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Whichever the UI reaches for, the client stops being written to."""
        through_patch = self.onboard(client, auth_headers)
        patched = self.queue_a_chase(client, auth_headers, through_patch)
        client.patch(
            f"{API}/clients/{through_patch}",
            headers=auth_headers,
            json={"is_active": False},
        )

        through_delete = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(name="Vaidya Exports LLP", pan="AABCV3456Q"),
        ).json()["client"]["id"]
        deleted = self.queue_a_chase(client, auth_headers, through_delete)
        client.delete(f"{API}/clients/{through_delete}", headers=auth_headers)

        assert self.reminder_status(db, patched) == self.reminder_status(db, deleted)
        assert self.reminder_status(db, patched) == ReminderStatus.CANCELLED

    def test_the_pending_badge_clears_when_a_client_is_let_go(
        self, client: TestClient, auth_headers: dict
    ):
        """The screen a practitioner checks must not show undeliverable work."""
        client_id = self.onboard(client, auth_headers)
        self.queue_a_chase(client, auth_headers, client_id)
        before = client.get(f"{API}/reminders/pending-count", headers=auth_headers).json()
        assert before["scheduled"] == 1

        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": False}
        )

        after = client.get(f"{API}/reminders/pending-count", headers=auth_headers).json()
        assert after["scheduled"] == 0

    def test_a_reminder_already_sent_is_left_as_sent(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """What went out happened, and the trail has to keep saying so."""
        client_id = self.onboard(client, auth_headers)
        reminder_id = self.queue_a_chase(client, auth_headers, client_id)
        sent = db.get(Reminder, uuid.UUID(reminder_id))
        sent.status = ReminderStatus.SENT
        db.commit()

        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": False}
        )

        assert self.reminder_status(db, reminder_id) == ReminderStatus.SENT

    def test_the_patch_records_what_it_cancelled(
        self, client: TestClient, auth_headers: dict
    ):
        """The audit line says what the off-boarding closed, by either door."""
        client_id = self.onboard(client, auth_headers)
        self.queue_a_chase(client, auth_headers, client_id)
        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": False}
        )

        entry = client.get(
            f"{API}/audit", params={"action": "client.update"}, headers=auth_headers
        ).json()["items"][0]
        assert "cancelled 1 scheduled reminder(s)" in entry["summary"]

    def test_an_edit_that_does_not_off_board_leaves_the_queue_alone(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Only the transition. Renaming a client is not stopping the chase."""
        client_id = self.onboard(client, auth_headers)
        reminder_id = self.queue_a_chase(client, auth_headers, client_id)

        client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"contact_person": "Meera Iyer"},
        )

        assert self.reminder_status(db, reminder_id) == ReminderStatus.SCHEDULED

    def test_reactivation_does_not_resurrect_a_cancelled_chase(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """It was queued against a deadline that has since been shelved.

        The sweeps raise whatever is genuinely outstanding on their next run,
        which is the same position ``DELETE`` already took.
        """
        client_id = self.onboard(client, auth_headers)
        reminder_id = self.queue_a_chase(client, auth_headers, client_id)
        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": False}
        )
        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": True}
        )

        assert self.reminder_status(db, reminder_id) == ReminderStatus.CANCELLED


class TestTenantIsolation:
    def test_a_firm_cannot_see_another_firms_clients(
        self, client: TestClient, auth_headers: dict
    ):
        client.post(f"{API}/clients", headers=auth_headers, json=make_client_payload())

        other = client.post(
            f"{API}/auth/register",
            json={
                "firm_name": "Iyer & Co",
                "firm_email": "office@iyer-ca.in",
                "owner_full_name": "Suresh Iyer",
                "owner_email": "suresh@iyer-ca.in",
                "owner_password": "another-strong-password",
            },
        ).json()
        other_headers = {"Authorization": f"Bearer {other['access_token']}"}

        listing = client.get(f"{API}/clients", headers=other_headers).json()
        assert listing["total"] == 0

    def test_cross_firm_client_fetch_is_404(self, client: TestClient, auth_headers: dict):
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()

        other = client.post(
            f"{API}/auth/register",
            json={
                "firm_name": "Iyer & Co",
                "firm_email": "office@iyer-ca.in",
                "owner_full_name": "Suresh Iyer",
                "owner_email": "suresh@iyer-ca.in",
                "owner_password": "another-strong-password",
            },
        ).json()
        other_headers = {"Authorization": f"Bearer {other['access_token']}"}

        response = client.get(
            f"{API}/clients/{created['client']['id']}", headers=other_headers
        )
        assert response.status_code == 404


class TestClientRolePermissions:
    """Onboarding and amending a client is manager-and-above.

    A junior does the filing work but does not decide who the firm acts for, or
    which obligations a client carries — changing a registration flag silently
    rewrites the compliance calendar. The web UI hides these controls from a
    junior; these tests are what makes that a guarantee rather than a courtesy.
    """

    @staticmethod
    def junior_headers(client: TestClient, auth_headers: dict) -> dict[str, str]:
        client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "Junior Jain",
                "email": "junior@sharma-ca.in",
                "password": "junior-password-1",
                "role": "junior",
            },
        )
        token = client.post(
            f"{API}/auth/login",
            json={"email": "junior@sharma-ca.in", "password": "junior-password-1"},
        ).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def test_a_junior_cannot_onboard_a_client(self, client: TestClient, auth_headers: dict):
        headers = self.junior_headers(client, auth_headers)

        response = client.post(f"{API}/clients", headers=headers, json=make_client_payload())

        assert response.status_code == 403
        assert "Insufficient permissions" in response.json()["detail"]

    def test_a_junior_cannot_amend_a_client(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        headers = self.junior_headers(client, auth_headers)

        response = client.patch(
            f"{API}/clients/{created_client['id']}",
            headers=headers,
            json={"name": "Renamed By Junior"},
        )

        assert response.status_code == 403

    def test_a_junior_cannot_deactivate_a_client(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        headers = self.junior_headers(client, auth_headers)

        response = client.delete(f"{API}/clients/{created_client['id']}", headers=headers)

        assert response.status_code == 403
        # The refusal is total: the client is still on the books.
        after = client.get(f"{API}/clients/{created_client['id']}", headers=auth_headers)
        assert after.json()["is_active"] is True

    def test_a_junior_cannot_regenerate_the_calendar(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        headers = self.junior_headers(client, auth_headers)

        response = client.post(
            f"{API}/clients/{created_client['id']}/compliance-items",
            headers=headers,
            json={},
        )

        assert response.status_code == 403

    def test_a_junior_can_still_read_the_client_they_work_on(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        headers = self.junior_headers(client, auth_headers)

        assert client.get(f"{API}/clients", headers=headers).status_code == 200
        assert (
            client.get(f"{API}/clients/{created_client['id']}", headers=headers).status_code == 200
        )

    def test_a_manager_can_amend_and_deactivate(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "Meera Manager",
                "email": "meera@sharma-ca.in",
                "password": "manager-password-1",
                "role": "manager",
            },
        )
        token = client.post(
            f"{API}/auth/login",
            json={"email": "meera@sharma-ca.in", "password": "manager-password-1"},
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        patched = client.patch(
            f"{API}/clients/{created_client['id']}",
            headers=headers,
            json={"contact_person": "Someone New"},
        )
        assert patched.status_code == 200
        assert patched.json()["client"]["contact_person"] == "Someone New"

        assert (
            client.delete(f"{API}/clients/{created_client['id']}", headers=headers).status_code
            == 204
        )


class TestTakingAClientBackOn:
    """Off-boarding is reversible, and reversing it has to restore the calendar.

    Deactivating a client closes every open filing as ``not_applicable``.
    Generation is idempotent per (compliance type, period) and reads every item
    whatever its status, so those closed rows count as already generated and are
    never replaced. Reactivation therefore left a client on the books —
    occupying a plan slot, listed as active — owing nothing at all for the
    twelve months already materialised: nothing chased, no task raised, no fee
    billed.
    """

    def _onboard(self, client: TestClient, auth_headers: dict) -> str:
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()
        assert created["compliance_items_created"] > 0
        return created["client"]["id"]

    def _statuses(self, db: Session, client_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in db.scalars(
            select(ComplianceItem).where(ComplianceItem.client_id == uuid.UUID(client_id))
        ).all():
            counts[item.status.value] = counts.get(item.status.value, 0) + 1
        return counts

    def _reactivate(self, client: TestClient, auth_headers: dict, client_id: str):
        response = client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": True}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_the_filings_off_boarding_closed_are_open_again(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = self._onboard(client, auth_headers)
        before = self._statuses(db, client_id)[ComplianceStatus.PENDING.value]

        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)
        assert self._statuses(db, client_id) == {
            ComplianceStatus.NOT_APPLICABLE.value: before
        }

        self._reactivate(client, auth_headers, client_id)

        assert self._statuses(db, client_id) == {ComplianceStatus.PENDING.value: before}

    def test_a_reopened_filing_comes_back_on_the_calendar(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The calendar is the thing a CA actually looks at, so check it there."""
        client_id = self._onboard(client, auth_headers)
        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)
        self._reactivate(client, auth_headers, client_id)

        summary = client.get(
            f"{API}/compliance/calendar",
            params={"from_date": "2020-01-01", "to_date": "2035-12-31", "limit": 1000},
            headers=auth_headers,
        ).json()["summary"]

        assert summary["upcoming"] + summary["due_soon"] + summary["overdue"] > 0

    def test_work_already_under_way_comes_back_as_in_progress(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Not collapsed into pending — a junior's half-done return stays that.

        Off-boarding writes one status over two, so restoring needs the one it
        overwrote rather than a single default.
        """
        client_id = self._onboard(client, auth_headers)
        started = db.scalars(
            select(ComplianceItem).where(ComplianceItem.client_id == uuid.UUID(client_id))
        ).first()
        started.status = ComplianceStatus.IN_PROGRESS
        db.commit()

        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)
        self._reactivate(client, auth_headers, client_id)

        db.refresh(started)
        assert started.status == ComplianceStatus.IN_PROGRESS

    def test_a_filing_the_firm_ruled_out_by_hand_stays_ruled_out(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The distinction the marker exists for.

        ``not_applicable`` is both "this client does not file this" and "we no
        longer act for them". Reopening everything not-applicable would undo a
        judgement the firm made about the filing itself.
        """
        client_id = self._onboard(client, auth_headers)
        ruled_out = db.scalars(
            select(ComplianceItem).where(ComplianceItem.client_id == uuid.UUID(client_id))
        ).first()
        ruled_out.status = ComplianceStatus.NOT_APPLICABLE
        db.commit()

        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)
        self._reactivate(client, auth_headers, client_id)

        db.refresh(ruled_out)
        assert ruled_out.status == ComplianceStatus.NOT_APPLICABLE
        assert ruled_out.offboarded_from_status is None

    def test_a_filing_recorded_while_they_were_away_is_not_reopened(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A return lodged after off-boarding is a record, not an open job."""
        client_id = self._onboard(client, auth_headers)
        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)

        item = db.scalars(
            select(ComplianceItem).where(ComplianceItem.client_id == uuid.UUID(client_id))
        ).first()
        item.status = ComplianceStatus.FILED
        item.filed_on = date(2026, 5, 1)
        db.commit()

        self._reactivate(client, auth_headers, client_id)

        db.refresh(item)
        assert item.status == ComplianceStatus.FILED
        assert item.offboarded_from_status is None

    def test_the_gap_they_were_away_for_is_filled_in(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Reopening covers the periods off-boarding closed, and nothing later.

        Generation only ever runs for active clients, so a client away while new
        periods fell due has a hole that no reopen can fill. Reactivation tops
        up as well, and reports how many that was.
        """
        client_id = self._onboard(client, auth_headers)
        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)

        # Drop the far end of the calendar, standing in for periods that had
        # not been generated yet when they left.
        horizon = add_months(clock.today(), settings.compliance_generation_months - 1)
        dropped = (
            db.query(ComplianceItem)
            .filter(
                ComplianceItem.client_id == uuid.UUID(client_id),
                ComplianceItem.due_date > horizon,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        assert dropped > 0

        response = self._reactivate(client, auth_headers, client_id)

        assert response["compliance_items_created"] == dropped

    def test_switching_them_off_again_closes_and_marks_afresh(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Off, on, off — the second close has to record itself like the first."""
        client_id = self._onboard(client, auth_headers)
        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)
        self._reactivate(client, auth_headers, client_id)
        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)

        self._reactivate(client, auth_headers, client_id)

        assert ComplianceStatus.PENDING.value in self._statuses(db, client_id)

    def test_an_ordinary_edit_does_not_reopen_anything(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Only the transition. Re-saving an active client changes no statuses."""
        client_id = self._onboard(client, auth_headers)
        before = self._statuses(db, client_id)

        patched = client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"is_active": True, "contact_person": "Someone New"},
        )
        assert patched.status_code == 200
        assert patched.json()["compliance_items_created"] == 0
        assert self._statuses(db, client_id) == before


class TestARegistrationTheClientNoLongerHolds:
    """Generation only ever added, and a registration is not for ever.

    A client who surrenders their GST registration, moves to QRMP, or converts
    from a company to an LLP keeps every filing the old registration had
    already materialised — a year of them. They sit pending on the calendar,
    are counted on the dashboard, go overdue one by one, raise tasks, and email
    the client asking for the paperwork behind a return nobody owes. Marked
    filed by someone working down the list, each then carries a fee onto an
    invoice.
    """

    def _onboard(self, client: TestClient, auth_headers: dict, **overrides) -> str:
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload(**overrides)
        ).json()
        assert created["compliance_items_created"] > 0
        return created["client"]["id"]

    def _open_items(self, db: Session, client_id: str, code: str) -> list[ComplianceItem]:
        return [
            item
            for item in items_of(db, uuid.UUID(client_id))
            if item.compliance_type.code == code
            and item.status
            in (ComplianceStatus.PENDING, ComplianceStatus.IN_PROGRESS)
        ]

    def test_surrendering_a_gst_registration_closes_the_filings_ahead(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = self._onboard(client, auth_headers)
        assert self._open_items(db, client_id, "GSTR3B_MONTHLY")

        response = client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"gst_registered": False},
        )
        assert response.status_code == 200, response.text

        remaining = self._open_items(db, client_id, "GSTR3B_MONTHLY")
        assert all(item.period_start <= clock.today() for item in remaining), (
            "a period that has not begun cannot be owed under a surrendered "
            "registration"
        )
        # The rest of the calendar is untouched: this client still files an ITR.
        assert self._open_items(db, client_id, "ITR_AUDIT")

    def test_the_period_the_change_lands_inside_is_left_alone(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A registration surrendered mid-month still owes that month's return.

        Which is the one period this cannot decide — only the practitioner
        knows the effective date — so the rule is to close what cannot have
        arisen and leave the rest to them.
        """
        client_id = self._onboard(client, auth_headers)
        current = [
            item
            for item in self._open_items(db, client_id, "GSTR3B_MONTHLY")
            if item.period_start <= clock.today() <= item.period_end
        ]

        client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"gst_registered": False},
        )

        for item in current:
            db.refresh(item)
            assert item.status == ComplianceStatus.PENDING

    def test_a_filed_return_is_never_withdrawn(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """It is the record of what was lodged, whatever the client registers now."""
        client_id = self._onboard(client, auth_headers)
        ahead = max(
            self._open_items(db, client_id, "GSTR3B_MONTHLY"),
            key=lambda item: item.due_date,
        )
        ahead.status = ComplianceStatus.FILED
        ahead.filed_on = clock.today()
        db.commit()

        client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"gst_registered": False},
        )

        db.refresh(ahead)
        assert ahead.status == ComplianceStatus.FILED

    def test_moving_to_qrmp_swaps_monthly_returns_for_quarterly_ones(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The commonest change of all, and the one that doubles up worst."""
        client_id = self._onboard(client, auth_headers)

        response = client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"gst_filing_frequency": "quarterly"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["compliance_items_created"] > 0

        monthly_ahead = [
            item
            for item in self._open_items(db, client_id, "GSTR3B_MONTHLY")
            if item.period_start > clock.today()
        ]
        assert not monthly_ahead
        assert self._open_items(db, client_id, "GSTR3B_QUARTERLY")

    def test_registering_again_reinstates_what_the_change_closed(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A flag switched back on has to bring the filings back with it.

        Generation skips a (type, period) that already exists whatever its
        status, so without this the client would be GST-registered and owe no
        GST returns for every period already materialised.
        """
        client_id = self._onboard(client, auth_headers)
        before = {item.id for item in self._open_items(db, client_id, "GSTR3B_MONTHLY")}

        client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"gst_registered": False},
        )
        client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"gst_registered": True},
        )

        after = {item.id for item in self._open_items(db, client_id, "GSTR3B_MONTHLY")}
        assert after == before

    def test_work_already_under_way_comes_back_as_it_was(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Not collapsed into pending — the same reason off-boarding records it."""
        client_id = self._onboard(client, auth_headers)
        started = max(
            self._open_items(db, client_id, "GSTR3B_MONTHLY"),
            key=lambda item: item.due_date,
        )
        started.status = ComplianceStatus.IN_PROGRESS
        db.commit()

        client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"gst_registered": False},
        )
        db.refresh(started)
        assert started.status == ComplianceStatus.NOT_APPLICABLE

        client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"gst_registered": True},
        )
        db.refresh(started)
        assert started.status == ComplianceStatus.IN_PROGRESS

    def test_an_item_a_practitioner_ruled_out_stays_ruled_out(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Their judgement about one filing is not this mechanism's to undo.

        Only what a registration change closed carries the marker, so a
        hand-marked item has nothing to reinstate it.
        """
        client_id = self._onboard(client, auth_headers)
        by_hand = self._open_items(db, client_id, "GSTR3B_MONTHLY")[0]
        by_hand.status = ComplianceStatus.NOT_APPLICABLE
        db.commit()

        client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"gst_registered": False},
        )
        client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"gst_registered": True},
        )

        db.refresh(by_hand)
        assert by_hand.status == ComplianceStatus.NOT_APPLICABLE

    def test_an_off_boarded_client_is_not_reopened_by_a_flag_edit(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Off-boarding closed everything; a registration edit must not undo it."""
        client_id = self._onboard(client, auth_headers)
        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)

        response = client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"tds_applicable": False},
        )
        assert response.status_code == 200, response.text

        statuses = {item.status for item in items_of(db, uuid.UUID(client_id))}
        assert statuses == {ComplianceStatus.NOT_APPLICABLE}

    def test_an_edit_that_touches_no_registration_changes_nothing(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = self._onboard(client, auth_headers)
        before = {item.id: item.status for item in items_of(db, uuid.UUID(client_id))}

        client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"contact_person": "Someone New"},
        )

        after = {item.id: item.status for item in items_of(db, uuid.UUID(client_id))}
        assert after == before

    def test_the_withdrawal_is_recorded_in_the_trail(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A firm's calendar losing a year of filings is not a silent change."""
        client_id = self._onboard(client, auth_headers)

        client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"gst_registered": False},
        )

        entries = client.get(
            f"{API}/audit", headers=auth_headers, params={"entity_id": client_id}
        ).json()["items"]
        summaries = [entry["summary"] for entry in entries]
        assert any("withdrew" in summary for summary in summaries), summaries


class TestOffBoardingAndARegistrationChangeAreDifferentCloses:
    """``offboarded_from_status`` is written by two mechanisms, not one.

    Off-boarding closes every open filing; a registration change closes the
    ones the client's registrations no longer call for. Both record what the
    item was, in the same column, because both have to be reversible — and
    reading that column alone cannot say which close set it.

    Reactivation reopened everything carrying the marker, so a client who
    surrendered their GST registration and was later off-boarded came back
    GST-free and holding a year of GST returns again: pending on the calendar,
    counted on the dashboard, going overdue one by one, raising tasks, and
    emailing the client for the paperwork behind a return nobody owes. Marked
    filed by someone working down the list, each then carries a fee onto an
    invoice.
    """

    def _onboard(self, client: TestClient, auth_headers: dict, **overrides) -> str:
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload(**overrides)
        ).json()
        assert created["compliance_items_created"] > 0
        return created["client"]["id"]

    def _of_type(self, db: Session, client_id: str, code: str) -> list[ComplianceItem]:
        return [
            item
            for item in items_of(db, uuid.UUID(client_id))
            if item.compliance_type.code == code
        ]

    def _ahead(self, db: Session, client_id: str, code: str) -> list[ComplianceItem]:
        return [
            item
            for item in self._of_type(db, client_id, code)
            if item.period_start > clock.today()
        ]

    def _patch(self, client: TestClient, auth_headers: dict, client_id: str, **body):
        response = client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json=body
        )
        assert response.status_code == 200, response.text
        return response.json()

    def _surrender_then_offboard(
        self, client: TestClient, auth_headers: dict, db: Session
    ) -> tuple[str, list[ComplianceItem]]:
        client_id = self._onboard(client, auth_headers)
        self._patch(client, auth_headers, client_id, gst_registered=False)
        ahead = self._ahead(db, client_id, "GSTR3B_MONTHLY")
        assert ahead
        assert all(i.status == ComplianceStatus.NOT_APPLICABLE for i in ahead)
        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)
        return client_id, ahead

    def test_a_surrendered_registration_survives_the_round_trip(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id, ahead = self._surrender_then_offboard(client, auth_headers, db)

        self._patch(client, auth_headers, client_id, is_active=True)

        for item in ahead:
            db.refresh(item)
            assert item.status == ComplianceStatus.NOT_APPLICABLE, (
                f"{item.period_label} came back for a client who is not GST registered"
            )

    def test_the_filings_they_do_still_owe_come_back(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Withholding the GST returns must not withhold the rest of the calendar."""
        client_id, _ = self._surrender_then_offboard(client, auth_headers, db)

        self._patch(client, auth_headers, client_id, is_active=True)

        open_itr = [
            item
            for item in self._of_type(db, client_id, "ITR_AUDIT")
            if item.status in (ComplianceStatus.PENDING, ComplianceStatus.IN_PROGRESS)
        ]
        assert open_itr

    def test_registering_again_still_brings_them_back(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The marker is kept, not cleared, on what is held back.

        Clearing it would leave those filings closed for good: generation skips
        a (type, period) that already exists whatever its status, so nothing
        else would ever raise them again.
        """
        client_id, ahead = self._surrender_then_offboard(client, auth_headers, db)
        self._patch(client, auth_headers, client_id, is_active=True)

        self._patch(client, auth_headers, client_id, gst_registered=True)

        for item in ahead:
            db.refresh(item)
            assert item.status == ComplianceStatus.PENDING

    def test_the_work_raised_for_a_withheld_filing_stays_cancelled(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A task follows its filing. Reinstating one without the other puts a
        deadline back on a practitioner's queue for a return nobody owes."""
        client_id = self._onboard(client, auth_headers)
        ahead = self._ahead(db, client_id, "GSTR3B_MONTHLY")
        target = min(ahead, key=lambda item: item.due_date)
        task = Task(
            firm_id=target.firm_id,
            client_id=target.client_id,
            compliance_item_id=target.id,
            title="File GSTR-3B",
            status=TaskStatus.TODO,
            due_date=target.due_date,
        )
        db.add(task)
        db.commit()

        self._patch(client, auth_headers, client_id, gst_registered=False)
        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)
        self._patch(client, auth_headers, client_id, is_active=True)

        db.refresh(task)
        assert task.status == TaskStatus.CANCELLED
        assert task.withdrawn_from_status == TaskStatus.TODO

    def test_a_flag_switched_off_while_they_were_away_is_honoured(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Nothing reconciles a registration edit made to an off-boarded client
        — every filing is already closed — so the reopen is where it lands."""
        client_id = self._onboard(client, auth_headers)
        ahead = self._ahead(db, client_id, "GSTR3B_MONTHLY")
        assert ahead

        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)
        self._patch(client, auth_headers, client_id, gst_registered=False)
        self._patch(client, auth_headers, client_id, is_active=True)

        for item in ahead:
            db.refresh(item)
            assert item.status == ComplianceStatus.NOT_APPLICABLE

    def test_an_ordinary_off_boarding_is_unaffected(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """No registration changed, so everything closed comes back."""
        client_id = self._onboard(client, auth_headers)
        before = {
            item.id
            for item in items_of(db, uuid.UUID(client_id))
            if item.status == ComplianceStatus.PENDING
        }

        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)
        self._patch(client, auth_headers, client_id, is_active=True)

        after = {
            item.id
            for item in items_of(db, uuid.UUID(client_id))
            if item.status == ComplianceStatus.PENDING
        }
        assert after == before


class TestCountingAClientsFilingsInTheDatabase:
    """The five counters on a client's detail page are aggregates, and they
    were computed by reading every filing the client has ever had.

    A filing is a permanent record and the nightly generator materialises a
    year of them ahead of time, so a client a firm has acted for a few years
    carries several hundred rows — every one hydrated into a mapped object on
    each visit, to be reduced to five integers. It is the curve the dashboard
    and the workload view were already taken off, on the page a practitioner
    opens before every call with a client.

    The counters have not moved; only where they are computed has.
    """

    @staticmethod
    def _summary(client: TestClient, auth_headers: dict, client_id: str) -> dict:
        response = client.get(f"{API}/clients/{client_id}", headers=auth_headers)
        assert response.status_code == 200, response.text
        return response.json()["compliance_summary"]

    def test_the_filings_are_counted_rather_than_read(
        self, client: TestClient, auth_headers: dict
    ):
        """The point of the change, asserted directly. Anything the detail page
        asks of ``compliance_items`` must come back as an aggregate."""
        from sqlalchemy import event

        from app.database import engine

        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()["client"]
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()).lower())

        event.listen(engine, "before_cursor_execute", record)
        try:
            self._summary(client, auth_headers, created["id"])
        finally:
            event.remove(engine, "before_cursor_execute", record)

        touched = [sql for sql in statements if "compliance_items" in sql]
        assert touched, "the client detail page never looked at the filings"
        unaggregated = [
            sql for sql in touched if "count(" not in sql and "sum(" not in sql
        ]
        assert not unaggregated, f"rows read instead of counted: {unaggregated}"

    def test_every_bucket_lands_where_it_did(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()["client"]
        items = list(
            db.scalars(
                select(ComplianceItem)
                .where(ComplianceItem.client_id == uuid.UUID(created["id"]))
                .order_by(ComplianceItem.due_date)
            ).all()
        )
        assert len(items) >= 5, "not enough generated filings to bucket"
        today = clock.today()

        items[0].status = ComplianceStatus.FILED
        items[0].filed_on = today
        items[1].status = ComplianceStatus.DELAYED_FILED
        items[1].filed_on = today
        items[2].status = ComplianceStatus.NOT_APPLICABLE
        items[3].due_date = today - timedelta(days=1)
        items[4].due_date = today + timedelta(days=3)
        for spare in items[5:]:
            spare.due_date = today + timedelta(days=90)
        db.commit()

        summary = self._summary(client, auth_headers, created["id"])

        assert summary["total"] == len(items)
        assert summary["filed"] == 2
        assert summary["overdue"] == 1
        assert summary["due_soon"] == 1
        # Everything open, whichever of the three states it is in — and a
        # not-applicable filing is neither owed nor filed, so it appears only
        # in the total.
        assert summary["pending"] == len(items) - 3

    def test_a_filing_due_today_is_due_soon_rather_than_overdue(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The boundary the two date comparisons share."""
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()["client"]
        items = list(
            db.scalars(
                select(ComplianceItem).where(
                    ComplianceItem.client_id == uuid.UUID(created["id"])
                )
            ).all()
        )
        today = clock.today()
        for item in items:
            item.due_date = today + timedelta(days=365)
        items[0].due_date = today
        db.commit()

        summary = self._summary(client, auth_headers, created["id"])

        assert summary["overdue"] == 0
        assert summary["due_soon"] == 1

    def test_another_clients_filings_are_not_counted(
        self, client: TestClient, auth_headers: dict
    ):
        """The aggregate runs in SQL now, so the client predicate is the only
        thing keeping one client's calendar out of another's."""
        first = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()["client"]
        before = self._summary(client, auth_headers, first["id"])

        second = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                name="Second Co", pan="AABCS4321Q", gstin="27AABCS4321Q1Z2"
            ),
        )
        assert second.status_code == 201, second.text

        assert self._summary(client, auth_headers, first["id"]) == before

    def test_a_client_with_no_calendar_reports_zeroes(
        self, client: TestClient, auth_headers: dict
    ):
        """An empty group produces no row at all, so every counter has to come
        back as 0 rather than as a missing key."""
        created = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(generate_compliance_items=False),
        ).json()["client"]

        assert self._summary(client, auth_headers, created["id"]) == {
            "total": 0,
            "pending": 0,
            "overdue": 0,
            "due_soon": 0,
            "filed": 0,
        }


class TestServiceFeeBounds:
    """``service_fees`` is a money field, and it was the one with no bounds.

    It is not merely stored: ``generate_compliance_items`` copies the amount
    onto every filing it materialises for the client, and from there it reaches
    the dashboard's unbilled total and the invoice line the client is billed
    for. All three halves of the field were open — the amount, the key, and the
    number of entries.
    """

    def _post(self, client: TestClient, auth_headers: dict, fees):
        return client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload(service_fees=fees)
        )

    def test_an_amount_beyond_the_money_ceiling_is_refused(
        self, client: TestClient, auth_headers: dict
    ):
        """Unbounded, this reached a BIGINT column and came back a 500 from the
        driver — which is precisely what the ceiling exists to turn into a 422
        naming the field."""
        response = self._post(client, auth_headers, {"GSTR3B_MONTHLY": MAX_AMOUNT_PAISE + 1})

        assert response.status_code == 422, response.text
        assert "service_fees" in response.text

    def test_the_ceiling_itself_is_still_allowed(
        self, client: TestClient, auth_headers: dict
    ):
        assert self._post(
            client, auth_headers, {"GSTR3B_MONTHLY": MAX_AMOUNT_PAISE}
        ).status_code == 201

    def test_a_negative_fee_is_refused(self, client: TestClient, auth_headers: dict):
        """The worse of the two, because nothing downstream rejects it: it
        lands on a year of filings and the dashboard's "unbilled" figure then
        *subtracts* it from what the firm is owed. The number a practice reads
        to find its own missing revenue is wrong in the direction of looking
        fine."""
        response = self._post(client, auth_headers, {"GSTR3B_MONTHLY": -50_000})

        assert response.status_code == 422, response.text

    def test_a_free_text_key_is_refused(self, client: TestClient, auth_headers: dict):
        """A key is this system's own vocabulary — a ``ComplianceType.code`` —
        and a megabyte of anything else was stored per entry in a JSON column
        read on every visit to the client screen."""
        response = self._post(client, auth_headers, {"x" * 500: 1_000})

        assert response.status_code == 422, response.text

    def test_a_key_that_could_never_name_a_filing_is_refused(
        self, client: TestClient, auth_headers: dict
    ):
        response = self._post(client, auth_headers, {"GSTR3B MONTHLY; DROP": 1_000})

        assert response.status_code == 422, response.text

    def test_the_number_of_overrides_is_bounded(
        self, client: TestClient, auth_headers: dict
    ):
        """Twenty thousand entries went in on one request and were read back on
        every one after it."""
        response = self._post(
            client, auth_headers, {f"CODE_{n}": 1_000 for n in range(500)}
        )

        assert response.status_code == 422, response.text

    def test_a_realistic_set_of_overrides_still_goes_through(
        self, client: TestClient, auth_headers: dict
    ):
        response = self._post(
            client,
            auth_headers,
            {"GSTR3B_MONTHLY": 150_000, "GSTR1_MONTHLY": 100_000, "ITR_FILING": 500_000},
        )

        assert response.status_code == 201, response.text
        assert response.json()["client"]["service_fees"]["GSTR3B_MONTHLY"] == 150_000

    def test_the_update_path_is_bounded_too(self, client: TestClient, auth_headers: dict):
        """Bounded as a type rather than per field, so the other door to the
        same column cannot be left open."""
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()["client"]

        response = client.patch(
            f"{API}/clients/{created['id']}",
            headers=auth_headers,
            json={"service_fees": {"GSTR3B_MONTHLY": -1}},
        )
        assert response.status_code == 422, response.text

    def test_an_update_may_still_set_ordinary_overrides(
        self, client: TestClient, auth_headers: dict
    ):
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()["client"]

        response = client.patch(
            f"{API}/clients/{created['id']}",
            headers=auth_headers,
            json={"service_fees": {"GSTR3B_MONTHLY": 250_000}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["client"]["service_fees"] == {"GSTR3B_MONTHLY": 250_000}


class TestAnOnboardingDateTheGeneratorCannotReach:
    """``onboarded_on`` is where the generation window *starts*, not a note.

    ``default_window`` runs from it to ``compliance_generation_months`` past
    today, so a date beyond that horizon makes a window that runs backwards —
    and a backwards window materialises nothing.

    That was reported as success: ``201`` with ``compliance_items_created: 0``,
    which reads exactly like a client who genuinely owes nothing. Nothing
    afterwards notices either, because the monthly top-up recomputes the same
    empty window. The client sits on the books occupying a plan slot with no
    calendar at all — no filing, no task, no reminder, no fee — and the firm
    finds out when the client asks why their return was not filed.

    A mistyped year produces it, which is the digit that gets mistyped in a
    date field, and it is the one typo whose result looks like a healthy
    record.
    """

    def _create(self, client: TestClient, auth_headers, onboarded_on, **overrides):
        payload = make_client_payload(**overrides)
        payload["name"] = "Horizon Traders"
        payload.pop("pan", None)
        payload.pop("gstin", None)
        payload["onboarded_on"] = onboarded_on.isoformat()
        return client.post(f"{API}/clients", json=payload, headers=auth_headers)

    @property
    def horizon(self) -> date:
        return add_months(clock.today(), settings.compliance_generation_months)

    def test_a_mistyped_year_is_refused_rather_than_creating_an_empty_calendar(
        self, client: TestClient, auth_headers
    ):
        response = self._create(client, auth_headers, add_months(clock.today(), 12 * 36))
        assert response.status_code == 422, response.text
        fields = response.json()["error"]["fields"]
        assert [f["field"] for f in fields] == ["onboarded_on"]
        assert "no filings at all" in fields[0]["message"]

    def test_the_refusal_names_the_last_date_that_would_work(
        self, client: TestClient, auth_headers
    ):
        response = self._create(client, auth_headers, self.horizon + timedelta(days=1))
        assert response.status_code == 422, response.text
        assert f"{self.horizon:%d %b %Y}" in response.json()["detail"]

    def test_the_horizon_itself_is_still_accepted(
        self, client: TestClient, auth_headers
    ):
        """Bounded, not banned — the edge belongs to the caller."""
        response = self._create(client, auth_headers, self.horizon)
        assert response.status_code == 201, response.text

    def test_an_engagement_starting_next_month_still_generates_its_calendar(
        self, client: TestClient, auth_headers
    ):
        """The reason this is a horizon rather than a ban.

        A client taken on with effect from the start of next month is ordinary,
        and their calendar has to come with them.
        """
        response = self._create(client, auth_headers, add_months(clock.today(), 1))
        assert response.status_code == 201, response.text
        assert response.json()["compliance_items_created"] > 0

    def test_back_dating_stays_open(self, client: TestClient, auth_headers):
        """Picking up a client whose returns began years ago is ordinary."""
        response = self._create(client, auth_headers, clock.today() - timedelta(days=365 * 8))
        assert response.status_code == 201, response.text
        assert response.json()["compliance_items_created"] > 0

    def test_omitting_it_is_unaffected(self, client: TestClient, auth_headers):
        payload = make_client_payload()
        payload["name"] = "No Date Traders"
        payload.pop("pan", None)
        payload.pop("gstin", None)
        response = client.post(f"{API}/clients", json=payload, headers=auth_headers)
        assert response.status_code == 201, response.text
        assert response.json()["client"]["onboarded_on"] == clock.today().isoformat()


class TestAnExplicitNullOnAClientsRequiredFields:
    """A PATCH body is all optionals, and the optionality carries two meanings.

    ``phone`` is optional because a client may not have one — ``null`` clears
    it, which is an instruction. ``name`` is optional because a PATCH need not
    name it; the column is ``NOT NULL``, so ``null`` is not an instruction, it
    is a value the row cannot hold.

    ``exclude_unset`` cannot separate them — a field explicitly set to ``null``
    is set — so the null reached the database and came back a 409 saying the
    change "conflicts with an existing record". That is what a duplicate PAN
    says, and it is not what happened: a caller reading it goes looking for the
    record it collided with.
    """

    @pytest.mark.parametrize(
        "field",
        [
            "name",
            "entity_type",
            "gst_registered",
            "gst_filing_frequency",
            "tds_applicable",
            "income_tax_applicable",
            "tax_audit_applicable",
            "roc_applicable",
            "payroll_applicable",
            "is_active",
            "service_fees",
        ],
    )
    def test_a_null_is_refused_by_name(
        self, client: TestClient, auth_headers: dict, created_client: dict, field: str
    ):
        response = client.patch(
            f"{API}/clients/{created_client['id']}", headers=auth_headers, json={field: None}
        )

        assert response.status_code == 422, response.text
        assert response.json()["error"]["fields"][0]["field"] == field
        assert "null" in response.json()["detail"]

    def test_the_client_is_left_exactly_as_it_was(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        """The refusal is total — nothing in the same body is applied."""
        response = client.patch(
            f"{API}/clients/{created_client['id']}",
            headers=auth_headers,
            json={"contact_person": "Someone New", "is_active": None},
        )
        assert response.status_code == 422, response.text

        after = client.get(f"{API}/clients/{created_client['id']}", headers=auth_headers).json()
        assert after["contact_person"] == created_client["contact_person"]
        assert after["is_active"] is True

    @pytest.mark.parametrize(
        "field", ["contact_person", "phone", "whatsapp", "address", "notes", "pan"]
    )
    def test_the_genuinely_optional_fields_stay_clearable(
        self, client: TestClient, auth_headers: dict, created_client: dict, field: str
    ):
        """The distinction this draws has to leave the other side working."""
        response = client.patch(
            f"{API}/clients/{created_client['id']}", headers=auth_headers, json={field: None}
        )

        assert response.status_code == 200, response.text
        assert response.json()["client"][field] is None

    def test_omitting_a_required_field_still_leaves_it_alone(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        """``mode="before"`` on a named field only runs when the caller sent
        it, so an absent field is untouched — which is the whole distinction."""
        response = client.patch(
            f"{API}/clients/{created_client['id']}",
            headers=auth_headers,
            json={"contact_person": "Priya Nair"},
        )

        assert response.status_code == 200, response.text
        updated = response.json()["client"]
        assert updated["name"] == created_client["name"]
        assert updated["is_active"] is True


class TestToppingUpAClientTheFirmHasLetGo:
    """Generation knows nothing about off-boarding, so the endpoint has to.

    Off-boarding closes every open filing a client has — that is what
    ``shelve_open_items`` does, and reactivating is what puts them back. The
    generator only ever adds: it materialises whatever the registrations call
    for, at ``pending``, and the shelved rows do not stop it because they only
    cover periods that already existed.

    So ``POST /clients/{id}/compliance-items`` was the one door back into the
    state the rest of the module takes care to prevent — a firm that has
    stopped acting for a client handed a fresh calendar of obligations for
    them, which nothing sweeps out again because only reactivation reopens a
    shelved calendar.
    """

    def onboard_and_let_go(self, client: TestClient, auth_headers: dict) -> str:
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()
        client_id = created["client"]["id"]
        assert created["compliance_items_created"] > 0
        assert (
            client.delete(f"{API}/clients/{client_id}", headers=auth_headers).status_code
            == 204
        )
        return client_id

    def test_the_calendar_of_an_off_boarded_client_is_not_regenerated(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = self.onboard_and_let_go(client, auth_headers)
        # A window past what was already materialised, which is what a
        # practitioner backfilling or extending a calendar actually sends.
        today = clock.today()
        response = client.post(
            f"{API}/clients/{client_id}/compliance-items",
            headers=auth_headers,
            json={
                "window_start": today.isoformat(),
                "window_end": add_months(today, 36).isoformat(),
            },
        )
        assert response.status_code == 409, response.text
        assert "off-boarded" in response.json()["detail"]

        # And nothing was written: every filing is still where off-boarding
        # left it.
        assert {
            item.status
            for item in db.scalars(
                select(ComplianceItem).where(
                    ComplianceItem.client_id == uuid.UUID(client_id)
                )
            ).all()
        } == {ComplianceStatus.NOT_APPLICABLE}

    def test_the_refusal_names_the_way_back(
        self, client: TestClient, auth_headers: dict
    ):
        client_id = self.onboard_and_let_go(client, auth_headers)
        detail = client.post(
            f"{API}/clients/{client_id}/compliance-items", headers=auth_headers, json={}
        ).json()["detail"]
        assert "reactivate" in detail.lower()

    def test_a_client_taken_back_on_can_be_topped_up_again(
        self, client: TestClient, auth_headers: dict
    ):
        """The refusal is about the standing, not about the client."""
        client_id = self.onboard_and_let_go(client, auth_headers)
        assert (
            client.patch(
                f"{API}/clients/{client_id}",
                headers=auth_headers,
                json={"is_active": True},
            ).status_code
            == 200
        )
        response = client.post(
            f"{API}/clients/{client_id}/compliance-items", headers=auth_headers, json={}
        )
        assert response.status_code == 200, response.text

    def test_an_active_client_is_untouched_by_the_check(
        self, client: TestClient, auth_headers: dict, client_id: str
    ):
        today = clock.today()
        response = client.post(
            f"{API}/clients/{client_id}/compliance-items",
            headers=auth_headers,
            json={
                "window_start": today.isoformat(),
                "window_end": add_months(today, 24).isoformat(),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["created"] > 0


class TestPagingClientsThatShareAName:
    """A name does not identify a client.

    That is the whole premise of ``billing._disambiguate``, and two clients of
    one firm under one name is the ordinary case it exists for: a proprietor
    and their firm, or two group companies under one trading name. Paging on
    the name alone repeats one of them and drops the other; see
    :func:`tests.conftest.paged_order_by`.
    """

    def _namesakes(self, client, auth_headers, count=6) -> int:
        for index in range(count):
            response = client.post(
                f"{API}/clients",
                json=make_client_payload(
                    name="Nimbus Textiles Pvt Ltd",
                    pan=None,
                    gstin=None,
                    email=f"accounts+{index}@nimbustextiles.in",
                ),
                headers=auth_headers,
            )
            assert response.status_code == 201, response.text
        return count

    def test_paging_shows_every_namesake_exactly_once(self, client, auth_headers):
        created = self._namesakes(client, auth_headers)

        seen: list[str] = []
        offset = 0
        while True:
            page = client.get(
                f"{API}/clients",
                params={"limit": 2, "offset": offset},
                headers=auth_headers,
            ).json()
            seen.extend(row["id"] for row in page["items"])
            offset += 2
            if offset >= page["total"]:
                break

        assert len(seen) == created
        assert len(set(seen)) == created, "a client appeared on two pages"

    def test_the_order_the_page_is_taken_in_settles_every_pair_of_rows(
        self, client, auth_headers, recorded_sql
    ):
        self._namesakes(client, auth_headers, count=2)

        recorded_sql.clear()
        assert client.get(f"{API}/clients", headers=auth_headers).status_code == 200

        assert paged_order_by(recorded_sql, "clients").endswith("clients.id")


class TestOneSaveThatBothReactivatesAndChangesARegistration:
    """The ordinary shape of taking a client back on.

    A firm rarely reactivates a client and nothing else — the reason they are
    back is usually that something about them changed, so the flag and the
    registration move in one PATCH. Both branches of the handler then run, and
    the second one reported over the first: the count came from its own
    generation, which is zero, because the flags were applied before either
    branch and the reactivation's generation had already materialised
    everything the new registrations call for.

    So the one request that creates a year of filings answered
    ``compliance_items_created: 0``, and the audit line it wrote said nothing
    about them at all.
    """

    def _off_boarded_non_gst_client(self, client: TestClient, auth_headers: dict) -> str:
        created = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(gst_registered=False, gstin=None),
        ).json()
        client_id = created["client"]["id"]
        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)
        return client_id

    def _take_back_on_as_gst_registered(
        self, client: TestClient, auth_headers: dict, client_id: str
    ):
        response = client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={
                "is_active": True,
                "gst_registered": True,
                "gstin": "27AABCN2345P1Z5",
                "gst_filing_frequency": "monthly",
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_the_count_is_of_every_filing_the_save_created(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = self._off_boarded_non_gst_client(client, auth_headers)
        before = len(items_of(db, uuid.UUID(client_id)))

        response = self._take_back_on_as_gst_registered(client, auth_headers, client_id)

        after = len(items_of(db, uuid.UUID(client_id)))
        assert after > before, "the GST registration should have added filings"
        assert response["compliance_items_created"] == after - before

    def test_the_trail_says_the_filings_were_generated(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The summary is built from the same count, so it went silent too."""
        client_id = self._off_boarded_non_gst_client(client, auth_headers)
        self._take_back_on_as_gst_registered(client, auth_headers, client_id)

        entries = client.get(
            f"{API}/audit",
            params={"action": "client.update", "entity_id": client_id},
            headers=auth_headers,
        ).json()["items"]
        assert entries, "the update should have been recorded"
        assert "new compliance item(s)" in entries[0]["summary"]

    def test_a_plain_reactivation_still_reports_its_own_count(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Nothing was added to the branch that already worked."""
        created = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()
        client_id = created["client"]["id"]
        client.delete(f"{API}/clients/{client_id}", headers=auth_headers)
        before = len(items_of(db, uuid.UUID(client_id)))

        response = client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": True}
        ).json()

        after = len(items_of(db, uuid.UUID(client_id)))
        assert response["compliance_items_created"] == after - before


class TestTheTaxDeductionAccountNumber:
    """A TAN is the third statutory identifier a client record carries.

    It is what a TDS return is filed under, so it is quoted back onto challans
    and correspondence and is not a field anyone re-derives — a wrong one is
    wrong for as long as the client is on the books. Validated on the same
    terms as the PAN and the GSTIN: normalised where it is recognisable, and
    refused where it is not, rather than stored as whatever was typed.
    """

    def test_a_tan_is_upper_cased_and_kept(self, client: TestClient, auth_headers: dict):
        response = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload(tan="mumn12345c")
        )
        assert response.status_code == 201, response.text
        assert response.json()["client"]["tan"] == "MUMN12345C"

    def test_something_that_is_not_a_tan_is_refused(
        self, client: TestClient, auth_headers: dict
    ):
        response = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload(tan="MUM123")
        )
        assert response.status_code == 422, response.text
        assert any(
            field["field"] == "tan" for field in response.json()["error"]["fields"]
        )

    def test_a_blank_tan_is_no_tan_rather_than_an_error(
        self, client: TestClient, auth_headers: dict
    ):
        """A form that submits an empty field means the client has not got one."""
        response = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload(tan="")
        )
        assert response.status_code == 201, response.text
        assert response.json()["client"]["tan"] is None

    def test_a_tan_can_be_corrected_on_an_existing_client(
        self, client: TestClient, auth_headers: dict, client_id: str
    ):
        assert (
            client.patch(
                f"{API}/clients/{client_id}", json={"tan": "MUMN12345C"}, headers=auth_headers
            ).status_code
            == 200
        )
        bad = client.patch(
            f"{API}/clients/{client_id}", json={"tan": "nope"}, headers=auth_headers
        )
        assert bad.status_code == 422

    def test_something_that_is_not_a_gstin_is_refused(
        self, client: TestClient, auth_headers: dict
    ):
        """The other identifier whose refusal nothing exercised."""
        response = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(gstin="27AABCN2345P1Q5", name="Other Co", pan=None),
        )
        assert response.status_code == 422, response.text


class TestNarrowingTheClientList:
    """Two filters on the client screen that nothing else covers."""

    @pytest.fixture
    def two_clients(self, client: TestClient, auth_headers: dict) -> dict[str, str]:
        registered = client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload()
        ).json()["client"]
        unregistered = client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                name="Priya Menon",
                entity_type="individual",
                pan="AABCP1111Q",
                gstin=None,
                gst_registered=False,
                roc_applicable=False,
            ),
        ).json()["client"]
        return {"registered": registered["id"], "unregistered": unregistered["id"]}

    def test_by_gst_registration(self, client: TestClient, auth_headers: dict, two_clients):
        listed = client.get(
            f"{API}/clients", params={"gst_registered": True}, headers=auth_headers
        ).json()
        assert [row["id"] for row in listed["items"]] == [two_clients["registered"]]

        without = client.get(
            f"{API}/clients", params={"gst_registered": False}, headers=auth_headers
        ).json()
        assert [row["id"] for row in without["items"]] == [two_clients["unregistered"]]

    def test_by_who_owns_the_client(
        self, client: TestClient, auth_headers: dict, registered_firm: dict, two_clients
    ):
        """"My clients" is the filter a practitioner opens the screen with."""
        owner_id = registered_firm["practitioner"]["id"]
        client.patch(
            f"{API}/clients/{two_clients['registered']}",
            json={"assigned_practitioner_id": owner_id},
            headers=auth_headers,
        )

        mine = client.get(
            f"{API}/clients", params={"assigned_to": owner_id}, headers=auth_headers
        ).json()
        assert [row["id"] for row in mine["items"]] == [two_clients["registered"]]
        assert (
            client.get(
                f"{API}/clients",
                params={"assigned_to": str(uuid.uuid4())},
                headers=auth_headers,
            ).json()["total"]
            == 0
        )
