"""Per-firm decisions: the lock that orders them, and the plan slots they hand out.

A plan limit is a count followed by a write, and between those two halves sits
every other request the firm has in flight. The lock is what makes the pair one
step; ``claim_*_slot`` is the only place that pair is written down, so that
every caller that adds to the count goes through the same guard — including the
one that adds to it by switching a deactivated row back on.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.base import FirmPlan
from app.models.client import Client as ClientModel
from app.models.firm import Firm
from app.services import firms
from tests.conftest import gstin_for, make_client_payload

API = "/api/v1"


@pytest.fixture
def one_client_plan(monkeypatch):
    """Pin the client ceiling to one, so a test need not create two hundred.

    Patched on :class:`Firm` rather than on an instance, because the guard
    re-reads the firm through the request's own session.
    """
    monkeypatch.setattr(Firm, "client_limit", property(lambda self: 1))


@pytest.fixture
def two_client_plan(monkeypatch):
    """The same, with room for one deactivation to be raced for."""
    monkeypatch.setattr(Firm, "client_limit", property(lambda self: 2))


class Recorder:
    """A stand-in session that keeps the statements handed to it."""

    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)


class TestTheLockIsOneTheDatabaseCanHonour:
    """Compiled for PostgreSQL, because SQLite has no row locks to assert on.

    SQLite silently drops the clause, so running the suite proves nothing about
    the statement unless the statement itself is inspected.
    """

    def test_locking_a_firm_asks_the_database_to_hold_that_row(self, firm_id):
        recorder = Recorder()

        firms.lock_firm(recorder, uuid.UUID(firm_id))

        sql = str(recorder.statements[0].compile(dialect=postgresql.dialect()))
        assert "FROM firms" in sql
        assert "FOR UPDATE" in sql

    def test_the_lock_is_narrowed_to_the_one_firm(self, firm_id):
        """Without the ``WHERE``, one firm's billing would queue behind every
        other firm's, which is the cost the row lock exists to avoid."""
        recorder = Recorder()

        firms.lock_firm(recorder, uuid.UUID(firm_id))

        sql = str(recorder.statements[0].compile(dialect=postgresql.dialect()))
        assert "WHERE firms.id" in sql


class TestCountingAndTakingAreOneStep:
    """The lock has to come first, or the count it guards is already stale."""

    @pytest.fixture
    def order(self, monkeypatch):
        """Records the sequence of lock and count calls."""
        events = []
        monkeypatch.setattr(firms, "lock_firm", lambda db, fid: events.append("lock"))
        monkeypatch.setattr(
            firms, "active_client_count", lambda db, fid: events.append("count") or 0
        )
        monkeypatch.setattr(
            firms, "active_user_count", lambda db, fid: events.append("count") or 0
        )
        return events

    def test_a_client_slot_is_counted_behind_the_lock(self, db: Session, firm_id, order):
        firm = db.get(Firm, uuid.UUID(firm_id))

        firms.claim_client_slot(db, firm)

        assert order == ["lock", "count"]

    def test_a_user_slot_is_counted_behind_the_lock(self, db: Session, firm_id, order):
        firm = db.get(Firm, uuid.UUID(firm_id))

        firms.claim_user_slot(db, firm)

        assert order == ["lock", "count"]

    def test_an_unlimited_plan_locks_nothing(self, db: Session, firm_id, order):
        """There is no number to be raced against, so no firm should be made to
        wait behind a count that cannot refuse anything."""
        firm = db.get(Firm, uuid.UUID(firm_id))
        firm.plan = FirmPlan.FIRM

        firms.claim_client_slot(db, firm)
        firms.claim_user_slot(db, firm)

        assert order == []


class TestTheCeilingCountsWhatIsActive:
    def test_a_full_firm_is_refused_in_the_words_of_its_plan(self, db: Session, firm_id):
        firm = db.get(Firm, uuid.UUID(firm_id))
        firm.plan = FirmPlan.SOLO  # one user; the owner already holds it

        with pytest.raises(firms.PlanLimitReached) as raised:
            firms.claim_user_slot(db, firm)

        assert "solo" in str(raised.value)
        assert "1 user" in str(raised.value)

    def test_a_deactivated_row_holds_no_slot(
        self, db: Session, firm_id, client_id, one_client_plan
    ):
        """Which is the whole reason deactivating is offered instead of deleting."""
        firm = db.get(Firm, uuid.UUID(firm_id))

        with pytest.raises(firms.PlanLimitReached):
            firms.claim_client_slot(db, firm)

        db.get(ClientModel, uuid.UUID(client_id)).is_active = False
        db.flush()

        firms.claim_client_slot(db, firm)  # the slot came back


class TestSwitchingOneBackOnCostsASlot:
    """Reactivation adds to the active count exactly as creation does.

    Without the guard the cap was a formality: deactivate ten, create ten more,
    switch the ten back on, and a fifty-client plan quietly holds sixty.
    """

    def make_client(self, client: TestClient, auth_headers: dict, name: str, pan: str):
        return client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(name=name, pan=pan, gstin=gstin_for(pan)),
        )

    def deactivate(self, client: TestClient, auth_headers: dict, client_id: str):
        response = client.patch(
            f"{API}/clients/{client_id}", headers=auth_headers, json={"is_active": False}
        )
        assert response.status_code == 200, response.text
        return response

    def test_a_full_firm_cannot_reactivate_a_client(
        self, client: TestClient, auth_headers: dict, two_client_plan, db: Session
    ):
        first = self.make_client(client, auth_headers, "First Co", "AABCF1234A").json()["client"]
        assert self.make_client(client, auth_headers, "Second Co", "AABCS2345B").status_code == 201

        self.deactivate(client, auth_headers, first["id"])
        # The freed slot is taken by somebody else before the owner changes
        # their mind.
        assert self.make_client(client, auth_headers, "Third Co", "AABCT3456C").status_code == 201

        response = client.patch(
            f"{API}/clients/{first['id']}", headers=auth_headers, json={"is_active": True}
        )

        assert response.status_code == 402
        assert "Upgrade" in response.json()["detail"]

    def test_a_refused_reactivation_leaves_the_client_switched_off(
        self, client: TestClient, auth_headers: dict, two_client_plan, db: Session
    ):
        """A 402 that half-applied would be worse than one that allowed it."""
        first = self.make_client(client, auth_headers, "First Co", "AABCF1234A").json()["client"]
        assert self.make_client(client, auth_headers, "Second Co", "AABCS2345B").status_code == 201
        self.deactivate(client, auth_headers, first["id"])
        assert self.make_client(client, auth_headers, "Third Co", "AABCT3456C").status_code == 201

        client.patch(
            f"{API}/clients/{first['id']}",
            headers=auth_headers,
            json={"is_active": True, "name": "Renamed Co"},
        )

        stored = db.get(ClientModel, uuid.UUID(first["id"]))
        db.refresh(stored)
        assert stored.is_active is False
        assert stored.name == "First Co"

    def test_room_left_means_the_reactivation_goes_through(
        self, client: TestClient, auth_headers: dict, two_client_plan
    ):
        """The guard must refuse a full firm, not every firm."""
        first = self.make_client(client, auth_headers, "First Co", "AABCF1234A").json()["client"]
        self.deactivate(client, auth_headers, first["id"])

        response = client.patch(
            f"{API}/clients/{first['id']}", headers=auth_headers, json={"is_active": True}
        )

        assert response.status_code == 200, response.text
        assert response.json()["client"]["is_active"] is True

    def test_re_saving_an_active_client_is_not_charged_a_second_slot(
        self, client: TestClient, auth_headers: dict, two_client_plan
    ):
        """The firm is at its ceiling and the row already holds one of the
        slots being counted, so charging again would refuse every edit that
        happens to carry ``is_active: true`` along with it."""
        first = self.make_client(client, auth_headers, "First Co", "AABCF1234A").json()["client"]
        assert self.make_client(client, auth_headers, "Second Co", "AABCS2345B").status_code == 201

        response = client.patch(
            f"{API}/clients/{first['id']}",
            headers=auth_headers,
            json={"is_active": True, "contact_person": "Someone New"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["client"]["contact_person"] == "Someone New"

    def test_an_edit_that_does_not_touch_the_flag_is_never_charged(
        self, client: TestClient, auth_headers: dict, two_client_plan
    ):
        first = self.make_client(client, auth_headers, "First Co", "AABCF1234A").json()["client"]
        assert self.make_client(client, auth_headers, "Second Co", "AABCS2345B").status_code == 201

        response = client.patch(
            f"{API}/clients/{first['id']}", headers=auth_headers, json={"phone": "+919812345000"}
        )

        assert response.status_code == 200, response.text


class TestSwitchingAPractitionerBackOnCostsASeat:
    """Seats are the same story, and the fixture firm's plan allows five."""

    def add_staff(self, client: TestClient, auth_headers: dict, index: int):
        return client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": f"Staff {index}",
                "email": f"staff{index}@sharma-ca.in",
                "password": "staff-password-123",
                "role": "junior",
            },
        )

    def fill_the_firm(self, client: TestClient, auth_headers: dict) -> list[dict]:
        """Four more on top of the owner puts the practice plan at its five."""
        staff = []
        for index in range(4):
            created = self.add_staff(client, auth_headers, index)
            assert created.status_code == 201, created.text
            staff.append(created.json())
        return staff

    def test_a_full_firm_cannot_reactivate_a_practitioner(
        self, client: TestClient, auth_headers: dict
    ):
        staff = self.fill_the_firm(client, auth_headers)

        off = client.patch(
            f"{API}/auth/practitioners/{staff[0]['id']}",
            headers=auth_headers,
            json={"is_active": False},
        )
        assert off.status_code == 200, off.text
        # Somebody is hired into the seat that just came free.
        assert self.add_staff(client, auth_headers, 99).status_code == 201

        response = client.patch(
            f"{API}/auth/practitioners/{staff[0]['id']}",
            headers=auth_headers,
            json={"is_active": True},
        )

        assert response.status_code == 402
        assert "Upgrade" in response.json()["detail"]

    def test_a_refused_reactivation_leaves_the_seat_switched_off(
        self, client: TestClient, auth_headers: dict, db: Session
    ):
        staff = self.fill_the_firm(client, auth_headers)
        client.patch(
            f"{API}/auth/practitioners/{staff[0]['id']}",
            headers=auth_headers,
            json={"is_active": False},
        )
        assert self.add_staff(client, auth_headers, 99).status_code == 201

        client.patch(
            f"{API}/auth/practitioners/{staff[0]['id']}",
            headers=auth_headers,
            json={"is_active": True, "full_name": "Renamed Person"},
        )

        listed = client.get(f"{API}/auth/practitioners", headers=auth_headers).json()
        stored = next(row for row in listed if row["id"] == staff[0]["id"])
        assert stored["is_active"] is False
        assert stored["full_name"] == "Staff 0"

    def test_a_reactivated_practitioner_cannot_log_back_in_after_a_402(
        self, client: TestClient, auth_headers: dict
    ):
        """The refusal is only worth something if the account stays shut."""
        staff = self.fill_the_firm(client, auth_headers)
        client.patch(
            f"{API}/auth/practitioners/{staff[0]['id']}",
            headers=auth_headers,
            json={"is_active": False},
        )
        assert self.add_staff(client, auth_headers, 99).status_code == 201
        client.patch(
            f"{API}/auth/practitioners/{staff[0]['id']}",
            headers=auth_headers,
            json={"is_active": True},
        )

        login = client.post(
            f"{API}/auth/login",
            json={"email": "staff0@sharma-ca.in", "password": "staff-password-123"},
        )

        assert login.status_code == 403

    def test_room_left_means_the_seat_comes_back(self, client: TestClient, auth_headers: dict):
        created = self.add_staff(client, auth_headers, 0)
        assert created.status_code == 201, created.text
        staff_id = created.json()["id"]
        client.patch(
            f"{API}/auth/practitioners/{staff_id}", headers=auth_headers, json={"is_active": False}
        )

        response = client.patch(
            f"{API}/auth/practitioners/{staff_id}", headers=auth_headers, json={"is_active": True}
        )

        assert response.status_code == 200, response.text
        assert response.json()["is_active"] is True

    def test_re_saving_an_active_practitioner_is_not_charged_a_second_seat(
        self, client: TestClient, auth_headers: dict
    ):
        staff = self.fill_the_firm(client, auth_headers)

        response = client.patch(
            f"{API}/auth/practitioners/{staff[0]['id']}",
            headers=auth_headers,
            json={"is_active": True, "phone": "+919812345001"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["phone"] == "+919812345001"


class TestTheCountIsPerFirm:
    """A cap that counted other tenants' rows would refuse a firm its own plan."""

    def test_another_firm_at_its_ceiling_does_not_fill_this_one(
        self, client: TestClient, auth_headers: dict, db: Session, firm_id
    ):
        from tests.conftest import FIRM_REGISTRATION

        registration = dict(FIRM_REGISTRATION)
        registration.update(
            firm_name="Neighbour & Co",
            firm_email="office@neighbour-ca.in",
            owner_email="owner@neighbour-ca.in",
        )
        assert client.post(f"{API}/auth/register", json=registration).status_code == 201

        firm = db.get(Firm, uuid.UUID(firm_id))
        assert firms.active_user_count(db, firm.id) == 1
        assert firms.active_client_count(db, firm.id) == 0

        # And the neighbour's owner is not counted into this firm's seats.
        neighbour = db.scalars(select(Firm).where(Firm.name == "Neighbour & Co")).one()
        assert firms.active_user_count(db, neighbour.id) == 1


class TestWhichRefusalAFullFirmIsGiven:
    """A firm at its ceiling still has to be told what actually stopped it.

    ``claim_client_slot`` used to run first, so every other thing wrong with
    the request was answered as a plan limit: "the solo plan allows 1 clients.
    Upgrade to add more", on a request that would have been refused anyway.

    That is the one refusal in this module that sends a practitioner somewhere
    else — to a pricing page — and neither of the two below is fixed by paying.
    Re-entering a client the firm already has under that PAN is a duplicate,
    and it is what a practitioner does when the search box did not find someone
    who is right there; naming an assignee who has been switched off is a stale
    id from a screen listing a member who has since left. Both were reported as
    money.

    ``add_practitioner`` already ordered these the other way round, for exactly
    this reason. The client endpoints are the same decision by the same door.
    """

    def add_client(self, client: TestClient, auth_headers: dict, **overrides):
        return client.post(
            f"{API}/clients", headers=auth_headers, json=make_client_payload(**overrides)
        )

    def test_a_duplicate_pan_is_a_conflict_rather_than_a_plan_limit(
        self, client: TestClient, auth_headers: dict, one_client_plan
    ):
        first = self.add_client(client, auth_headers, name="Kumar Enterprises", pan="AABCK1234A")
        assert first.status_code == 201, first.text

        response = self.add_client(
            client, auth_headers, name="Kumar Enterprises", pan="AABCK1234A"
        )

        assert response.status_code == 409, response.text
        assert "AABCK1234A" in response.json()["detail"]

    def test_an_assignee_who_has_left_is_a_bad_request_rather_than_a_plan_limit(
        self, client: TestClient, auth_headers: dict, one_client_plan, db: Session
    ):
        added = client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "Meera Iyer",
                "email": "meera@sharma-ca.in",
                "password": "another-good-password",
                "role": "junior",
            },
        )
        assert added.status_code == 201, added.text
        client.patch(
            f"{API}/auth/practitioners/{added.json()['id']}",
            headers=auth_headers,
            json={"is_active": False},
        )
        assert self.add_client(client, auth_headers, name="First Co", pan="AABCF1234A").status_code == 201

        response = self.add_client(
            client,
            auth_headers,
            name="Second Co",
            pan="AABCS2345B",
            assigned_practitioner_id=added.json()["id"],
        )

        assert response.status_code == 400, response.text
        assert "Meera Iyer" in response.json()["detail"]

    def test_a_full_firm_with_nothing_else_wrong_still_gets_the_plan_limit(
        self, client: TestClient, auth_headers: dict, one_client_plan
    ):
        """The reordering must not blunt the cap itself."""
        assert self.add_client(client, auth_headers, name="First Co", pan="AABCF1234A").status_code == 201

        response = self.add_client(client, auth_headers, name="Second Co", pan="AABCS2345B")

        assert response.status_code == 402, response.text
        assert "Upgrade" in response.json()["detail"]

    def test_reactivating_names_the_real_refusal_too(
        self, client: TestClient, auth_headers: dict, two_client_plan
    ):
        """The patch reaches the same claim by the other door, and it can carry
        a duplicate PAN in the very same request."""
        first = self.add_client(client, auth_headers, name="First Co", pan="AABCF1234A")
        assert first.status_code == 201, first.text
        second = self.add_client(client, auth_headers, name="Second Co", pan="AABCS2345B")
        assert second.status_code == 201, second.text

        client.patch(
            f"{API}/clients/{first.json()['client']['id']}",
            headers=auth_headers,
            json={"is_active": False},
        )
        assert self.add_client(client, auth_headers, name="Third Co", pan="AABCT3456C").status_code == 201

        response = client.patch(
            f"{API}/clients/{first.json()['client']['id']}",
            headers=auth_headers,
            json={"is_active": True, "pan": "AABCS2345B"},
        )

        assert response.status_code == 409, response.text
        assert "AABCS2345B" in response.json()["detail"]
