"""The audit trail read API.

The trail is written all over the codebase; these tests are about reading it
back — that it is scoped to one firm, restricted to owners and partners, and
filterable down to the history of a single record.
"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.services import audit
from tests.conftest import (
    FIRM_REGISTRATION,
    first_lapsed_item_of_type,
    make_client_payload,
)

API = "/api/v1"


def staff_token(client: TestClient, auth_headers: dict, role: str, email: str) -> str:
    """Add a practitioner in the given role and sign in as them."""
    response = client.post(
        f"{API}/auth/practitioners",
        headers=auth_headers,
        json={
            "full_name": f"{role.title()} Person",
            "email": email,
            "password": "another-good-password",
            "role": role,
        },
    )
    assert response.status_code == 201, response.text
    login = client.post(
        f"{API}/auth/login", json={"email": email, "password": "another-good-password"}
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def headers_for(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestAuditTrailIsWritten:
    def test_creating_a_client_leaves_a_trail(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        response = client.get(f"{API}/audit", headers=auth_headers)
        assert response.status_code == 200, response.text

        entries = response.json()["items"]
        assert any(
            entry["entity_type"] == "client" and entry["entity_id"] == created_client["id"]
            for entry in entries
        )

    def test_the_entry_names_who_did_it(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        entries = client.get(f"{API}/audit", headers=auth_headers).json()["items"]
        entry = next(e for e in entries if e["entity_id"] == created_client["id"])

        assert FIRM_REGISTRATION["owner_email"] in entry["actor_label"]
        assert entry["actor_practitioner_id"] is not None
        assert entry["summary"]

    def test_an_update_records_only_the_fields_that_changed(
        self, client: TestClient, auth_headers: dict, client_id: str
    ):
        client.patch(
            f"{API}/clients/{client_id}",
            headers=auth_headers,
            json={"contact_person": "Priya Nair"},
        )

        response = client.get(
            f"{API}/audit",
            headers=auth_headers,
            params={"entity_type": "client", "entity_id": client_id},
        )
        changed = [e for e in response.json()["items"] if e["changes"]]
        assert changed, "an update should record a before/after diff"

        diff = changed[0]["changes"]
        assert diff["after"]["contact_person"] == "Priya Nair"
        # Untouched fields have no business in the diff.
        assert "name" not in diff["after"]


class TestAuditTrailIsReadOnly:
    @pytest.mark.parametrize("method", ["post", "patch", "put", "delete"])
    def test_the_trail_cannot_be_written_through_the_api(
        self, client: TestClient, auth_headers: dict, method: str
    ):
        # request() rather than the per-verb helpers: delete() takes no body.
        response = client.request(method.upper(), f"{API}/audit", headers=auth_headers)
        assert response.status_code == 405


class TestAuditTrailAccess:
    def test_a_junior_cannot_read_the_trail(self, client: TestClient, auth_headers: dict):
        token = staff_token(client, auth_headers, "junior", "junior@sharma-ca.in")

        response = client.get(f"{API}/audit", headers=headers_for(token))

        assert response.status_code == 403

    def test_a_manager_cannot_read_the_trail(self, client: TestClient, auth_headers: dict):
        # The log records what the manager did too, so it is not theirs to read.
        token = staff_token(client, auth_headers, "manager", "manager@sharma-ca.in")

        response = client.get(f"{API}/audit", headers=headers_for(token))

        assert response.status_code == 403

    def test_a_partner_can_read_the_trail(self, client: TestClient, auth_headers: dict):
        token = staff_token(client, auth_headers, "partner", "partner@sharma-ca.in")

        response = client.get(f"{API}/audit", headers=headers_for(token))

        assert response.status_code == 200

    def test_the_trail_needs_a_credential(self, client: TestClient):
        assert client.get(f"{API}/audit").status_code == 401

    def test_one_firm_never_sees_another_firms_trail(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        other = client.post(
            f"{API}/auth/register",
            json={
                **FIRM_REGISTRATION,
                "firm_name": "Iyer & Co",
                "icai_registration_number": "998877S",
                "firm_email": "office@iyer-ca.in",
                "pan": "AAACI9999F",
                "owner_email": "raj@iyer-ca.in",
                "owner_membership_number": "654321",
            },
        )
        assert other.status_code == 201, other.text
        other_headers = headers_for(other.json()["access_token"])

        entries = client.get(f"{API}/audit", headers=other_headers).json()["items"]

        assert all(entry["entity_id"] != created_client["id"] for entry in entries)


class TestAuditFilters:
    def test_filters_by_entity_type(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        response = client.get(
            f"{API}/audit", headers=auth_headers, params={"entity_type": "client"}
        )

        assert response.status_code == 200
        assert response.json()["items"]
        assert {e["entity_type"] for e in response.json()["items"]} == {"client"}

    def test_entity_type_and_id_give_one_records_history(
        self, client: TestClient, auth_headers: dict, client_id: str
    ):
        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"notes": "Chase GST"}
        )
        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"notes": "Chased"}
        )

        response = client.get(
            f"{API}/audit",
            headers=auth_headers,
            params={"entity_type": "client", "entity_id": client_id},
        )

        items = response.json()["items"]
        assert len(items) >= 3  # created, then two updates
        assert all(e["entity_id"] == client_id for e in items)

    def test_filters_by_action(self, client: TestClient, auth_headers: dict, created_client: dict):
        actions = client.get(f"{API}/audit/actions", headers=auth_headers).json()["actions"]
        assert actions
        first = actions[0]["action"]

        response = client.get(f"{API}/audit", headers=auth_headers, params={"action": first})

        assert {e["action"] for e in response.json()["items"]} == {first}

    def test_filters_by_actor(
        self, client: TestClient, auth_headers: dict, created_client: dict, registered_firm: dict
    ):
        owner_id = registered_firm["practitioner"]["id"]

        response = client.get(
            f"{API}/audit", headers=auth_headers, params={"actor_practitioner_id": owner_id}
        )

        assert response.json()["items"]
        assert {e["actor_practitioner_id"] for e in response.json()["items"]} == {owner_id}

    def test_searches_the_summary(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        response = client.get(
            f"{API}/audit", headers=auth_headers, params={"search": "Nimbus"}
        )

        assert response.status_code == 200
        assert response.json()["items"]
        assert all("Nimbus" in (e["summary"] or "") for e in response.json()["items"])

    def test_an_unknown_action_returns_an_empty_page_not_an_error(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        response = client.get(
            f"{API}/audit", headers=auth_headers, params={"action": "nothing.happened"}
        )

        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}

    def test_a_backwards_date_range_is_rejected(self, client: TestClient, auth_headers: dict):
        response = client.get(
            f"{API}/audit",
            headers=auth_headers,
            params={"from_date": "2026-08-01", "to_date": "2026-07-01"},
        )

        assert response.status_code == 422
        assert "to_date" in response.json()["detail"]


def _write_entry(db: Session, firm_id: str, moment: datetime, action: str = "test.event") -> None:
    import uuid as _uuid

    db.add(
        AuditLog(
            firm_id=_uuid.UUID(firm_id),
            action=action,
            entity_type="probe",
            summary=f"probe at {moment.isoformat()}",
            changes={},
            created_at=moment,
        )
    )
    db.commit()


class TestAuditDateWindow:
    """``to_date`` is inclusive: entries later that day must not fall out."""

    @pytest.fixture
    def dated_entries(self, db: Session, firm_id: str) -> None:
        for moment in (
            datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
            # 23:59 IST on the 15th — the last minute of the Indian day.
            datetime(2026, 7, 15, 18, 29, tzinfo=UTC),
            datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        ):
            _write_entry(db, firm_id, moment)

    def test_includes_entries_late_on_the_final_day(
        self, client: TestClient, auth_headers: dict, dated_entries: None
    ):
        response = client.get(
            f"{API}/audit",
            headers=auth_headers,
            params={"action": "test.event", "from_date": "2026-07-01", "to_date": "2026-07-15"},
        )

        # 23:59 on the 15th is within "up to and including the 15th".
        assert response.json()["total"] == 2

    def test_excludes_entries_outside_the_window(
        self, client: TestClient, auth_headers: dict, dated_entries: None
    ):
        response = client.get(
            f"{API}/audit",
            headers=auth_headers,
            params={"action": "test.event", "from_date": "2026-07-20"},
        )

        assert response.json()["total"] == 1


class TestAuditDaysAreIndianDays:
    """The day a filter names is a working day in India, not one in UTC.

    Entries are stored as UTC instants — right for an instant — but the
    practitioner typing a date here is picking a day in the practice's own
    calendar, and UTC is five and a half hours behind it. Bounding the window
    in UTC filed the first five and a half hours of every Indian day under the
    previous date, which is deadline-night work: exactly the entries someone
    goes looking for.
    """

    def test_an_entry_from_the_small_hours_belongs_to_the_indian_day(
        self, client: TestClient, auth_headers: dict, db: Session, firm_id: str
    ):
        # 02:00 IST on 6 August — still 5 August in UTC.
        _write_entry(db, firm_id, datetime(2026, 8, 5, 20, 30, tzinfo=UTC), "probe.late")

        def total(day: str) -> int:
            return client.get(
                f"{API}/audit",
                headers=auth_headers,
                params={"action": "probe.late", "from_date": day, "to_date": day},
            ).json()["total"]

        assert total("2026-08-06") == 1
        assert total("2026-08-05") == 0

    def test_an_entry_late_in_the_indian_evening_stays_on_that_day(
        self, client: TestClient, auth_headers: dict, db: Session, firm_id: str
    ):
        """The other edge: 23:30 IST must not spill into the following day."""
        _write_entry(db, firm_id, datetime(2026, 8, 5, 18, 0, tzinfo=UTC), "probe.evening")

        def total(day: str) -> int:
            return client.get(
                f"{API}/audit",
                headers=auth_headers,
                params={"action": "probe.evening", "from_date": day, "to_date": day},
            ).json()["total"]

        assert total("2026-08-05") == 1
        assert total("2026-08-06") == 0


class TestAuditOrderingAndPaging:
    def test_newest_first(self, client: TestClient, auth_headers: dict, client_id: str):
        client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"notes": "Latest"}
        )

        items = client.get(f"{API}/audit", headers=auth_headers).json()["items"]

        timestamps = [e["created_at"] for e in items]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_paging_never_repeats_or_drops_an_entry(
        self, client: TestClient, auth_headers: dict, db: Session, firm_id: str
    ):
        import uuid as _uuid

        # All written at the same instant: without a tie-break in the sort, a
        # paged read of these can return the same row twice and miss another.
        moment = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
        for index in range(10):
            db.add(
                AuditLog(
                    firm_id=_uuid.UUID(firm_id),
                    action="test.tie",
                    entity_type="probe",
                    summary=f"entry {index}",
                    changes={},
                    created_at=moment,
                )
            )
        db.commit()

        seen = []
        for offset in (0, 4, 8):
            page = client.get(
                f"{API}/audit",
                headers=auth_headers,
                params={"action": "test.tie", "limit": 4, "offset": offset},
            ).json()
            seen.extend(entry["id"] for entry in page["items"])

        assert len(seen) == 10
        assert len(set(seen)) == 10

    def test_reports_the_total_beyond_the_page(
        self, client: TestClient, auth_headers: dict, db: Session, firm_id: str
    ):
        import uuid as _uuid

        base = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
        for index in range(5):
            db.add(
                AuditLog(
                    firm_id=_uuid.UUID(firm_id),
                    action="test.count",
                    entity_type="probe",
                    changes={},
                    created_at=base + timedelta(minutes=index),
                )
            )
        db.commit()

        page = client.get(
            f"{API}/audit", headers=auth_headers, params={"action": "test.count", "limit": 2}
        ).json()

        assert len(page["items"]) == 2
        assert page["total"] == 5


class TestAuditActions:
    def test_lists_only_actions_with_entries_behind_them(
        self, client: TestClient, auth_headers: dict, created_client: dict
    ):
        response = client.get(f"{API}/audit/actions", headers=auth_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["actions"]
        assert all(entry["count"] > 0 for entry in body["actions"])
        assert "client" in body["entity_types"]

    def test_counts_are_per_firm(self, client: TestClient, auth_headers: dict, client_id: str):
        client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                name="Second Client Pvt Ltd", pan="AABCS7777P", gstin="27AABCS7777P1Z5"
            ),
        )

        actions = client.get(f"{API}/audit/actions", headers=auth_headers).json()["actions"]
        create = next(a for a in actions if a["action"].endswith("create"))

        assert create["count"] >= 2

    def test_actions_are_admin_only(self, client: TestClient, auth_headers: dict):
        token = staff_token(client, auth_headers, "junior", "junior2@sharma-ca.in")

        response = client.get(f"{API}/audit/actions", headers=headers_for(token))

        assert response.status_code == 403


class TestTheTrailRecordsWhatHappened:
    """The diff is what the row became, not what the request asked for.

    Several handlers derive a field after applying the patch. Diffing the
    payload against the pre-state recorded the derivation as the value the
    caller sent — or, when the caller never named the field, not at all.
    """

    def _entries(self, client: TestClient, auth_headers: dict, entity_id: str) -> list[dict]:
        response = client.get(
            f"{API}/audit", headers=auth_headers, params={"entity_id": entity_id}
        )
        assert response.status_code == 200, response.text
        return [e for e in response.json()["items"] if e["changes"]]

    def test_a_late_filing_is_recorded_as_delayed_not_as_filed(
        self, client: TestClient, auth_headers: dict, long_standing_client_id: str
    ):
        item = first_lapsed_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        late = date.fromisoformat(item["due_date"]) + timedelta(days=5)

        response = client.patch(
            f"{API}/compliance/items/{item['id']}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": late.isoformat()},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "delayed_filed"

        diff = self._entries(client, auth_headers, item["id"])[0]["changes"]
        # "filed" is a status this row never held.
        assert diff["after"]["status"] == "delayed_filed"
        assert diff["after"]["filed_on"] == late.isoformat()

    def test_correcting_only_the_date_records_the_reclassification(
        self, client: TestClient, auth_headers: dict, long_standing_client_id: str
    ):
        item = first_lapsed_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        due = date.fromisoformat(item["due_date"])

        on_time = client.patch(
            f"{API}/compliance/items/{item['id']}",
            headers=auth_headers,
            json={"status": "filed", "filed_on": (due - timedelta(days=1)).isoformat()},
        )
        assert on_time.json()["status"] == "filed"

        # Only the date moves; the status follows it across the deadline.
        late = client.patch(
            f"{API}/compliance/items/{item['id']}",
            headers=auth_headers,
            json={"filed_on": (due + timedelta(days=3)).isoformat()},
        )
        assert late.status_code == 200, late.text
        assert late.json()["status"] == "delayed_filed"

        latest = self._entries(client, auth_headers, item["id"])[0]["changes"]
        assert latest["before"]["status"] == "filed"
        assert latest["after"]["status"] == "delayed_filed"

    def test_a_bulk_revert_does_not_claim_a_status_the_items_do_not_have(
        self, client: TestClient, auth_headers: dict, long_standing_client_id: str
    ):
        item = first_lapsed_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        late = date.fromisoformat(item["due_date"]) + timedelta(days=2)

        response = client.post(
            f"{API}/compliance/items/bulk-status",
            headers=auth_headers,
            json={
                "item_ids": [item["id"]],
                "status": "filed",
                "filed_on": late.isoformat(),
            },
        )
        assert response.status_code == 200, response.text

        entries = client.get(
            f"{API}/audit", headers=auth_headers, params={"action": "compliance_item.bulk_status"}
        ).json()["items"]
        assert entries[0]["changes"]["status"] == "filed"
        assert entries[0]["changes"]["resulting_statuses"] == ["delayed_filed"]

    def test_finishing_a_task_records_when_it_was_finished(
        self, client: TestClient, auth_headers: dict, client_id: str
    ):
        created = client.post(
            f"{API}/tasks",
            headers=auth_headers,
            json={"title": "Collect bank statements", "client_id": client_id},
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]

        done = client.patch(
            f"{API}/tasks/{task_id}", headers=auth_headers, json={"status": "done"}
        )
        assert done.status_code == 200, done.text

        diff = self._entries(client, auth_headers, task_id)[0]["changes"]
        assert diff["before"]["completed_at"] is None
        # The stamp is the record of when the work was finished. Compared as
        # instants: SQLite hands the column back naive, so the trail carries an
        # offset the response does not.
        recorded = datetime.fromisoformat(diff["after"]["completed_at"])
        returned = datetime.fromisoformat(done.json()["completed_at"])
        assert recorded.replace(tzinfo=None) == returned.replace(tzinfo=None)

    def test_reopening_a_task_records_the_stamp_being_cleared(
        self, client: TestClient, auth_headers: dict, client_id: str
    ):
        task_id = client.post(
            f"{API}/tasks",
            headers=auth_headers,
            json={"title": "File GSTR-3B", "client_id": client_id},
        ).json()["id"]
        client.patch(f"{API}/tasks/{task_id}", headers=auth_headers, json={"status": "done"})

        reopened = client.patch(
            f"{API}/tasks/{task_id}", headers=auth_headers, json={"status": "in_progress"}
        )
        assert reopened.status_code == 200, reopened.text

        diff = self._entries(client, auth_headers, task_id)[0]["changes"]
        assert diff["before"]["completed_at"] is not None
        assert diff["after"]["completed_at"] is None

    def test_categorising_by_hand_records_the_confidence_being_dropped(
        self, client: TestClient, auth_headers: dict, client_id: str
    ):
        uploaded = client.post(
            f"{API}/documents/upload",
            files={
                "file": ("statement.pdf", io.BytesIO(b"%PDF-1.4\n% a statement\n"), "application/pdf")
            },
            data={"client_id": client_id},
            headers=auth_headers,
        )
        assert uploaded.status_code == 201, uploaded.text
        document_id = uploaded.json()["document"]["id"]

        corrected = client.patch(
            f"{API}/documents/{document_id}", headers=auth_headers, json={"category": "form_16"}
        )
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["is_category_confirmed"] is True

        diff = self._entries(client, auth_headers, document_id)[0]["changes"]
        assert diff["after"]["category"] == "form_16"
        # A human choosing the category is what makes the guess meaningless,
        # and the trail is where "a human chose it" is recorded.
        assert diff["after"]["is_category_confirmed"] is True


class TestWhereASignInCameFrom:
    """``ip_address`` is the one field on the one table that answers it.

    It was ``request.client.host`` — the machine that opened the TCP
    connection. In every deployment of this system that machine is Caddy, so
    the field held the reverse proxy's address: the same value on every row,
    for every firm, for every registration and every sign-in. The trail is what
    a firm reads once a credential is suspected of having leaked, and it was
    answering with a constant.

    ``client_ip`` is the resolution the access log and the rate limiter already
    use — the careful one, which believes a forwarded chain only from a trusted
    peer and walks it right-to-left so a forged prefix is skipped. The three
    places that name a caller now agree about who it was.
    """

    @pytest.fixture
    def behind_a_proxy(self, monkeypatch):
        """The production shape: something trusted terminates the connection."""
        from app.config import settings

        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        monkeypatch.setattr(settings, "trusted_proxy_ips", "*")

    def _entry(self, db: Session, action: str) -> AuditLog:
        entries = db.scalars(
            select(AuditLog).where(AuditLog.action == action)
        ).all()
        assert len(entries) == 1, f"expected one {action} entry, got {len(entries)}"
        return entries[0]

    def test_registration_records_the_caller_not_the_proxy(
        self, client: TestClient, db: Session, behind_a_proxy
    ):
        response = client.post(
            f"{API}/auth/register",
            json=FIRM_REGISTRATION,
            headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
        )
        assert response.status_code == 201, response.text

        assert self._entry(db, "firm.register").ip_address == "203.0.113.7"

    def test_signing_in_records_the_caller_not_the_proxy(
        self, client: TestClient, db: Session, registered_firm: dict, behind_a_proxy
    ):
        response = client.post(
            f"{API}/auth/login",
            json={
                "email": FIRM_REGISTRATION["owner_email"],
                "password": FIRM_REGISTRATION["owner_password"],
            },
            headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
        )
        assert response.status_code == 200, response.text

        assert self._entry(db, "auth.login").ip_address == "203.0.113.7"

    def test_an_unproxied_deployment_still_records_the_peer(
        self, client: TestClient, db: Session, registered_firm: dict
    ):
        """With the switch off the header is not believed, which is the
        conservative direction — a caller gets its own address, not one it
        chose."""
        response = client.post(
            f"{API}/auth/login",
            json={
                "email": FIRM_REGISTRATION["owner_email"],
                "password": FIRM_REGISTRATION["owner_password"],
            },
            headers={"X-Forwarded-For": "203.0.113.7"},
        )
        assert response.status_code == 200, response.text

        assert self._entry(db, "auth.login").ip_address != "203.0.113.7"


class TestTheUserAgentARequestArrivesWith:
    """``audit_log.user_agent`` is a VARCHAR(512) fed straight from the header.

    Nothing bounded it, on the two endpoints that record it — registration and
    sign-in, both reachable without credentials. SQLite truncates silently;
    PostgreSQL, which is what the deployment runs, refuses the row, so a caller
    sending a kilobyte of User-Agent could not register *and could not sign
    in*. Truncated rather than refused: the sign-in is genuine either way.
    """

    def _login(self, client: TestClient, agent: str):
        return client.post(
            f"{API}/auth/login",
            json={
                "email": FIRM_REGISTRATION["owner_email"],
                "password": FIRM_REGISTRATION["owner_password"],
            },
            headers={"User-Agent": agent},
        )

    def _entry(self, db: Session) -> AuditLog:
        return db.scalars(
            select(AuditLog).where(AuditLog.action == "auth.login")
        ).one()

    def test_an_over_long_agent_is_cut_to_what_the_column_holds(
        self, client: TestClient, db: Session, registered_firm: dict
    ):
        response = self._login(client, "Mozilla/5.0 " + "A" * 4000)

        assert response.status_code == 200, response.text
        recorded = self._entry(db).user_agent
        assert len(recorded) == audit.MAX_USER_AGENT
        # The head is kept: a user agent identifies itself at the front.
        assert recorded.startswith("Mozilla/5.0 ")

    def test_an_ordinary_agent_is_recorded_whole(
        self, client: TestClient, db: Session, registered_firm: dict
    ):
        agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) CAFlow/1.0"

        assert self._login(client, agent).status_code == 200
        assert self._entry(db).user_agent == agent

    def test_control_characters_do_not_reach_the_trail(self):
        """It is rendered back into the audit screen and into the log, and
        every other string reaching the database is sanitised on the way in.

        Asserted against the resolver rather than over HTTP: these are bytes an
        HTTP client will not put in a header for you, which is exactly why one
        arriving from something that is not a browser is worth stripping.
        """
        from tests.test_middleware import fake_request

        request = fake_request({"user-agent": "CAFlow/1.0\x00\x07​ spoof"})

        assert audit.request_origin(request)[1] == "CAFlow/1.0 spoof"

    def test_a_request_with_no_agent_records_nothing_rather_than_a_blank(
        self, client: TestClient, db: Session, registered_firm: dict
    ):
        response = client.request(
            "POST",
            f"{API}/auth/login",
            json={
                "email": FIRM_REGISTRATION["owner_email"],
                "password": FIRM_REGISTRATION["owner_password"],
            },
            headers={"User-Agent": ""},
        )
        assert response.status_code == 200, response.text
        assert self._entry(db).user_agent is None


class TestTheLabelNamingWhoActed:
    """``audit_log.actor_label`` is a VARCHAR(255) built from two caller strings.

    For a practitioner it is ``"<full name> <email>"``. The schema allows 255
    characters of name and an ``EmailStr`` allows 254 of address, so the format
    string could produce over 500 characters for a 255-character column.
    SQLite truncates silently; PostgreSQL, which is what the deployment runs,
    refuses the row — and ``record`` is called inside the transaction of the
    action it describes, so that refusal takes the *action* down with it. Every
    mutating endpoint in the API writes here, so the practitioner concerned
    could not file, invoice, upload or add a client at all.

    Cut rather than refused, and cut from the head: the address is the half
    that identifies the account and the half a reader searches on.
    """

    def _long_name_headers(self, client: TestClient, auth_headers: dict) -> dict:
        """Add a practitioner whose name and address fill both columns."""
        response = client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "Vishwanathan " * 19 + "Rao",  # 250 characters
                "email": "v" * 60 + "@" + "d" * 60 + ".example.in",
                "password": "another-good-password",
                "role": "manager",
            },
        )
        assert response.status_code == 201, response.text
        token = client.post(
            f"{API}/auth/login",
            json={
                "email": response.json()["email"],
                "password": "another-good-password",
            },
        ).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def test_a_practitioner_label_is_cut_to_what_the_column_holds(
        self, client: TestClient, db: Session, auth_headers: dict
    ):
        headers = self._long_name_headers(client, auth_headers)

        response = client.post(
            f"{API}/clients", json=make_client_payload(), headers=headers
        )

        assert response.status_code == 201, response.text
        entry = db.scalars(
            select(AuditLog).where(AuditLog.action == "client.create")
        ).one()
        assert len(entry.actor_label) <= audit.MAX_ACTOR_LABEL

    def test_the_address_survives_the_cut_rather_than_the_name(
        self, client: TestClient, db: Session, auth_headers: dict
    ):
        """The name is what gives. An entry naming nobody findable is no entry."""
        headers = self._long_name_headers(client, auth_headers)
        address = client.get(f"{API}/auth/me", headers=headers).json()["email"]

        client.post(f"{API}/clients", json=make_client_payload(), headers=headers)

        entry = db.scalars(
            select(AuditLog).where(AuditLog.action == "client.create")
        ).one()
        assert entry.actor_label.endswith(f"<{address}>")

    def test_an_ordinary_practitioner_is_named_in_full(
        self, client: TestClient, db: Session, auth_headers: dict, created_client: dict
    ):
        entry = db.scalars(
            select(AuditLog).where(AuditLog.action == "client.create")
        ).one()

        assert entry.actor_label == (
            f"{FIRM_REGISTRATION['owner_full_name']} "
            f"<{FIRM_REGISTRATION['owner_email']}>"
        )

    def test_a_label_a_caller_composed_is_cut_on_the_same_terms(self):
        """Nothing reaches the column without going through the bound —
        otherwise a second call site can reintroduce the problem by formatting
        its own string, which is how the portal label got there."""
        assert len(audit.bounded_label("N" * 400)) == audit.MAX_ACTOR_LABEL

    def test_a_trailing_marker_is_kept_when_the_name_is_too_long(self):
        """The portal's label is ``"<client name> (client portal)"``, and the
        marker is the part that says this was the client rather than the firm."""
        label = audit.bounded_label("Kumar Enterprises " * 30, " (client portal)")

        assert len(label) == audit.MAX_ACTOR_LABEL
        assert label.endswith(" (client portal)")

    def test_a_suffix_longer_than_the_column_keeps_its_own_tail(self):
        """Nothing left to trim around it, so the end of the address wins."""
        label = audit.bounded_label("Anita", " <" + "a" * 400 + "@example.in>")

        assert len(label) == audit.MAX_ACTOR_LABEL
        assert label.endswith("@example.in>")
