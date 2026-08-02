"""Registration, login, JWT and role enforcement."""

from __future__ import annotations

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.core.security import (
    TokenError,
    _absent_account_hash,
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from tests.conftest import FIRM_REGISTRATION

API = "/api/v1"


class TestPasswordHashing:
    def test_round_trip(self):
        digest = hash_password("correct-horse-battery")
        assert digest != "correct-horse-battery"
        assert verify_password("correct-horse-battery", digest)
        assert not verify_password("wrong-password", digest)

    def test_rejects_overlong_password(self):
        with pytest.raises(ValueError, match="at most 72 bytes"):
            hash_password("x" * 73)

    def test_malformed_hash_is_not_a_match(self):
        assert not verify_password("anything", "not-a-bcrypt-hash")


class TestTokens:
    def test_decode_round_trip(self):
        token = create_access_token(
            practitioner_id="11111111-1111-1111-1111-111111111111",
            firm_id="22222222-2222-2222-2222-222222222222",
            role="owner",
        )
        payload = decode_token(token)
        assert payload["sub"] == "11111111-1111-1111-1111-111111111111"
        assert payload["role"] == "owner"

    def test_expired_token_rejected(self):
        token = create_access_token(
            practitioner_id="1", firm_id="2", role="owner", expires_minutes=-1
        )
        with pytest.raises(TokenError, match="expired"):
            decode_token(token)

    def test_wrong_token_type_rejected(self):
        token = create_access_token(practitioner_id="1", firm_id="2", role="owner")
        with pytest.raises(TokenError, match="Unexpected token type"):
            decode_token(token, expected_type="magic_link")

    def test_tampered_token_rejected(self):
        token = create_access_token(practitioner_id="1", firm_id="2", role="owner")
        with pytest.raises(TokenError):
            decode_token(token[:-3] + "abc")


class TestRegistration:
    def test_register_creates_firm_and_owner(self, registered_firm: dict):
        assert registered_firm["token_type"] == "bearer"
        assert registered_firm["firm"]["name"] == "Sharma & Associates"
        assert registered_firm["firm"]["plan"] == "practice"
        assert registered_firm["practitioner"]["role"] == "owner"
        assert registered_firm["practitioner"]["email"] == "anita@sharma-ca.in"
        assert "password" not in registered_firm["practitioner"]

    def test_duplicate_owner_email_conflicts(self, client: TestClient, registered_firm: dict):
        response = client.post(f"{API}/auth/register", json=FIRM_REGISTRATION)
        assert response.status_code == 409

    def test_invalid_pan_rejected(self, client: TestClient):
        payload = FIRM_REGISTRATION | {"pan": "NOTAPAN", "owner_email": "x@y.in"}
        response = client.post(f"{API}/auth/register", json=payload)
        assert response.status_code == 422

    def test_short_password_rejected(self, client: TestClient):
        payload = FIRM_REGISTRATION | {"owner_password": "short", "owner_email": "x@y.in"}
        response = client.post(f"{API}/auth/register", json=payload)
        assert response.status_code == 422


class TestLogin:
    def test_login_succeeds(self, client: TestClient, registered_firm: dict):
        response = client.post(
            f"{API}/auth/login",
            json={"email": "anita@sharma-ca.in", "password": "correct-horse-battery"},
        )
        assert response.status_code == 200
        assert response.json()["access_token"]

    def test_login_is_case_insensitive_on_email(self, client: TestClient, registered_firm: dict):
        response = client.post(
            f"{API}/auth/login",
            json={"email": "ANITA@Sharma-CA.in", "password": "correct-horse-battery"},
        )
        assert response.status_code == 200

    def test_wrong_password_rejected(self, client: TestClient, registered_firm: dict):
        response = client.post(
            f"{API}/auth/login",
            json={"email": "anita@sharma-ca.in", "password": "nope-nope-nope"},
        )
        assert response.status_code == 401

    def test_unknown_email_gives_same_error(self, client: TestClient, registered_firm: dict):
        response = client.post(
            f"{API}/auth/login",
            json={"email": "ghost@nowhere.in", "password": "correct-horse-battery"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Incorrect email or password"


class TestSignInDoesNotSayWhichEmailsAreCustomers:
    """The two ways to fail a sign-in have to be indistinguishable — by the clock too.

    An identical status code and detail message settle it for anyone reading
    the response. They settle nothing for anyone timing it: bcrypt costs about
    a quarter of a second, so skipping it when the email matched nobody makes
    "no such account" return in a handful of milliseconds while "wrong
    password" takes 250. Anyone with a list of email addresses and a stopwatch
    can then read off which firms bank here.

    Asserted by counting the hashing rather than by the wall clock, which is
    both flaky under a loaded CI box and vague about what went wrong. One
    bcrypt round per attempt, whichever way the attempt fails, is the property
    that closes the gap — and it fails loudly the moment a short-circuit is
    reintroduced.
    """

    @staticmethod
    def _count_password_checks(monkeypatch) -> list:
        checked = []
        real_checkpw = bcrypt.checkpw

        def counting_checkpw(password: bytes, hashed: bytes):
            checked.append(hashed)
            return real_checkpw(password, hashed)

        monkeypatch.setattr(bcrypt, "checkpw", counting_checkpw)
        return checked

    def test_a_wrong_password_costs_one_hash(
        self, client: TestClient, registered_firm: dict, monkeypatch
    ):
        checked = self._count_password_checks(monkeypatch)

        response = client.post(
            f"{API}/auth/login",
            json={"email": "anita@sharma-ca.in", "password": "nope-nope-nope"},
        )

        assert response.status_code == 401
        assert len(checked) == 1

    def test_an_email_nobody_holds_costs_exactly_the_same(
        self, client: TestClient, registered_firm: dict, monkeypatch
    ):
        checked = self._count_password_checks(monkeypatch)

        response = client.post(
            f"{API}/auth/login",
            json={"email": "ghost@nowhere.in", "password": "correct-horse-battery"},
        )

        assert response.status_code == 401
        # The row lookup found nothing, and a full round is spent regardless.
        assert len(checked) == 1

    def test_the_stand_in_hash_costs_what_a_real_password_costs(self):
        # Same algorithm and same work factor, or the timing gap simply
        # reappears smaller: bcrypt encodes both in the "$2b$12$" prefix.
        assert _absent_account_hash()[:7] == hash_password("correct-horse-battery")[:7]

    def test_the_stand_in_hash_is_built_once_and_reused(self):
        # Minting a fresh one per attempt would put the salt generation on only
        # one of the two paths, and cost a quarter-second of CPU per probe to
        # an endpoint that attackers are the heaviest users of.
        assert _absent_account_hash() is _absent_account_hash()

    def test_no_password_ever_matches_the_stand_in(self):
        # It hashes a random secret, so there is nothing to guess — but a
        # sign-in that succeeded against a missing account would be the worst
        # possible way to find that out.
        assert not verify_password("correct-horse-battery", None)
        assert not verify_password("", None)


class TestProtectedRoutes:
    def test_me_requires_a_token(self, client: TestClient):
        assert client.get(f"{API}/auth/me").status_code == 401

    def test_me_rejects_garbage_token(self, client: TestClient):
        response = client.get(f"{API}/auth/me", headers={"Authorization": "Bearer nonsense"})
        assert response.status_code == 401

    def test_me_returns_the_practitioner(self, client: TestClient, auth_headers: dict):
        response = client.get(f"{API}/auth/me", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["email"] == "anita@sharma-ca.in"


class TestPractitioners:
    def test_owner_can_add_a_practitioner(self, client: TestClient, auth_headers: dict):
        response = client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "Vikram Rao",
                "email": "vikram@sharma-ca.in",
                "password": "another-good-password",
                "role": "manager",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["role"] == "manager"

        listing = client.get(f"{API}/auth/practitioners", headers=auth_headers).json()
        assert {p["email"] for p in listing} == {"anita@sharma-ca.in", "vikram@sharma-ca.in"}

    def test_cannot_create_a_second_owner(self, client: TestClient, auth_headers: dict):
        response = client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "Second Owner",
                "email": "second@sharma-ca.in",
                "password": "another-good-password",
                "role": "owner",
            },
        )
        assert response.status_code == 400

    def test_duplicate_email_in_firm_conflicts(self, client: TestClient, auth_headers: dict):
        body = {
            "full_name": "Vikram Rao",
            "email": "vikram@sharma-ca.in",
            "password": "another-good-password",
            "role": "junior",
        }
        assert client.post(f"{API}/auth/practitioners", headers=auth_headers, json=body).status_code == 201
        assert client.post(f"{API}/auth/practitioners", headers=auth_headers, json=body).status_code == 409

    def test_junior_cannot_add_practitioners(self, client: TestClient, auth_headers: dict):
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
        junior_token = client.post(
            f"{API}/auth/login",
            json={"email": "junior@sharma-ca.in", "password": "junior-password-1"},
        ).json()["access_token"]

        response = client.post(
            f"{API}/auth/practitioners",
            headers={"Authorization": f"Bearer {junior_token}"},
            json={
                "full_name": "Someone Else",
                "email": "else@sharma-ca.in",
                "password": "yet-another-password",
                "role": "junior",
            },
        )
        assert response.status_code == 403

    def test_plan_user_limit_enforced(self, client: TestClient, auth_headers: dict):
        # The "practice" plan allows 5 users; the owner is already one of them.
        for index in range(4):
            created = client.post(
                f"{API}/auth/practitioners",
                headers=auth_headers,
                json={
                    "full_name": f"Staff {index}",
                    "email": f"staff{index}@sharma-ca.in",
                    "password": "staff-password-123",
                    "role": "junior",
                },
            )
            assert created.status_code == 201, created.text

        overflow = client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "One Too Many",
                "email": "toomany@sharma-ca.in",
                "password": "staff-password-123",
                "role": "junior",
            },
        )
        assert overflow.status_code == 402

    def test_deactivated_practitioner_cannot_log_in(self, client: TestClient, auth_headers: dict):
        created = client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "Temp Staff",
                "email": "temp@sharma-ca.in",
                "password": "temp-password-123",
                "role": "junior",
            },
        ).json()

        patched = client.patch(
            f"{API}/auth/practitioners/{created['id']}",
            headers=auth_headers,
            json={"is_active": False},
        )
        assert patched.status_code == 200
        assert patched.json()["is_active"] is False

        response = client.post(
            f"{API}/auth/login",
            json={"email": "temp@sharma-ca.in", "password": "temp-password-123"},
        )
        assert response.status_code == 403


class TestThePlanCeilingIsVisible:
    """A firm should see the ceiling before it hits it.

    The limit is enforced when a practitioner is added, and a 402 at that
    moment is the worst time to learn the plan is full. Serving the number
    alongside the firm lets the team screen say how many seats are left.
    """

    def test_the_firm_reports_its_seat_and_client_ceilings(
        self, client: TestClient, auth_headers: dict
    ):
        firm = client.get(f"{API}/auth/firm", headers=auth_headers).json()

        # The fixture firm is on the "practice" plan: 5 users, 200 clients.
        assert firm["user_limit"] == 5
        assert firm["client_limit"] == 200

    def test_an_unlimited_plan_says_so_rather_than_naming_a_number(
        self, client: TestClient, db
    ):
        """The top plan has no ceiling, and ``null`` is how that is said."""
        from sqlalchemy import select

        from app.models.base import FirmPlan
        from app.models.firm import Firm

        registration = dict(FIRM_REGISTRATION)
        registration.update(
            firm_name="Unlimited & Co",
            firm_email="office@unlimited-ca.in",
            owner_email="owner@unlimited-ca.in",
        )
        token = client.post(f"{API}/auth/register", json=registration).json()["access_token"]

        firm = db.scalars(select(Firm).where(Firm.name == "Unlimited & Co")).one()
        firm.plan = FirmPlan.FIRM
        db.commit()

        response = client.get(f"{API}/auth/firm", headers={"Authorization": f"Bearer {token}"})
        assert response.json()["user_limit"] is None
        assert response.json()["client_limit"] is None

    def test_the_ceiling_is_the_one_the_add_endpoint_enforces(
        self, client: TestClient, auth_headers: dict
    ):
        """A number that disagrees with the guard would be worse than none."""
        limit = client.get(f"{API}/auth/firm", headers=auth_headers).json()["user_limit"]

        # The owner already occupies one seat, so `limit - 1` more must fit.
        for index in range(limit - 1):
            created = client.post(
                f"{API}/auth/practitioners",
                headers=auth_headers,
                json={
                    "full_name": f"Seat {index}",
                    "email": f"seat{index}@sharma-ca.in",
                    "password": "seat-password-123",
                    "role": "junior",
                },
            )
            assert created.status_code == 201, created.text

        overflow = client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "Over The Line",
                "email": "over@sharma-ca.in",
                "password": "seat-password-123",
                "role": "junior",
            },
        )
        assert overflow.status_code == 402


class TestTheFirmKeepsAnAdministrator:
    """The owner must not be able to lock the firm out of its own account.

    Every other practitioner can be repaired by an admin, but the owner is
    protected from being edited by anyone else — so a change the owner makes to
    their own role or status is one nobody has the standing to undo. Left
    unguarded, a single PATCH strands the firm with no one who can add staff,
    read the audit trail, or reach the owner-only endpoints again.
    """

    def owner_id(self, client: TestClient, auth_headers: dict) -> str:
        return client.get(f"{API}/auth/me", headers=auth_headers).json()["id"]

    def test_the_owner_cannot_deactivate_themselves(
        self, client: TestClient, auth_headers: dict
    ):
        owner_id = self.owner_id(client, auth_headers)

        response = client.patch(
            f"{API}/auth/practitioners/{owner_id}",
            headers=auth_headers,
            json={"is_active": False},
        )

        assert response.status_code == 400
        assert "owner" in response.json()["detail"].lower()

    def test_a_refused_deactivation_leaves_the_owner_able_to_sign_in(
        self, client: TestClient, auth_headers: dict
    ):
        """The guard is only worth having if the refusal also rolls back."""
        owner_id = self.owner_id(client, auth_headers)
        client.patch(
            f"{API}/auth/practitioners/{owner_id}",
            headers=auth_headers,
            json={"is_active": False},
        )

        response = client.post(
            f"{API}/auth/login",
            json={
                "email": FIRM_REGISTRATION["owner_email"],
                "password": FIRM_REGISTRATION["owner_password"],
            },
        )
        assert response.status_code == 200

    def test_the_owner_cannot_demote_themselves(self, client: TestClient, auth_headers: dict):
        """Demotion is deactivation by another name — it drops admin rights."""
        owner_id = self.owner_id(client, auth_headers)

        response = client.patch(
            f"{API}/auth/practitioners/{owner_id}",
            headers=auth_headers,
            json={"role": "junior"},
        )

        assert response.status_code == 400
        assert client.get(f"{API}/auth/me", headers=auth_headers).json()["role"] == "owner"

    def test_the_owner_may_still_correct_their_own_details(
        self, client: TestClient, auth_headers: dict
    ):
        """The guard covers standing, not the whole record."""
        owner_id = self.owner_id(client, auth_headers)

        response = client.patch(
            f"{API}/auth/practitioners/{owner_id}",
            headers=auth_headers,
            json={"full_name": "Rohit Sharma", "phone": "+91 98200 11111"},
        )

        assert response.status_code == 200
        assert response.json()["full_name"] == "Rohit Sharma"
        assert response.json()["role"] == "owner"

    def test_a_partner_may_still_be_deactivated(self, client: TestClient, auth_headers: dict):
        """Only the owner is irreplaceable; the rest of the team is not."""
        partner = client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "Partner Patel",
                "email": "partner@sharma-ca.in",
                "password": "partner-password-1",
                "role": "partner",
            },
        ).json()

        response = client.patch(
            f"{API}/auth/practitioners/{partner['id']}",
            headers=auth_headers,
            json={"is_active": False},
        )

        assert response.status_code == 200
        assert response.json()["is_active"] is False
