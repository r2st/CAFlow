"""Compliance calendar API: period listing, status filtering and filing updates."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core import clock
from app.database import engine
from app.models.base import ComplianceStatus, TaskPriority, TaskStatus
from app.models.compliance import ComplianceItem
from app.models.task import Task
from app.services import tasks as task_service
from tests.conftest import FIRM_REGISTRATION, make_client_payload

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


def client_of_long_standing(client: TestClient, auth_headers: dict, **overrides) -> str:
    """A client the firm has acted for a year, so its calendar reaches back.

    A client onboarded this morning has no deadline that has already passed —
    generation starts at the onboarding date — and a filing date cannot be in
    the future, so a test about the filed/delayed split has nowhere to put one.
    Anything exercising a lodged return wants a deadline behind it, which is
    also the only shape the question arises in for a real firm.
    """
    return create_client_record(
        client,
        auth_headers,
        onboarded_on=(clock.today() - timedelta(days=365)).isoformat(),
        **overrides,
    )


def lapsed_items(db: Session, client_id: str) -> list[ComplianceItem]:
    """That client's filings whose statutory deadline has already passed."""
    items = [item for item in items_for(db, client_id) if item.due_date < clock.today()]
    assert items, "expected at least one filing whose deadline has passed"
    return items


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
        all_items[0].due_date = clock.today() - timedelta(days=10)
        all_items[1].due_date = clock.today() + timedelta(days=200)
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
        items[0].due_date = clock.today() + timedelta(days=3)
        items[1].due_date = clock.today() + timedelta(days=30)
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
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
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
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
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
        assert body["filed_on"] == clock.today().isoformat()

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

    def test_a_filing_date_is_refused_on_a_status_that_is_not_filed(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The single-item edit takes the same position the bulk one does.

        Bulk refuses this outright: a filing date belongs to a filed item, and
        accepting it elsewhere means recording that a return was lodged on a
        day when it was not. The single-item route used to take it, and the
        stale date it left behind is the one that later decides `filed` against
        `delayed_filed` — a return lodged on time stamped as a late filing, in
        the record the firm would show an assessing officer.
        """
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "pending", "filed_on": clock.today().isoformat()},
        )
        assert response.status_code == 422
        assert "filed_on" in response.json()["detail"]

        after = client.get(
            f"{API}/compliance/items/{item.id}", headers=auth_headers
        ).json()
        assert after["filed_on"] is None
        assert after["status"] == "pending"

    def test_correcting_the_filing_date_alone_re_derives_delayed(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Moving the date past the due date makes it a late filing, and says so.

        The filed/delayed split is derived from the date, so a correction to
        the date has to re-derive it. Nothing re-ran when the status was left
        out of the patch, and the item stayed `filed` while carrying a date
        after its own deadline.
        """
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        on_time = item.due_date - timedelta(days=1)
        late = item.due_date + timedelta(days=3)

        filed = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": on_time.isoformat()},
        ).json()
        assert filed["status"] == "filed"

        corrected = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"filed_on": late.isoformat()},
        ).json()
        assert corrected["status"] == "delayed_filed"
        assert corrected["filed_on"] == late.isoformat()

    def test_correcting_the_filing_date_back_within_the_deadline_clears_delayed(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A correction the other way is the one that matters to the client."""
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        late = item.due_date + timedelta(days=3)
        on_time = item.due_date - timedelta(days=1)

        delayed = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": late.isoformat()},
        ).json()
        assert delayed["status"] == "delayed_filed"

        corrected = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"filed_on": on_time.isoformat()},
        ).json()
        assert corrected["status"] == "filed"

    def test_extending_the_due_date_clears_a_filing_that_is_no_longer_late(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A deadline extension is the other half of the same derivation.

        CBIC and CBDT extend due dates routinely — the seeded calendar carries
        the ordinary dates precisely so a firm can move them. Only ``filed_on``
        re-derived the split, so a return lodged on the 25th against an
        extension to the 30th kept the ``delayed_filed`` it was stamped with
        while the due date still said the 20th: the firm's own record calling a
        filing late that was five days inside the window.
        """
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        statutory = item.due_date
        filed_on = statutory + timedelta(days=5)
        extended = statutory + timedelta(days=10)

        delayed = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": filed_on.isoformat()},
        ).json()
        assert delayed["status"] == "delayed_filed"

        after = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"due_date": extended.isoformat()},
        ).json()
        assert after["due_date"] == extended.isoformat()
        assert after["filed_on"] == filed_on.isoformat()
        assert after["status"] == "filed"

    def test_pulling_the_due_date_in_marks_the_filing_delayed(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The direction that costs the client: an on-time filing turned late.

        A due date corrected *earlier* than the filing date left the item
        reading ``filed`` — the record understating a late filing, which is the
        worse way for it to be wrong.
        """
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        filed_on = item.due_date - timedelta(days=1)
        corrected_due = filed_on - timedelta(days=2)

        filed = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": filed_on.isoformat()},
        ).json()
        assert filed["status"] == "filed"

        after = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"due_date": corrected_due.isoformat()},
        ).json()
        assert after["status"] == "delayed_filed"
        assert after["filed_on"] == filed_on.isoformat()

    def test_a_due_date_change_records_the_status_it_moved(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The re-derived status is the half of the change the trail most needs."""
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        filed_on = item.due_date + timedelta(days=5)

        client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": filed_on.isoformat()},
        )
        client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"due_date": (item.due_date + timedelta(days=10)).isoformat()},
        )

        entries = client.get(
            f"{API}/audit",
            headers=auth_headers,
            params={"action": "compliance_item.update", "entity_id": str(item.id)},
        ).json()["items"]
        latest = entries[0]["changes"]
        assert latest["before"]["status"] == "delayed_filed"
        assert latest["after"]["status"] == "filed"

    def test_a_due_date_change_leaves_an_unfiled_item_alone(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Re-deriving must not invent a filing for work nobody has done."""
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]

        after = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"due_date": (item.due_date + timedelta(days=10)).isoformat()},
        ).json()
        assert after["status"] == "pending"
        assert after["filed_on"] is None

    def test_a_filing_date_in_the_future_is_refused(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A return cannot have been lodged on a day that has not arrived.

        The year is the digit that gets mistyped, so the mistake lands twelve
        months out rather than one day, and nothing downstream reads it as a
        mistake: the item drops off the chase list, the split records it as
        delayed_filed against its own deadline, and the work becomes billable
        that moment — so the client is invoiced for a filing nobody has made.
        """
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        next_year = clock.today() + timedelta(days=365)

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": next_year.isoformat()},
        )
        assert response.status_code == 422
        assert "future" in response.json()["detail"]

        after = client.get(
            f"{API}/compliance/items/{item.id}", headers=auth_headers
        ).json()
        assert after["status"] == "pending"
        assert after["filed_on"] is None

    def test_a_batch_cannot_be_filed_in_the_future_either(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The bulk endpoint is where a mistyped year reaches a hundred rows."""
        client_id = client_of_long_standing(client, auth_headers)
        items = lapsed_items(db, client_id)[:3]

        response = client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={
                "item_ids": [str(i.id) for i in items],
                "status": "filed",
                "filed_on": (clock.today() + timedelta(days=1)).isoformat(),
            },
        )
        assert response.status_code == 422
        assert "future" in response.json()["detail"]

    def test_filing_today_is_still_allowed(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The bound is today in India, not the server's own date.

        A practitioner lodging a return at 01:00 IST on the 20th means the
        20th; measuring this against UTC would refuse them for five and a half
        hours of every working day.
        """
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]

        body = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": clock.today().isoformat()},
        )
        assert body.status_code == 200, body.text
        assert body.json()["filed_on"] == clock.today().isoformat()

    def test_a_filing_date_on_an_item_that_was_never_filed_is_refused(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Without a status in the patch there is still nothing it could mean."""
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"filed_on": clock.today().isoformat()},
        )
        assert response.status_code == 422

    def test_bulk_status_update(self, client: TestClient, auth_headers: dict, db: Session):
        client_id = client_of_long_standing(client, auth_headers)
        items = lapsed_items(db, client_id)[:4]
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

    def test_reverting_a_batch_to_pending_clears_the_filing_dates(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The single-item PATCH already did this; the batch quietly did not."""
        client_id = create_client_record(client, auth_headers)
        items = items_for(db, client_id)[:3]
        ids = [str(i.id) for i in items]

        client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={"item_ids": ids, "status": "filed", "filed_on": "2024-01-15"},
        )
        client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={"item_ids": ids, "status": "pending"},
        )

        for item_id in ids:
            body = client.get(
                f"{API}/compliance/items/{item_id}", headers=auth_headers
            ).json()
            assert body["status"] == "pending"
            assert body["filed_on"] is None

    def test_a_reverted_batch_refiled_today_is_not_stamped_with_the_old_date(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The stale date decided filed-vs-delayed, so this was a false record.

        Mark a batch filed on a date long past its due date, revert it — the
        deadline was mis-keyed, say — and file it for real today, on time. The
        leftover date made every one of them ``delayed_filed``.
        """
        client_id = create_client_record(client, auth_headers)
        item = next(i for i in items_for(db, client_id) if i.due_date > clock.today())
        ids = [str(item.id)]
        long_past = item.due_date - timedelta(days=400)

        for payload in (
            {"item_ids": ids, "status": "filed", "filed_on": long_past.isoformat()},
            {"item_ids": ids, "status": "pending"},
            {"item_ids": ids, "status": "filed"},
        ):
            client.post(
                f"{API}/compliance/items/bulk-status", headers=auth_headers, json=payload
            )

        body = client.get(f"{API}/compliance/items/{item.id}", headers=auth_headers).json()
        assert body["filed_on"] == clock.today().isoformat()
        assert body["status"] == "filed"

    def test_a_batch_filed_with_no_date_is_dated_today(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        item = next(i for i in items_for(db, client_id) if i.due_date > clock.today())
        client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={"item_ids": [str(item.id)], "status": "filed"},
        )
        body = client.get(f"{API}/compliance/items/{item.id}", headers=auth_headers).json()
        assert body["filed_on"] == clock.today().isoformat()

    def test_a_batch_filed_late_is_recorded_as_delayed(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        late = item.due_date + timedelta(days=3)
        client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={"item_ids": [str(item.id)], "status": "filed", "filed_on": late.isoformat()},
        )
        body = client.get(f"{API}/compliance/items/{item.id}", headers=auth_headers).json()
        assert body["status"] == "delayed_filed"

    def test_a_filing_date_on_a_non_filed_batch_is_refused(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The endpoint never stored it; saying so beats discarding it quietly."""
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        response = client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={
                "item_ids": [str(item.id)],
                "status": "in_progress",
                "filed_on": "2024-03-09",
            },
        )
        assert response.status_code == 422
        assert "filed_on" in response.json()["detail"]

    def test_a_delayed_filed_batch_may_carry_a_date(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Both filed statuses are filed statuses, not just the on-time one."""
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        late = item.due_date + timedelta(days=9)
        response = client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={
                "item_ids": [str(item.id)],
                "status": "delayed_filed",
                "filed_on": late.isoformat(),
            },
        )
        assert response.status_code == 200, response.text
        body = client.get(f"{API}/compliance/items/{item.id}", headers=auth_headers).json()
        assert body["filed_on"] == late.isoformat()

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
        items[0].due_date = clock.today() - timedelta(days=5)
        items[1].due_date = clock.today() + timedelta(days=2)
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
            json={"status": "filed", "filed_on": clock.today().isoformat()},
        )
        stats = client.get(f"{API}/compliance/dashboard", headers=auth_headers).json()
        assert stats["filed_this_month"] == 1
        assert stats["unbilled_fee_paise"] == fee

    def test_empty_practice_dashboard(self, client: TestClient, auth_headers: dict):
        stats = client.get(f"{API}/compliance/dashboard", headers=auth_headers).json()
        assert stats["total_clients"] == 0
        assert stats["total_items"] == 0
        assert stats["by_category"] == {}


class TestTheDashboardIsCountedByTheDatabase:
    """Every counter on the landing page is an aggregate.

    They used to be computed by reading the firm's whole compliance history
    into memory and tallying it in Python: a filing is a permanent record and
    the nightly generator adds a year of them per client ahead of time, so the
    set only grows, and it was materialised in full on every visit — by every
    practitioner, all of whom open the app in the same half hour.

    The numbers themselves must not have moved, so the numbers are what these
    pin: against what ``derive_display_status`` says row by row, which is the
    definition the calendar screen beside it is rendered from.
    """

    def _stats(self, client, auth_headers) -> dict:
        response = client.get(f"{API}/compliance/dashboard", headers=auth_headers)
        assert response.status_code == 200, response.text
        return response.json()

    def test_the_buckets_agree_with_the_calendar_row_by_row(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A spread across every display state, tallied both ways."""
        client_id = client_of_long_standing(client, auth_headers)
        items = items_for(db, client_id)
        assert len(items) > 6, "expected a calendar worth spreading across states"
        today = clock.today()
        items[0].due_date = today - timedelta(days=30)  # overdue
        items[1].due_date = today                       # due today, so due_soon
        items[2].due_date = today + timedelta(days=7)   # the edge of due_soon
        items[3].due_date = today + timedelta(days=8)   # one day past it
        items[4].status = ComplianceStatus.NOT_APPLICABLE
        items[5].status = ComplianceStatus.FILED
        items[5].filed_on = today
        db.commit()

        stats = self._stats(client, auth_headers)

        expected = {"overdue": 0, "due_soon": 0, "upcoming": 0}
        by_category: dict[str, int] = {}
        for item in items_for(db, client_id):
            state = item.derive_display_status(today)
            if state not in expected:
                continue
            expected[state] += 1
            category = item.compliance_type.category.value
            by_category[category] = by_category.get(category, 0) + 1

        assert {key: stats[key] for key in expected} == expected
        assert stats["by_category"] == by_category
        assert stats["total_items"] == len(items)

    def test_a_withdrawn_filing_is_counted_nowhere_but_the_total(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Not-applicable is a filing the client does not owe. It stays on the
        record — hence the total — and belongs in no bucket and no category."""
        client_id = create_client_record(client, auth_headers)
        items = items_for(db, client_id)
        for item in items:
            item.status = ComplianceStatus.NOT_APPLICABLE
        db.commit()

        stats = self._stats(client, auth_headers)

        assert stats["total_items"] == len(items)
        assert (stats["overdue"], stats["due_soon"], stats["upcoming"]) == (0, 0, 0)
        assert stats["by_category"] == {}

    def test_a_return_lodged_last_month_is_not_this_months_work(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        item.status = ComplianceStatus.FILED
        item.filed_on = clock.today().replace(day=1) - timedelta(days=1)
        db.commit()

        assert self._stats(client, auth_headers)["filed_this_month"] == 0

    def test_work_already_on_an_invoice_is_not_still_unbilled(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = client_of_long_standing(client, auth_headers)
        first, second = lapsed_items(db, client_id)[:2]
        for item in (first, second):
            item.status = ComplianceStatus.FILED
            item.filed_on = clock.today()
        first.is_billed = True
        db.commit()

        stats = self._stats(client, auth_headers)
        assert stats["filed_this_month"] == 2
        assert stats["unbilled_fee_paise"] == second.fee_paise

    def test_another_firms_filings_are_not_counted(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The aggregates run in SQL now, so the tenancy predicate is the only
        thing keeping one firm's numbers out of another's."""
        create_client_record(client, auth_headers)
        mine = self._stats(client, auth_headers)

        neighbour = client.post(
            f"{API}/auth/register",
            json={
                **FIRM_REGISTRATION,
                "firm_name": "Neighbouring Associates",
                "firm_email": "hello@neighbour.in",
                "owner_email": "owner@neighbour.in",
            },
        )
        assert neighbour.status_code == 201, neighbour.text
        their_headers = {"Authorization": f"Bearer {neighbour.json()['access_token']}"}
        create_client_record(
            client, their_headers, name="Their Client", pan="AABCT9999Z", gstin=None
        )

        assert self._stats(client, auth_headers) == mine

    def test_the_filings_are_counted_rather_than_read(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The point of the change, asserted directly.

        Anything this endpoint asks of ``compliance_items`` must come back as
        an aggregate. A statement selecting the rows themselves is one whose
        cost grows with the firm's whole filing history, which is what put the
        landing page on that curve in the first place.
        """
        create_client_record(client, auth_headers)
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()).lower())

        event.listen(engine, "before_cursor_execute", record)
        try:
            self._stats(client, auth_headers)
        finally:
            event.remove(engine, "before_cursor_execute", record)

        touched = [sql for sql in statements if "compliance_items" in sql]
        assert touched, "the dashboard never looked at the compliance items"
        unaggregated = [sql for sql in touched if "count(" not in sql and "sum(" not in sql]
        assert not unaggregated, f"rows read instead of counted: {unaggregated}"


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


class TestTheNumberThePortalIssues:
    """An acknowledgement number is issued by the statutory portal at the
    moment it accepts a return, so it cannot exist for a return nobody lodged.

    Nothing enforced that. Reverting a filing cleared the date and left the
    number, and something reads it: the filing-confirmation draft quotes it
    back, so a firm could send "your return has been filed successfully,
    acknowledgement number …" for a return sitting on its own chase list.
    Marked filed again later, the item kept that number for good — against a
    period it was never issued for, in the register the firm would show an
    assessing officer.
    """

    ACK = "AA2707260012345"

    def _filed_item(self, client: TestClient, auth_headers: dict, db: Session):
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={
                "status": "filed",
                "filed_on": (item.due_date - timedelta(days=1)).isoformat(),
                "acknowledgement_number": self.ACK,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["acknowledgement_number"] == self.ACK
        return client_id, item

    def test_reverting_a_filing_takes_the_number_with_it(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        _, item = self._filed_item(client, auth_headers, db)

        reverted = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "pending"},
        ).json()

        assert reverted["filed_on"] is None
        assert reverted["acknowledgement_number"] is None

    def test_the_confirmation_draft_stops_quoting_it(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The consumer that turns a stale number into something a client reads."""
        client_id, item = self._filed_item(client, auth_headers, db)
        client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "pending"},
        )

        draft = client.post(
            f"{API}/reminders/draft",
            headers=auth_headers,
            json={
                "client_id": client_id,
                "purpose": "filing_confirmation",
                "compliance_item_id": str(item.id),
            },
        ).json()
        assert self.ACK not in draft["body"]

    def test_ruling_a_filing_out_clears_it_too(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        _, item = self._filed_item(client, auth_headers, db)

        ruled_out = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "not_applicable"},
        ).json()
        assert ruled_out["acknowledgement_number"] is None

    def test_a_correction_to_a_filed_item_keeps_it(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Nothing about the return changed — only which day it was lodged."""
        _, item = self._filed_item(client, auth_headers, db)

        corrected = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"filed_on": (item.due_date - timedelta(days=2)).isoformat()},
        ).json()
        assert corrected["acknowledgement_number"] == self.ACK

    def test_setting_one_on_something_unfiled_is_refused(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"acknowledgement_number": self.ACK},
        )
        assert response.status_code == 422, response.text
        assert "accepts a return" in response.json()["detail"]

    def test_the_change_is_recorded_in_the_trail(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The number is dropped without the caller naming it, so nothing else
        in the record would say it had been there."""
        from app.models.audit import AuditLog

        _, item = self._filed_item(client, auth_headers, db)
        client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "pending"},
        )

        entries = list(
            db.scalars(
                select(AuditLog).where(AuditLog.action == "compliance_item.update")
            ).all()
        )
        dropped = [
            entry
            for entry in entries
            if entry.changes["before"].get("acknowledgement_number") == self.ACK
            and entry.changes["after"].get("acknowledgement_number") is None
        ]
        assert dropped, [entry.changes for entry in entries]

    def test_a_batch_cannot_share_one_number(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The portal issues one per return, and this endpoint carries one."""
        client_id = client_of_long_standing(client, auth_headers)
        item_ids = [str(item.id) for item in lapsed_items(db, client_id)[:3]]
        assert len(item_ids) == 3

        response = client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={
                "item_ids": item_ids,
                "status": "filed",
                "acknowledgement_number": self.ACK,
            },
        )
        assert response.status_code == 422, response.text
        assert "identifies one return" in response.json()["detail"]

    def test_a_batch_of_one_still_may(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """It names exactly the return the number belongs to."""
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]

        response = client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={
                "item_ids": [str(item.id)],
                "status": "filed",
                "acknowledgement_number": self.ACK,
            },
        )
        assert response.status_code == 200, response.text
        db.refresh(item)
        assert item.acknowledgement_number == self.ACK

    def test_reverting_a_batch_clears_every_number(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = client_of_long_standing(client, auth_headers)
        items = lapsed_items(db, client_id)[:3]
        for item in items:
            client.post(
                f"{API}/compliance/items/bulk-status",
                headers=auth_headers,
                json={
                    "item_ids": [str(item.id)],
                    "status": "filed",
                    "acknowledgement_number": f"{self.ACK}{item.period_label}",
                },
            )

        response = client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={"item_ids": [str(i.id) for i in items], "status": "pending"},
        )
        assert response.status_code == 200, response.text
        for item in items:
            db.refresh(item)
            assert item.acknowledgement_number is None
            assert item.filed_on is None


class TestWorkForAFilingRuledOutByHand:
    """Ruling a filing not-applicable takes its task with it.

    Two paths already close a filing and close the work raised for it:
    off-boarding the client, and dropping the registration behind the return.
    Editing the filing directly did neither — and that is the path a
    practitioner actually uses, both as the everyday "this one does not apply
    to them" on the calendar screen and as the end-of-deadline batch on
    ``bulk-status``.

    What was left behind is a task nobody can discharge. It stays TODO on
    somebody's queue, it is counted in the workload view a manager reads to
    decide who is drowning, and it counts down to a statutory deadline against
    a return this firm has decided is not owed — going overdue on the day that
    deadline passes, in red, for good.
    """

    def _generate_tasks(self, client: TestClient, auth_headers: dict) -> int:
        response = client.post(
            f"{API}/tasks/generate", json={"horizon_days": 365}, headers=auth_headers
        )
        assert response.status_code == 200, response.text
        return response.json()["created"]

    def _task_for(self, db: Session, item: ComplianceItem) -> Task:
        task = db.scalars(
            select(Task).where(Task.compliance_item_id == item.id)
        ).first()
        assert task is not None, "no task was raised for that filing"
        return task

    def _set_status(
        self, client: TestClient, auth_headers: dict, item: ComplianceItem, status: str
    ):
        return client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": status},
        )

    def test_the_task_is_withdrawn_with_the_filing(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = items_for(db, client_id)[0]
        task = self._task_for(db, item)
        assert task.status == TaskStatus.TODO

        response = self._set_status(client, auth_headers, item, "not_applicable")
        assert response.status_code == 200, response.text

        db.refresh(task)
        assert task.status == TaskStatus.CANCELLED
        assert task.withdrawn_from_status == TaskStatus.TODO

    def test_putting_the_filing_back_puts_the_work_back(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Reversible on the same terms as everywhere else — task generation
        skips a filing that already carries one, so nothing else would ever
        raise work for it again."""
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = items_for(db, client_id)[0]
        task = self._task_for(db, item)
        client.patch(
            f"{API}/tasks/{task.id}",
            headers=auth_headers,
            json={"status": "in_progress"},
        )

        self._set_status(client, auth_headers, item, "not_applicable")
        db.refresh(task)
        assert task.status == TaskStatus.CANCELLED

        response = self._set_status(client, auth_headers, item, "pending")
        assert response.status_code == 200, response.text
        db.refresh(task)
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.withdrawn_from_status is None

    def test_the_workload_view_stops_counting_it(
        self, client: TestClient, auth_headers: dict, db: Session, firm_id: str
    ):
        """Where the stale task does its damage: this is what a manager reads
        to decide who is free."""
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = items_for(db, client_id)[0]

        def open_tasks() -> int:
            response = client.get(f"{API}/tasks/workload", headers=auth_headers)
            assert response.status_code == 200, response.text
            return response.json()["total_open"]

        before = open_tasks()
        self._set_status(client, auth_headers, item, "not_applicable")
        assert open_tasks() == before - 1

    def test_a_batch_ruled_out_takes_its_whole_queue(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """``bulk-status`` is the end-of-deadline workflow, so it is where a
        year of stale work is created in one press."""
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        items = items_for(db, client_id)[:5]
        tasks = [self._task_for(db, item) for item in items]

        response = client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={"item_ids": [str(i.id) for i in items], "status": "not_applicable"},
        )
        assert response.status_code == 200, response.text

        for task in tasks:
            db.refresh(task)
            assert task.status == TaskStatus.CANCELLED
        assert not db.scalars(
            select(Task).where(
                Task.compliance_item_id.in_([i.id for i in items]),
                Task.status.in_(task_service.OPEN_TASK_STATUSES),
            )
        ).all()

    def test_reverting_the_batch_brings_the_queue_back(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        items = items_for(db, client_id)[:5]
        tasks = [self._task_for(db, item) for item in items]

        for status_value in ("not_applicable", "pending"):
            response = client.post(
                f"{API}/compliance/items/bulk-status",
                headers=auth_headers,
                json={"item_ids": [str(i.id) for i in items], "status": status_value},
            )
            assert response.status_code == 200, response.text

        for task in tasks:
            db.refresh(task)
            assert task.status == TaskStatus.TODO
            assert task.withdrawn_from_status is None

    def test_filing_a_return_leaves_its_task_alone(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Only the obligation going away moves the work. A return being
        lodged is the task being *done*, and whoever holds it says so."""
        client_id = client_of_long_standing(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        task = self._task_for(db, item)

        response = self._set_status(client, auth_headers, item, "filed")
        assert response.status_code == 200, response.text
        db.refresh(task)
        assert task.status == TaskStatus.TODO

    def test_a_status_that_does_not_move_leaves_the_work_untouched(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A task a manager cancelled themselves carries no withdrawal marker,
        so re-saving the filing at the status it already holds must not
        resurrect it."""
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = items_for(db, client_id)[0]
        task = self._task_for(db, item)
        client.patch(
            f"{API}/tasks/{task.id}", headers=auth_headers, json={"status": "cancelled"}
        )

        self._set_status(client, auth_headers, item, "pending")

        db.refresh(task)
        assert task.status == TaskStatus.CANCELLED
        assert task.withdrawn_from_status is None


class TestWorkFollowingADeadlineThatMoves:
    """A task carries its own copy of the deadline, and nothing moved it.

    ``due_date`` is copied onto the task once, when it is raised, and it is
    what the board sorts on, what the workload view counts as overdue, and what
    ``overdue_only`` filters by. The filing's own due date, meanwhile, is a
    field a practitioner is expected to edit — CBIC and CBDT extend deadlines
    routinely, and the seeded calendar carries the ordinary dates precisely so
    a firm can correct them.

    So the two drifted apart, in both directions:

    * an extension left the task counting down to the old date, going overdue
      in red on a day the deadline no longer falls, sorted to the top of
      somebody's board and counted against them in the workload view;
    * a deadline corrected *earlier* is the direction that costs a client. The
      task went on showing weeks of margin against a return now due next week,
      so the one signal the board gives that something needs doing now was the
      signal it withheld.
    """

    def _generate_tasks(self, client: TestClient, auth_headers: dict) -> int:
        response = client.post(
            f"{API}/tasks/generate", json={"horizon_days": 365}, headers=auth_headers
        )
        assert response.status_code == 200, response.text
        return response.json()["created"]

    def _task_for(self, db: Session, item: ComplianceItem) -> Task:
        task = db.scalars(select(Task).where(Task.compliance_item_id == item.id)).first()
        assert task is not None, "no task was raised for that filing"
        return task

    def _distant_item(self, db: Session, client_id: str, days: int = 30) -> ComplianceItem:
        """A filing whose deadline is comfortably ahead — priority ``low``."""
        horizon = clock.today() + timedelta(days=days)
        items = [item for item in items_for(db, client_id) if item.due_date >= horizon]
        assert items, f"no filing falls due more than {days} days out"
        return items[0]

    def _move_deadline(self, client: TestClient, auth_headers: dict, item, to):
        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"due_date": to.isoformat()},
        )
        assert response.status_code == 200, response.text
        return response

    def _overdue_total(self, client: TestClient, auth_headers: dict) -> int:
        rows = client.get(f"{API}/tasks/workload", headers=auth_headers).json()["rows"]
        return sum(row["overdue"] for row in rows)

    # ------------------------------------------------------------ extension --

    def test_an_extension_takes_the_task_with_it(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = client_of_long_standing(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        task = self._task_for(db, item)
        assert task.due_date == item.due_date
        extended = clock.today() + timedelta(days=20)

        self._move_deadline(client, auth_headers, item, extended)

        db.refresh(task)
        assert task.due_date == extended

    def test_the_task_stops_reading_overdue(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """What the board showed: red, on a day the deadline no longer falls."""
        client_id = client_of_long_standing(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        task = self._task_for(db, item)
        assert client.get(f"{API}/tasks/{task.id}", headers=auth_headers).json()["is_overdue"]

        self._move_deadline(client, auth_headers, item, clock.today() + timedelta(days=20))

        after = client.get(f"{API}/tasks/{task.id}", headers=auth_headers).json()
        assert after["is_overdue"] is False
        assert after["days_remaining"] == 20

    def test_the_workload_view_stops_counting_it_overdue(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The manager reading it is deciding who is drowning."""
        client_id = client_of_long_standing(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        before = self._overdue_total(client, auth_headers)

        self._move_deadline(client, auth_headers, item, clock.today() + timedelta(days=20))

        assert self._overdue_total(client, auth_headers) == before - 1

    def test_an_extension_relaxes_a_priority_the_deadline_derived(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = client_of_long_standing(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        task = self._task_for(db, item)
        assert task.priority == TaskPriority.URGENT

        self._move_deadline(client, auth_headers, item, clock.today() + timedelta(days=20))

        db.refresh(task)
        assert task.priority == TaskPriority.LOW

    # ----------------------------------------------------- the other direction --

    def test_a_deadline_pulled_in_moves_the_task_forward(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The direction that costs a client: weeks of margin that is not there."""
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = self._distant_item(db, client_id)
        task = self._task_for(db, item)
        corrected = clock.today() + timedelta(days=2)

        self._move_deadline(client, auth_headers, item, corrected)

        db.refresh(task)
        assert task.due_date == corrected

    def test_a_deadline_pulled_in_raises_the_priority(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The board's one signal that something needs doing now."""
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = self._distant_item(db, client_id)
        task = self._task_for(db, item)
        assert task.priority == TaskPriority.LOW

        self._move_deadline(client, auth_headers, item, clock.today() + timedelta(days=2))

        db.refresh(task)
        assert task.priority == TaskPriority.HIGH

    def test_work_already_under_way_moves_too(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Open is open — a task somebody has started is still owed by the
        deadline, and the deadline is what moved."""
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = self._distant_item(db, client_id)
        task = self._task_for(db, item)
        client.patch(
            f"{API}/tasks/{task.id}", headers=auth_headers, json={"status": "in_progress"}
        )
        corrected = clock.today() + timedelta(days=2)

        self._move_deadline(client, auth_headers, item, corrected)

        db.refresh(task)
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.due_date == corrected

    # ------------------------------------------------ what is left untouched --

    def test_an_internal_target_a_practitioner_set_is_left_alone(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A date somebody chose is their plan for the work, not a copy of the
        deadline — "get this done by the 15th" survives the 20th becoming the
        30th."""
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = self._distant_item(db, client_id)
        task = self._task_for(db, item)
        internal = item.due_date - timedelta(days=5)
        client.patch(
            f"{API}/tasks/{task.id}",
            headers=auth_headers,
            json={"due_date": internal.isoformat()},
        )

        self._move_deadline(client, auth_headers, item, item.due_date + timedelta(days=10))

        db.refresh(task)
        assert task.due_date == internal

    def test_a_priority_a_manager_set_by_hand_survives(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The date is a copy of the deadline; the priority may be a judgement."""
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = self._distant_item(db, client_id)
        task = self._task_for(db, item)
        client.patch(
            f"{API}/tasks/{task.id}", headers=auth_headers, json={"priority": "urgent"}
        )
        corrected = clock.today() + timedelta(days=2)

        self._move_deadline(client, auth_headers, item, corrected)

        db.refresh(task)
        assert task.due_date == corrected
        assert task.priority == TaskPriority.URGENT

    def test_a_task_already_finished_keeps_the_date_it_was_done_against(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = self._distant_item(db, client_id)
        task = self._task_for(db, item)
        was = task.due_date
        client.patch(f"{API}/tasks/{task.id}", headers=auth_headers, json={"status": "done"})

        self._move_deadline(client, auth_headers, item, clock.today() + timedelta(days=2))

        db.refresh(task)
        assert task.status == TaskStatus.DONE
        assert task.due_date == was

    def test_a_task_a_manager_cancelled_is_not_dragged_along(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = self._distant_item(db, client_id)
        task = self._task_for(db, item)
        was = task.due_date
        client.patch(
            f"{API}/tasks/{task.id}", headers=auth_headers, json={"status": "cancelled"}
        )

        self._move_deadline(client, auth_headers, item, clock.today() + timedelta(days=2))

        db.refresh(task)
        assert task.due_date == was

    def test_a_patch_that_leaves_the_deadline_alone_moves_nothing(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = self._distant_item(db, client_id)
        task = self._task_for(db, item)
        was_priority = task.priority

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"due_date": item.due_date.isoformat(), "notes": "Extension expected"},
        )
        assert response.status_code == 200, response.text

        db.refresh(task)
        assert task.due_date == item.due_date
        assert task.priority == was_priority

    # ------------------------------------------------------------- the trail --

    def test_the_move_is_recorded_in_the_trail(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """A task changing date under its owner is a thing the firm should be
        able to account for."""
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = self._distant_item(db, client_id)

        self._move_deadline(client, auth_headers, item, clock.today() + timedelta(days=2))

        entries = client.get(
            f"{API}/audit",
            headers=auth_headers,
            params={"action": "compliance_item.update", "entity_id": str(item.id)},
        ).json()["items"]
        assert "moved 1 task(s) to the new deadline" in entries[0]["summary"]

    def test_a_filing_ruled_out_in_the_same_patch_is_withdrawn_not_moved(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Withdrawal wins: there is no deadline left for the work to follow."""
        client_id = create_client_record(client, auth_headers)
        self._generate_tasks(client, auth_headers)
        item = self._distant_item(db, client_id)
        task = self._task_for(db, item)
        was = task.due_date

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={
                "status": "not_applicable",
                "due_date": (clock.today() + timedelta(days=2)).isoformat(),
            },
        )
        assert response.status_code == 200, response.text

        db.refresh(task)
        assert task.status == TaskStatus.CANCELLED
        assert task.due_date == was


class TestASelectionThatNamesOneFilingTwice:
    """``skipped`` was measured against the raw id list, so a repeat counted
    as a filing the endpoint could not find.

    A selection is built by clicking rows, and the calendar re-reads on every
    filter change — so the same filing arrives twice often enough to matter.
    What came back was "12 updated, 3 skipped" for a batch of fifteen clicks
    on twelve filings, every one of which was written. The number a
    practitioner is meant to act on is the one naming filings the firm cannot
    reach; a phantom skip sends them looking for work that is already done,
    and at the end of a deadline that is the wrong thing to spend an hour on.
    """

    def test_a_repeated_id_is_not_reported_as_skipped(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]

        response = client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={"item_ids": [str(item.id)] * 3, "status": "in_progress"},
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"updated": 1, "skipped": 0}

    def test_the_repeated_filing_is_still_updated(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]

        client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={"item_ids": [str(item.id), str(item.id)], "status": "in_progress"},
        )

        body = client.get(f"{API}/compliance/items/{item.id}", headers=auth_headers).json()
        assert body["status"] == "in_progress"

    def test_an_unreachable_id_is_still_counted_once_beside_a_repeat(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The count still names what the firm cannot reach — which is the
        whole point of reporting it — and a repeated unknown id is one miss,
        not two."""
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        stranger = str(uuid.uuid4())

        response = client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={
                "item_ids": [str(item.id), str(item.id), stranger, stranger],
                "status": "filed",
            },
        )

        assert response.json() == {"updated": 1, "skipped": 1}


class TestTheCalendarIsCountedAndPagedByTheDatabase:
    """``display_status`` is derived rather than stored, and that used to be
    what made ``limit`` buy nothing here: every filing in the caller's window
    was read off disk, hydrated, *and* validated into a response model before
    all but one page of it was discarded.

    The window is the caller's own, so its size is too — a mistyped or
    deliberately wide range is a firm's entire calendar, past and pre-generated.
    The numbers must not have moved; only where they are computed has.
    """

    def _calendar(self, client, auth_headers, query: str = "") -> dict:
        response = client.get(
            f"{API}/compliance/calendar?from_date=2020-01-01&to_date=2035-12-31{query}",
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def _spread(self, db: Session, client_id: str) -> None:
        """Put the client's filings across every display state."""
        items = items_for(db, client_id)
        assert len(items) > 6, "expected a calendar worth spreading across states"
        today = clock.today()
        items[0].due_date = today - timedelta(days=30)  # overdue
        items[1].due_date = today                       # due today, so due_soon
        items[2].due_date = today + timedelta(days=7)   # the edge of due_soon
        items[3].due_date = today + timedelta(days=8)   # one day past it
        items[4].status = ComplianceStatus.NOT_APPLICABLE
        items[5].status = ComplianceStatus.FILED
        items[5].filed_on = today
        db.commit()

    def test_the_counts_agree_with_the_model_row_by_row(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The SQL split and ``derive_display_status`` are one definition."""
        client_id = client_of_long_standing(client, auth_headers)
        self._spread(db, client_id)
        today = clock.today()

        expected = {"overdue": 0, "due_soon": 0, "upcoming": 0, "filed": 0}
        total = 0
        for item in items_for(db, client_id):
            total += 1
            state = item.derive_display_status(today)
            if state in expected:
                expected[state] += 1

        summary = self._calendar(client, auth_headers)["summary"]
        assert summary["total"] == total
        assert {key: summary[key] for key in expected} == expected

    def test_every_display_state_filters_to_exactly_its_own_rows(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = client_of_long_standing(client, auth_headers)
        self._spread(db, client_id)
        today = clock.today()

        for state in ("overdue", "due_soon", "upcoming", "filed", "not_applicable"):
            expected = {
                str(item.id)
                for item in items_for(db, client_id)
                if item.derive_display_status(today) == state
            }
            body = self._calendar(client, auth_headers, f"&display_status={state}&limit=1000")
            assert body["total"] == len(expected), state
            assert {row["id"] for row in body["items"]} == expected, state

    def test_the_buckets_still_add_up_to_the_filtered_total(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = client_of_long_standing(client, auth_headers)
        self._spread(db, client_id)

        body = self._calendar(client, auth_headers, "&display_status=upcoming")
        assert body["total"] > 0
        assert sum(b["total"] for b in body["buckets"]) == body["total"]
        assert sum(b["upcoming"] for b in body["buckets"]) == body["total"]

    def test_paging_walks_the_window_without_repeating_or_dropping_a_row(
        self, client: TestClient, auth_headers: dict
    ):
        """Two filings of one client can share a deadline and a period, so the
        order has to be total or a page boundary between them loses one."""
        client_of_long_standing(client, auth_headers)
        whole = self._calendar(client, auth_headers, "&limit=1000")
        assert whole["total"] > 10

        walked: list[str] = []
        for offset in range(0, whole["total"], 5):
            page = self._calendar(client, auth_headers, f"&limit=5&offset={offset}")
            walked.extend(row["id"] for row in page["items"])

        assert len(walked) == whole["total"]
        assert len(set(walked)) == whole["total"]
        assert walked == [row["id"] for row in whole["items"]]

    def test_only_one_page_of_filings_is_read(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The point of the change, asserted directly.

        The counts come back as aggregates and the rows come back bounded, so
        the cost of this screen follows the page rather than the firm's whole
        filing history.
        """
        client_of_long_standing(client, auth_headers)
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()).lower())

        event.listen(engine, "before_cursor_execute", record)
        try:
            body = self._calendar(client, auth_headers, "&limit=5")
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert body["total"] > 5, "expected more filings than one page holds"
        assert len(body["items"]) == 5
        reading_rows = [
            sql
            for sql in statements
            if "from compliance_items" in sql and "count(" not in sql
        ]
        assert reading_rows, "the calendar never looked at the compliance items"
        assert all("limit" in sql for sql in reading_rows), reading_rows


class TestABatchMovesItsTasksInOneQuery:
    """``bulk-status`` takes up to five hundred ids and is the end-of-deadline
    workflow — a month of GST returns marked filed in one click, on the
    twentieth, by every practice at once.

    The work raised for a filing follows the filing, which is right; asking per
    filing whether it had any was up to five hundred round-trips inside one
    transaction to answer what one ``IN`` clause answers.
    """

    def _withdrawable(self, client: TestClient, auth_headers: dict, db: Session):
        client_id = create_client_record(client, auth_headers)
        items = items_for(db, client_id)[:8]
        response = client.post(
            f"{API}/tasks/generate", headers=auth_headers, json={"horizon_days": 365}
        )
        assert response.status_code == 200, response.text
        return [str(item.id) for item in items]

    def _count_task_reads(self, fn) -> int:
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()).lower())

        event.listen(engine, "before_cursor_execute", record)
        try:
            fn()
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len([sql for sql in statements if sql.startswith("select") and " tasks" in sql])

    def test_withdrawing_a_batch_asks_about_its_tasks_once(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        item_ids = self._withdrawable(client, auth_headers, db)

        def withdraw():
            response = client.post(
                f"{API}/compliance/items/bulk-status",
                headers=auth_headers,
                json={"item_ids": item_ids, "status": "not_applicable"},
            )
            assert response.status_code == 200, response.text

        assert self._count_task_reads(withdraw) <= 1

    def test_the_tasks_still_follow_the_whole_batch(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        item_ids = self._withdrawable(client, auth_headers, db)
        tracked = [
            uuid.UUID(item_id)
            for item_id in item_ids
            if db.scalars(
                select(Task).where(Task.compliance_item_id == uuid.UUID(item_id))
            ).first()
        ]
        assert tracked, "expected the batch to carry tasks"

        client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={"item_ids": item_ids, "status": "not_applicable"},
        )
        db.expire_all()
        assert all(
            task.status == TaskStatus.CANCELLED
            for task in db.scalars(
                select(Task).where(Task.compliance_item_id.in_(tracked))
            ).all()
        )

        client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={"item_ids": item_ids, "status": "pending"},
        )
        db.expire_all()
        assert all(
            task.status == TaskStatus.TODO
            for task in db.scalars(
                select(Task).where(Task.compliance_item_id.in_(tracked))
            ).all()
        )


class TestAnExplicitNullOnAFilingsRequiredFields:
    """A PATCH body is all optionals, and the optionality means two different
    things. ``notes`` is optional because a filing may not have any — ``null``
    clears it. ``due_date`` is optional because a PATCH need not name it; the
    column is ``NOT NULL``, so ``null`` is not an instruction.

    ``exclude_unset`` cannot tell them apart, so the null was written — and
    ``_normalise_filing`` then compared the filing date against a deadline that
    was no longer there, which came back a 500 with nothing naming the field.
    """

    def test_clearing_the_deadline_is_refused_by_name(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "filed", "due_date": None},
        )

        assert response.status_code == 422, response.text
        body = response.json()
        assert body["error"]["fields"][0]["field"] == "due_date"
        assert "null" in body["detail"]

    @pytest.mark.parametrize("field", ["status", "fee_paise"])
    def test_the_other_required_fields_are_refused_too(
        self, client: TestClient, auth_headers: dict, db: Session, field: str
    ):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]

        response = client.patch(
            f"{API}/compliance/items/{item.id}", headers=auth_headers, json={field: None}
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["fields"][0]["field"] == field

    def test_the_genuinely_optional_fields_stay_clearable(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The distinction this draws has to leave the other side working."""
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"notes": "chase the client"},
        )

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"notes": None, "assigned_practitioner_id": None},
        )
        assert response.status_code == 200, response.text
        assert response.json()["notes"] is None

    def test_omitting_a_required_field_still_leaves_it_alone(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]
        was_due = item.due_date

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "in_progress"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["due_date"] == was_due.isoformat()


class TestTwoGenerationsOfOneFirmAtOnce:
    """Materialising a calendar is a read-decide-write, and nothing ordered it.

    Read which (type, period) pairs the client already has, decide these are
    missing, insert one row each. The reasoning was that the database refuses
    the duplicate — ``uq_compliance_item_period`` covers (client, type, period)
    — and it does. That is the problem: the refusal is an ``IntegrityError``,
    not a row quietly skipped, so the loser of the race does not no-op. It
    fails, and takes its whole transaction with it.

    Four doors reach generation: creating a client, taking one back on, saving
    a registration change, and the explicit top-up — against the 02:00 sweep
    that walks every firm on the deployment inside one transaction.
    """

    def _stage_a_competing_run(self, monkeypatch, run) -> list:
        """Run ``run`` on its own session at the instant this one takes the lock.

        Which is where a real loser resumes: the winner has committed by then,
        so everything after it is the ordinary code path deciding what is left.
        """
        from app.services import compliance_generator

        fired: list = []
        real = compliance_generator.firms.lock_firm

        def winner_commits_first(session, firm_id):
            if not fired:
                fired.append(firm_id)
                run()
            return real(session, firm_id)

        monkeypatch.setattr(
            compliance_generator.firms, "lock_firm", winner_commits_first
        )
        return fired

    def test_the_firm_is_held_before_the_generated_set_is_read(
        self, client: TestClient, auth_headers: dict, db: Session, monkeypatch
    ):
        """A lock taken after the decision orders the writes and nothing else.

        Driven through the explicit top-up, which is the one door that takes no
        other lock on the way in — creating a client already holds the firm for
        its plan-limit check, so it cannot show which lock this is about.
        """
        from app.services import compliance_generator

        client_id = create_client_record(client, auth_headers)
        order: list[str] = []
        real_lock = compliance_generator.firms.lock_firm
        real_read = compliance_generator._existing_keys
        monkeypatch.setattr(
            compliance_generator.firms,
            "lock_firm",
            lambda s, f: (order.append("lock"), real_lock(s, f))[1],
        )
        monkeypatch.setattr(
            compliance_generator,
            "_existing_keys",
            lambda s, c: (order.append("read"), real_read(s, c))[1],
        )

        response = client.post(
            f"{API}/clients/{client_id}/compliance-items", headers=auth_headers, json={}
        )

        assert response.status_code == 200, response.text
        assert order == ["lock", "read"]

    def test_a_top_up_that_lost_the_race_still_answers(
        self, client: TestClient, auth_headers: dict, db: Session, monkeypatch
    ):
        """The explicit top-up, with a whole calendar generated underneath it.

        Unordered this was a 409 saying the change "conflicts with an existing
        record" — the wording of a duplicate PAN — on a request that asked for
        a calendar top-up and was right to.
        """
        client_id = create_client_record(client, auth_headers)
        before = len(items_for(db, client_id))

        def competing_top_up():
            client.post(
                f"{API}/clients/{client_id}/compliance-items",
                headers=auth_headers,
                json={
                    "window_start": (clock.today() - timedelta(days=400)).isoformat(),
                    "window_end": (clock.today() + timedelta(days=400)).isoformat(),
                },
            )

        fired = self._stage_a_competing_run(monkeypatch, competing_top_up)

        response = client.post(
            f"{API}/clients/{client_id}/compliance-items",
            headers=auth_headers,
            json={
                "window_start": (clock.today() - timedelta(days=400)).isoformat(),
                "window_end": (clock.today() + timedelta(days=400)).isoformat(),
            },
        )

        assert fired, "the competing run never happened"
        assert response.status_code == 200, response.text
        # The loser creates nothing, because the winner already did — which is
        # what "idempotent per (type, period)" was always meant to mean.
        assert response.json()["created"] == 0
        assert response.json()["skipped_existing"] > 0
        db.expire_all()
        after = items_for(db, client_id)
        assert len(after) > before
        keys = [(item.compliance_type_id, item.period_label) for item in after]
        assert len(keys) == len(set(keys)), "the same filing was materialised twice"

    def test_the_nightly_sweep_survives_a_top_up_running_against_it(
        self, client: TestClient, auth_headers: dict, db: Session, monkeypatch
    ):
        """The sweep is the expensive loser: it holds every firm at once.

        One collision on one client of one firm rolled back the filings
        materialised for every firm ahead of it, and Celery then redelivered
        the task to collide again.
        """
        from app.worker import tasks as worker_tasks

        client_id = create_client_record(client, auth_headers)
        db.expire_all()
        before = len(items_for(db, client_id))

        def competing_top_up():
            response = client.post(
                f"{API}/clients/{client_id}/compliance-items",
                headers=auth_headers,
                json={
                    "window_start": (clock.today() - timedelta(days=400)).isoformat(),
                    "window_end": (clock.today() + timedelta(days=400)).isoformat(),
                },
            )
            assert response.status_code == 200, response.text

        fired = self._stage_a_competing_run(monkeypatch, competing_top_up)

        result = worker_tasks.generate_compliance_items_task()

        assert fired, "the competing run never happened"
        assert result["firms"] == 1
        db.expire_all()
        after = items_for(db, client_id)
        # The top-up's rows are still there: the sweep did not roll them back.
        assert len(after) > before
        keys = [(item.compliance_type_id, item.period_label) for item in after]
        assert len(keys) == len(set(keys)), "the same filing was materialised twice"

    def test_the_sweep_walks_every_firm_and_holds_them_in_a_fixed_order(
        self, client: TestClient, auth_headers: dict, db: Session, monkeypatch
    ):
        """Two unordered sweeps can each hold what the other wants next."""
        from app.services import compliance_generator
        from app.worker import tasks as worker_tasks

        create_client_record(client, auth_headers)
        for suffix in ("two", "three"):
            registration = {
                **FIRM_REGISTRATION,
                "firm_name": f"Firm {suffix}",
                "firm_email": f"office-{suffix}@example.in",
                "owner_email": f"owner-{suffix}@example.in",
                "pan": None,
            }
            other = client.post(f"{API}/auth/register", json=registration)
            assert other.status_code == 201, other.text
            headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
            create_client_record(client, headers, pan=None, gstin=None)

        held: list = []
        real = compliance_generator.firms.lock_firm
        monkeypatch.setattr(
            compliance_generator.firms,
            "lock_firm",
            lambda s, f: (held.append(f), real(s, f))[1],
        )

        result = worker_tasks.generate_compliance_items_task()

        assert result["firms"] == 3
        assert len(set(held)) == 3, "the sweep stopped covering every firm"
        assert held == sorted(held), "an undefined lock order lets two sweeps cross"


class TestClearingTheDateAReturnWasLodgedOn:
    """``filed_on: null`` on a filing that stays filed.

    The field is deliberately clearable — reverting a filing is what clears it —
    so ``not_clearable`` leaves it alone. But clearing it only means anything
    alongside a status that is *not* filed. Sent on an item that stays filed,
    ``_normalise_filing`` read the now-empty field, found nothing to keep, and
    filled it with today: the caller asked for the date to come off and the
    record was re-dated instead, with nothing saying so.

    That is a date of record being rewritten. The filed/delayed split is
    derived from it, so a return lodged inside its window and re-dated to today
    becomes ``delayed_filed`` — the firm's own account of when it was lodged,
    and the one an assessing officer asks about, now saying it was late. There
    is one ``filed_on`` and nothing keeps what it was before.

    And it is not a shape a UI sends on purpose: it is what a client library
    serialising an absent field as ``null`` produces, which is the same road
    ``TestAnExplicitNullOnAFilingsRequiredFields`` covers for the columns that
    cannot hold one at all.
    """

    def _filed_item(
        self, client: TestClient, auth_headers: dict, db: Session
    ) -> tuple[ComplianceItem, str]:
        """A filing lodged on time, so a re-date to today would be visible."""
        client_id = client_of_long_standing(client, auth_headers)
        item = lapsed_items(db, client_id)[0]
        lodged = (item.due_date - timedelta(days=2)).isoformat()
        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": lodged},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "filed"
        return item, lodged

    def test_it_is_refused_by_name_rather_than_re_dated(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        item, _ = self._filed_item(client, auth_headers, db)

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"filed_on": None},
        )

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "filed_on" in detail
        assert "filed" in detail

    def test_the_lodged_date_is_left_exactly_as_it_was(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        item, lodged = self._filed_item(client, auth_headers, db)

        client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"filed_on": None},
        )

        stored = client.get(
            f"{API}/compliance/items/{item.id}", headers=auth_headers
        ).json()
        assert stored["filed_on"] == lodged
        # The half that costs the firm: re-dated to today the return would be
        # past its own deadline and stored as a late filing.
        assert stored["status"] == "filed"

    def test_it_is_refused_alongside_a_status_that_is_also_filed(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Named or inherited, the status this patch *lands on* is what decides."""
        item, _ = self._filed_item(client, auth_headers, db)

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "delayed_filed", "filed_on": None},
        )

        assert response.status_code == 422, response.text
        assert "delayed_filed" in response.json()["detail"]

    def test_reverting_a_filing_still_clears_the_date(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """The reason the field is clearable at all, and the refusal must not
        take it away: a return lodged in error goes back to pending and the
        date goes with it."""
        item, _ = self._filed_item(client, auth_headers, db)

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"status": "pending", "filed_on": None},
        )

        assert response.status_code == 200, response.text
        assert response.json()["filed_on"] is None
        assert response.json()["status"] == "pending"

    def test_a_pending_filing_takes_the_null_without_complaint(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Nothing to rewrite, so nothing to refuse."""
        client_id = create_client_record(client, auth_headers)
        item = items_for(db, client_id)[0]

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"filed_on": None},
        )

        assert response.status_code == 200, response.text
        assert response.json()["filed_on"] is None

    def test_correcting_the_date_is_untouched(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        """Only ``null`` is refused. Naming a different date is the ordinary
        correction and still re-derives the filed/delayed split."""
        item, _ = self._filed_item(client, auth_headers, db)
        late = (item.due_date + timedelta(days=3)).isoformat()

        response = client.patch(
            f"{API}/compliance/items/{item.id}",
            headers=auth_headers,
            json={"filed_on": late},
        )

        assert response.status_code == 200, response.text
        assert response.json()["filed_on"] == late
        assert response.json()["status"] == "delayed_filed"
