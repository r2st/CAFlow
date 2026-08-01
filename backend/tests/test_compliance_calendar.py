"""Compliance calendar API: period listing, status filtering and filing updates."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.compliance import ComplianceItem
from tests.conftest import make_client_payload

API = "/api/v1"


def create_client_record(client: TestClient, auth_headers: dict, **overrides) -> str:
    response = client.post(
        f"{API}/clients", headers=auth_headers, json=make_client_payload(**overrides)
    )
    assert response.status_code == 201, response.text
    return response.json()["client"]["id"]


def items_for(db: Session, client_id: str) -> list[ComplianceItem]:
    return list(
        db.scalars(
            select(ComplianceItem)
            .where(ComplianceItem.client_id == uuid.UUID(client_id))
            .order_by(ComplianceItem.due_date)
        ).all()
    )


class TestComplianceTypes:
    def test_seeded_calendar_is_listed(self, client: TestClient, auth_headers: dict):
        response = client.get(f"{API}/compliance/types", headers=auth_headers)
        assert response.status_code == 200
        codes = {t["code"] for t in response.json()}
        assert {"GSTR1_MONTHLY", "GSTR3B_MONTHLY", "TDS_RETURN_24Q", "ITR_NON_AUDIT", "ROC_AOC4"} <= codes

    def test_the_headline_indian_deadlines_are_configured(
        self, client: TestClient, auth_headers: dict
    ):
        types = {t["code"]: t for t in client.get(f"{API}/compliance/types", headers=auth_headers).json()}
        assert types["GSTR1_MONTHLY"]["due_day"] == 11
        assert types["GSTR3B_MONTHLY"]["due_day"] == 20
        assert types["TDS_RETURN_24Q"]["frequency"] == "quarterly"
        assert types["ITR_NON_AUDIT"]["due_day"] == 31
        assert types["ITR_NON_AUDIT"]["due_month_offset"] == 4  # 31 July
        assert types["ROC_AOC4"]["frequency"] == "annual"

    def test_filter_by_category(self, client: TestClient, auth_headers: dict):
        response = client.get(f"{API}/compliance/types?category=gst", headers=auth_headers)
        assert response.status_code == 200
        assert {t["category"] for t in response.json()} == {"gst"}

    def test_requires_authentication(self, client: TestClient):
        assert client.get(f"{API}/compliance/types").status_code == 401


class TestCalendar:
    def test_returns_items_with_derived_state(self, client: TestClient, auth_headers: dict):
        create_client_record(client, auth_headers)
        response = client.get(
            f"{API}/compliance/calendar?from_date=2026-01-01&to_date=2027-12-31",
            headers=auth_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] > 0
        item = body["items"][0]
        assert item["client_name"] == "Nimbus Textiles Pvt Ltd"
        assert item["compliance_type_code"]
        assert item["display_status"] in {"upcoming", "due_soon", "overdue", "filed"}
        assert isinstance(item["days_remaining"], int)

    def test_buckets_group_by_period(self, client: TestClient, auth_headers: dict):
        create_client_record(client, auth_headers)
        body = client.get(
            f"{API}/compliance/calendar?from_date=2026-01-01&to_date=2027-12-31",
            headers=auth_headers,
        ).json()

        assert len(body["buckets"]) > 1
        assert sum(b["total"] for b in body["buckets"]) == body["total"]
        assert body["summary"]["total"] == body["total"]
        # Buckets come back in period order.
        labels = [b["period_label"] for b in body["buckets"]]
        assert labels == sorted(labels)

    def test_filter_by_exact_period(self, client: TestClient, auth_headers: dict, db: Session):
        client_id = create_client_record(client, auth_headers)
        target = items_for(db, client_id)[0].period_label

        body = client.get(
            f"{API}/compliance/calendar?from_date=2026-01-01&to_date=2027-12-31&period={target}",
            headers=auth_headers,
        ).json()
        assert body["total"] > 0
        assert {i["period_label"] for i in body["items"]} == {target}

    def test_filter_by_category(self, client: TestClient, auth_headers: dict):
        create_client_record(client, auth_headers)
        body = client.get(
            f"{API}/compliance/calendar?from_date=2026-01-01&to_date=2027-12-31&category=gst",
            headers=auth_headers,
        ).json()
        assert body["total"] > 0
        assert {i["category"] for i in body["items"]} == {"gst"}

    def test_filter_by_client(self, client: TestClient, auth_headers: dict):
        first = create_client_record(client, auth_headers)
        create_client_record(
            client,
            auth_headers,
            name="Aurora Foods LLP",
            entity_type="llp",
            pan="AABFA1111K",
            gstin=None,
        )
        body = client.get(
            f"{API}/compliance/calendar?from_date=2026-01-01&to_date=2027-12-31&client_id={first}",
            headers=auth_headers,
        ).json()
        assert {i["client_id"] for i in body["items"]} == {first}

    def test_filter_by_stored_status(self, client: TestClient, auth_headers: dict, db: Session):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "in_progress"},
        )

        body = client.get(
            f"{API}/compliance/calendar"
            f"?from_date=2020-01-01&to_date=2030-12-31&compliance_status=in_progress",
            headers=auth_headers,
        ).json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == str(item.id)

    def test_filter_by_display_status(self, client: TestClient, auth_headers: dict, db: Session):
        client_id = create_client_record(client, auth_headers)
        # Force one item overdue and one comfortably in the future.
        all_items = items_for(db, client_id)
        all_items[0].due_date = date.today() - timedelta(days=10)
        all_items[1].due_date = date.today() + timedelta(days=200)
        db.commit()

        overdue = client.get(
            f"{API}/compliance/calendar"
            f"?from_date=2020-01-01&to_date=2030-12-31&display_status=overdue",
            headers=auth_headers,
        ).json()
        assert overdue["total"] >= 1
        assert {i["display_status"] for i in overdue["items"]} == {"overdue"}

    def test_due_soon_window_is_seven_days(self, client: TestClient, auth_headers: dict, db: Session):
        client_id = create_client_record(client, auth_headers)
        items = items_for(db, client_id)
        items[0].due_date = date.today() + timedelta(days=3)
        items[1].due_date = date.today() + timedelta(days=30)
        db.commit()

        body = client.get(
            f"{API}/compliance/calendar"
            f"?from_date=2020-01-01&to_date=2030-12-31&display_status=due_soon",
            headers=auth_headers,
        ).json()
        returned = {i["id"] for i in body["items"]}
        assert str(items[0].id) in returned
        assert str(items[1].id) not in returned

    def test_invalid_display_status_is_rejected(self, client: TestClient, auth_headers: dict):
        response = client.get(
            f"{API}/compliance/calendar?display_status=nonsense", headers=auth_headers
        )
        assert response.status_code == 422

    def test_inverted_date_range_is_rejected(self, client: TestClient, auth_headers: dict):
        response = client.get(
            f"{API}/compliance/calendar?from_date=2026-12-01&to_date=2026-01-01",
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_pagination_slices_items_but_not_counts(
        self, client: TestClient, auth_headers: dict
    ):
        create_client_record(client, auth_headers)
        body = client.get(
            f"{API}/compliance/calendar?from_date=2026-01-01&to_date=2027-12-31&limit=3",
            headers=auth_headers,
        ).json()
        assert len(body["items"]) == 3
        assert body["total"] > 3
        assert body["summary"]["total"] == body["total"]

    def test_calendar_is_firm_scoped(self, client: TestClient, auth_headers: dict):
        create_client_record(client, auth_headers)
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
        body = client.get(
            f"{API}/compliance/calendar?from_date=2020-01-01&to_date=2030-12-31",
            headers={"Authorization": f"Bearer {other['access_token']}"},
        ).json()
        assert body["total"] == 0


class TestFilingUpdates:
    def test_marking_filed_on_time_sets_filed_status(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        on_time = item.due_date - timedelta(days=1)

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": on_time.isoformat(), "acknowledgement_number": "ACK123"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "filed"
        assert body["display_status"] == "filed"
        assert body["acknowledgement_number"] == "ACK123"

    def test_filing_after_the_due_date_is_recorded_as_delayed(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        late = item.due_date + timedelta(days=5)

        body = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": late.isoformat()},
        ).json()
        assert body["status"] == "delayed_filed"
        assert body["display_status"] == "filed"

    def test_filed_without_a_date_defaults_to_today(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        body = client.patch(
            f"{API}/compliance/items/{item.id}", headers=auth_headers, json={"status": "filed"}
        ).json()
        assert body["filed_on"] == date.today().isoformat()

    def test_reverting_to_pending_clears_the_filing_date(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        client.patch(
            f"{API}/compliance/items/{item.id}", headers=auth_headers, json={"status": "filed"}
        )
        body = client.patch(
            f"{API}/compliance/items/{item.id}", headers=auth_headers, json={"status": "pending"}
        ).json()
        assert body["status"] == "pending"
        assert body["filed_on"] is None

    def test_bulk_status_update(self, client: TestClient, auth_headers: dict, db: Session):
        client_id = create_client_record(client, auth_headers)
        items = items_for(db, client_id)[:4]
        filed_on = min(i.due_date for i in items) - timedelta(days=1)

        response = client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={
                "item_ids": [str(i.id) for i in items],
                "status": "filed",
                "filed_on": filed_on.isoformat(),
            },
        )
        assert response.status_code == 200
        assert response.json() == {"updated": 4, "skipped": 0}

        refreshed = client.get(
            f"{API}/compliance/calendar"
            f"?from_date=2020-01-01&to_date=2030-12-31&display_status=filed",
            headers=auth_headers,
        ).json()
        assert refreshed["total"] == 4

    def test_bulk_update_skips_items_from_other_firms(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        mine = items_for(db, client_id)[0]
        response = client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={
                "item_ids": [str(mine.id), "99999999-9999-9999-9999-999999999999"],
                "status": "filed",
            },
        ).json()
        assert response == {"updated": 1, "skipped": 1}

    def test_cannot_update_another_firms_item(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
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
        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers={"Authorization": f"Bearer {other['access_token']}"},
            json={"status": "filed"},
        )
        assert response.status_code == 404

    def test_assignee_must_be_in_the_firm(self, client: TestClient, auth_headers: dict, db: Session):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"assigned_practitioner_id": "99999999-9999-9999-9999-999999999999"},
        )
        assert response.status_code == 400


class TestDashboard:
    def test_dashboard_counts_reflect_the_practice(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        items = items_for(db, client_id)
        items[0].due_date = date.today() - timedelta(days=5)
        items[1].due_date = date.today() + timedelta(days=2)
        db.commit()

        stats = client.get(f"{API}/compliance/dashboard", headers=auth_headers).json()
        assert stats["total_clients"] == 1
        assert stats["active_clients"] == 1
        assert stats["total_items"] == len(items)
        assert stats["overdue"] >= 1
        assert stats["due_soon"] >= 1
        assert stats["by_category"]
        assert stats["filed_this_month"] == 0

    def test_filing_moves_fees_into_unbilled(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        fee = item.fee_paise

        client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": date.today().isoformat()},
        )
        stats = client.get(f"{API}/compliance/dashboard", headers=auth_headers).json()
        assert stats["filed_this_month"] == 1
        assert stats["unbilled_fee_paise"] == fee

    def test_empty_practice_dashboard(self, client: TestClient, auth_headers: dict):
        stats = client.get(f"{API}/compliance/dashboard", headers=auth_headers).json()
        assert stats["total_clients"] == 0
        assert stats["total_items"] == 0
        assert stats["by_category"] == {}


class TestAuditTrail:
    def test_actions_are_recorded(self, client: TestClient, auth_headers: dict, db: Session):
        from app.models.audit import AuditLog

        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        client.patch(
            f"{API}/compliance/items/{item.id}", headers=auth_headers, json={"status": "filed"}
        )

        actions = {row.action for row in db.scalars(select(AuditLog)).all()}
        assert {"firm.register", "client.create", "compliance_item.update"} <= actions

    def test_status_change_is_captured_in_the_diff(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        from app.models.audit import AuditLog

        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "in_progress"},
        )
        entry = db.scalars(
            select(AuditLog).where(AuditLog.action == "compliance_item.update")
        ).first()
        assert entry.changes["before"]["status"] == "pending"
        assert entry.changes["after"]["status"] == "in_progress"
