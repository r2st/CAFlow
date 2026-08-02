"""Client CRUD and the compliance items generated from a client's registrations."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.base import ComplianceStatus, EntityType, GSTFilingFrequency
from app.models.client import Client as ClientModel
from app.models.compliance import ComplianceItem, ComplianceType
from app.services.applicability import applies_to
from app.services.compliance_generator import applicable_types, generate_compliance_items
from tests.conftest import make_client_payload

API = "/api/v1"


def codes_generated(db: Session, client_id: str) -> set[str]:
    rows = db.execute(
        select(ComplianceType.code)
        .join(ComplianceItem, ComplianceItem.compliance_type_id == ComplianceType.id)
        .where(ComplianceItem.client_id == uuid.UUID(client_id))
        .distinct()
    ).all()
    return {row[0] for row in rows}


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
            json={"gst_registered": True, "gstin": "27AAACL9876R1Z1"},
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
