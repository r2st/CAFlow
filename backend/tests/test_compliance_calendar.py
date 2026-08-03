"""Compliance calendar API: period listing, status filtering and filing updates."""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
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
