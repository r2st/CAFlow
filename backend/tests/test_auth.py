"""Registration, login, JWT and role enforcement."""

from __future__ import annotations

import uuid

import bcrypt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import _active_firm
from app.core.security import (
    TokenError,
    _absent_account_hash,
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.base import PractitionerRole
from app.models.firm import Firm, Practitioner
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


class TestAPasswordIsMeasuredInBytes:
    """bcrypt takes at most 72 bytes; the schema capped 72 *characters*.

    UTF-8 spends three bytes on every Devanagari letter and two on every
    accented Latin one, so a thirty-character Hindi passphrase is ninety bytes
    — well inside the character cap and well outside what can be hashed.
    Nothing checked the bytes before the hash was attempted, so it reached
    ``hash_password``, raised, and came back as a 500 with the generic
    "something went wrong on our side" and a request id: nothing named the
    field, nothing said a shorter one would work, and the firm being turned
    away was signing up.

    The one class of user it hit is the one whose password is not in ASCII,
    which for a product sold to Indian accountants is not an edge.
    """

    # 35 characters, 105 bytes.
    HINDI_PASSPHRASE = "पासवर्ड" * 5
    # 60 characters, 75 bytes — three over, from the accents alone.
    ACCENTED = "café" * 15

    def test_the_two_measures_really_do_disagree(self):
        """Guards the premise: if this stops holding the tests below prove nothing."""
        assert len(self.HINDI_PASSPHRASE) <= 72 < len(self.HINDI_PASSPHRASE.encode())
        assert len(self.ACCENTED) <= 72 < len(self.ACCENTED.encode())

    @pytest.mark.parametrize("password", [HINDI_PASSPHRASE, ACCENTED])
    def test_registration_names_the_field_instead_of_failing(
        self, client: TestClient, password: str
    ):
        payload = FIRM_REGISTRATION | {
            "owner_password": password,
            "owner_email": "x@y.in",
        }
        response = client.post(f"{API}/auth/register", json=payload)

        assert response.status_code == 422, response.text
        body = response.json()
        assert body["error"]["fields"][0]["field"] == "owner_password"
        assert "bytes" in body["error"]["fields"][0]["message"]

    def test_the_refusal_happens_before_the_firm_is_built(
        self, client: TestClient, db: Session
    ):
        """``register_firm`` adds and flushes the firm before it hashes the
        owner's password, so the old failure got as far as a half-built firm
        and relied on the session rollback to undo it. Refused at the door,
        nothing is built to undo."""
        client.post(
            f"{API}/auth/register",
            json=FIRM_REGISTRATION
            | {"owner_password": self.HINDI_PASSPHRASE, "owner_email": "x@y.in"},
        )
        assert db.query(Firm).count() == 0

    def test_adding_a_team_member_is_refused_the_same_way(
        self, client: TestClient, auth_headers: dict
    ):
        response = client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "Vikram Rao",
                "email": "vikram@sharma-ca.in",
                "password": self.HINDI_PASSPHRASE,
                "role": "manager",
            },
        )

        assert response.status_code == 422, response.text
        assert response.json()["error"]["fields"][0]["field"] == "password"

    def test_a_passphrase_that_fits_is_still_accepted(self, client: TestClient):
        """The limit is bytes, not ASCII — non-Latin passwords are not banned."""
        password = "पासवर्ड-२०२६"  # 12 characters, 30 bytes
        assert len(password.encode()) <= 72
        payload = FIRM_REGISTRATION | {"owner_password": password, "owner_email": "x@y.in"}

        assert client.post(f"{API}/auth/register", json=payload).status_code == 201

        signed_in = client.post(
            f"{API}/auth/login", json={"email": "x@y.in", "password": password}
        )
        assert signed_in.status_code == 200, signed_in.text

    def test_seventy_two_ascii_characters_still_fit(self, client: TestClient):
        """The boundary is unchanged for the passwords that always worked."""
        payload = FIRM_REGISTRATION | {"owner_password": "a" * 72, "owner_email": "x@y.in"}
        assert client.post(f"{API}/auth/register", json=payload).status_code == 201

    def test_signing_in_with_one_too_long_is_still_just_a_wrong_password(
        self, client: TestClient, registered_firm: dict
    ):
        """Being checked is not the same as being set. No account can hold one,
        so it is wrong rather than malformed — and answering 422 would come
        back faster than a real attempt and say so."""
        response = client.post(
            f"{API}/auth/login",
            json={"email": "anita@sharma-ca.in", "password": self.HINDI_PASSPHRASE},
        )
        assert response.status_code == 401


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


class TestDeactivatingAFirmTakesEffectNow:
    """A firm that is no longer served stops being served at once.

    Sign-in has always refused a deactivated firm, and the Celery jobs have
    always skipped one — but an access token lives twelve hours, and the
    resolution of "is this firm still active?" sat in ``get_current_firm``,
    which only the handful of endpoints that need the firm object declare. So a
    firm switched off in the morning went on updating filings, issuing invoices
    and handing out portal links all day, while nothing was generated for it
    and no reminder went out: half switched off, in the half nobody sees.

    The check belongs where the practitioner is resolved, so every endpoint
    that takes a practitioner inherits it.
    """

    @pytest.fixture
    def deactivate(self, db, firm_id):
        def go():
            firm = db.get(Firm, uuid.UUID(firm_id))
            firm.is_active = False
            db.commit()

        return go

    def test_a_token_issued_before_it_stops_working(
        self, client: TestClient, auth_headers: dict, deactivate
    ):
        assert client.get(f"{API}/auth/me", headers=auth_headers).status_code == 200

        deactivate()

        response = client.get(f"{API}/auth/me", headers=auth_headers)
        assert response.status_code == 403
        assert response.json()["detail"] == "Firm is not active"

    def test_an_endpoint_that_never_asks_for_the_firm_refuses_too(
        self, client: TestClient, auth_headers: dict, deactivate
    ):
        """The whole point: the old check was only on the endpoints that
        happened to want the firm object, which is a small minority of them."""
        deactivate()

        assert client.get(f"{API}/clients", headers=auth_headers).status_code == 403
        assert (
            client.get(f"{API}/compliance/dashboard", headers=auth_headers).status_code == 403
        )
        assert client.get(f"{API}/tasks", headers=auth_headers).status_code == 403

    def test_writes_are_refused_as_well_as_reads(
        self, client: TestClient, auth_headers: dict, client_id: str, deactivate
    ):
        deactivate()

        response = client.post(
            f"{API}/tasks",
            json={"title": "File GSTR-3B", "client_id": client_id},
            headers=auth_headers,
        )
        assert response.status_code == 403

    def test_signing_in_again_is_refused_too(self, client: TestClient, deactivate):
        """The other half of the same rule, and the one that already held."""
        deactivate()

        response = client.post(
            f"{API}/auth/login",
            json={
                "email": FIRM_REGISTRATION["owner_email"],
                "password": FIRM_REGISTRATION["owner_password"],
            },
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Firm is not active"

    def test_reactivating_restores_the_same_token(
        self, client: TestClient, auth_headers: dict, db, firm_id: str, deactivate
    ):
        """Deactivation is a state, not a revocation — nothing is blocklisted."""
        deactivate()
        assert client.get(f"{API}/auth/me", headers=auth_headers).status_code == 403

        firm = db.get(Firm, uuid.UUID(firm_id))
        firm.is_active = True
        db.commit()

        assert client.get(f"{API}/auth/me", headers=auth_headers).status_code == 200

    def test_a_deleted_firm_takes_its_practitioners_credentials_with_it(
        self, client: TestClient, auth_headers: dict, db, firm_id: str
    ):
        """Deletion cascades, so the token stops resolving to anyone at all."""
        db.delete(db.get(Firm, uuid.UUID(firm_id)))
        db.commit()

        assert client.get(f"{API}/auth/me", headers=auth_headers).status_code == 401

    def test_a_firm_row_that_is_simply_gone_is_refused_not_returned(self, db):
        """Held here rather than through an endpoint, because the cascade above
        means no request can reach this branch — which is exactly why a `None`
        slipping through as "active" would go unnoticed."""
        with pytest.raises(HTTPException) as raised:
            _active_firm(db, uuid.uuid4())

        assert raised.value.status_code == 403


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


class TestAnEmailSignsInToOneAccount:
    """An address reaches one account, or it reaches an unusable one.

    ``/auth/login`` is handed an address and a password and no firm, so the
    address is the identity. Uniqueness was per firm, and the duplicate check
    when adding a team member only looked inside the adding firm — so two firms
    could each hold an account for one address, and the second was unreachable
    for ever: sign-in resolved to one of the rows, that row's hash never matched
    the other password, and the member was told their own credentials were
    wrong. The firm that added them had seen a 201.

    Not a contrived pairing. A part-time accountant on two firms' books is one
    route to it; someone leaving one practice for another is the ordinary one,
    because a departing member's row is deactivated and kept for the history
    hanging off it.
    """

    SHARED = "bob@shared-accountant.in"

    def _second_firm_token(self, client: TestClient) -> str:
        payload = FIRM_REGISTRATION | {
            "firm_name": "Iyer & Co",
            "firm_email": "office@iyer-ca.in",
            "owner_email": "meera@iyer-ca.in",
            "icai_registration_number": "998877S",
        }
        response = client.post(f"{API}/auth/register", json=payload)
        assert response.status_code == 201, response.text
        return response.json()["access_token"]

    def _add(self, client: TestClient, token: str, password: str, name: str):
        return client.post(
            f"{API}/auth/practitioners",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "full_name": name,
                "email": self.SHARED,
                "password": password,
                "role": "junior",
            },
        )

    def test_a_second_firm_cannot_add_an_address_already_in_use(
        self, client: TestClient, registered_firm: dict, auth_headers: dict
    ):
        second = self._second_firm_token(client)
        first = self._add(client, registered_firm["access_token"], "sharma-password-1", "Bob")
        assert first.status_code == 201, first.text

        clash = self._add(client, second, "iyer-password-1", "Bob")
        assert clash.status_code == 409, clash.text
        # The refusal must not confirm that the address belongs to someone at
        # another firm — an admin may type any address into this form, and the
        # answer is not theirs to have.
        assert "iyer" not in clash.text.lower()
        assert "sharma" not in clash.text.lower()

    def test_the_account_that_does_exist_still_signs_in(
        self, client: TestClient, registered_firm: dict
    ):
        second = self._second_firm_token(client)
        self._add(client, registered_firm["access_token"], "sharma-password-1", "Bob")
        self._add(client, second, "iyer-password-1", "Bob")

        response = client.post(
            f"{API}/auth/login",
            json={"email": self.SHARED, "password": "sharma-password-1"},
        )
        assert response.status_code == 200, response.text

    def test_rejoining_at_another_firm_is_refused_rather_than_locked_out(
        self, client: TestClient, registered_firm: dict, auth_headers: dict
    ):
        """The case that made this silent: leaving one firm and joining another.

        The old code created the second account happily and then refused every
        sign-in against it. Refusing the *creation* is what turns an
        indefinite, unexplained lockout into a message at the moment someone
        can still act on it.
        """
        second = self._second_firm_token(client)
        created = self._add(
            client, registered_firm["access_token"], "sharma-password-1", "Bob"
        ).json()
        gone = client.patch(
            f"{API}/auth/practitioners/{created['id']}",
            headers=auth_headers,
            json={"is_active": False},
        )
        assert gone.status_code == 200, gone.text

        rejoin = self._add(client, second, "iyer-password-1", "Bob")
        assert rejoin.status_code == 409, rejoin.text

    def test_the_database_refuses_it_even_when_the_route_is_bypassed(
        self, db: Session, registered_firm: dict
    ):
        """Two firms adding one address at the same moment both read "free".

        The route's check is a read followed by a write, so under concurrency
        it is a suggestion. The unique index is the part that holds.
        """
        firm_id = uuid.UUID(registered_firm["firm"]["id"])
        other = Firm(name="Iyer & Co", email="office@iyer-ca.in")
        db.add(other)
        db.flush()

        for owner_firm in (firm_id, other.id):
            db.add(
                Practitioner(
                    firm_id=owner_firm,
                    full_name="Bob",
                    email=self.SHARED,
                    password_hash=hash_password("a-password-here"),
                    role=PractitionerRole.JUNIOR,
                )
            )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_case_only_differences_are_the_same_address(
        self, client: TestClient, registered_firm: dict
    ):
        """Sign-in lowercases, so ``Bob@`` and ``bob@`` must not be two accounts."""
        second = self._second_firm_token(client)
        assert (
            self._add(client, registered_firm["access_token"], "sharma-password-1", "Bob")
        ).status_code == 201

        clash = client.post(
            f"{API}/auth/practitioners",
            headers={"Authorization": f"Bearer {second}"},
            json={
                "full_name": "Bob",
                "email": self.SHARED.upper(),
                "password": "iyer-password-1",
                "role": "junior",
            },
        )
        assert clash.status_code == 409, clash.text

    def test_a_full_firm_reusing_an_address_is_told_which_one_stopped_it(
        self, client: TestClient, registered_firm: dict, auth_headers: dict
    ):
        """A duplicate is a duplicate, not a reason to go and buy more seats.

        The slot was claimed before the address was checked, so a firm at its
        plan ceiling that mistyped an existing address was answered 402 and
        sent to the pricing page over a typo.
        """
        # The "practice" plan allows 5; the owner holds one.
        for index in range(4):
            filled = client.post(
                f"{API}/auth/practitioners",
                headers=auth_headers,
                json={
                    "full_name": f"Staff {index}",
                    "email": f"seat{index}@sharma-ca.in",
                    "password": "staff-password-123",
                    "role": "junior",
                },
            )
            assert filled.status_code == 201, filled.text

        response = client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "Duplicate Anita",
                "email": FIRM_REGISTRATION["owner_email"],
                "password": "another-good-password",
                "role": "junior",
            },
        )
        assert response.status_code == 409, response.text


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


class TestAnExplicitNullOnATeamMembersRequiredFields:
    """``phone`` is optional because a member may not have given one — ``null``
    clears it. ``role`` is optional because a PATCH need not name it; the
    column is ``NOT NULL``, so ``null`` is not an instruction.

    ``exclude_unset`` cannot separate them, so the null reached the database
    and came back a 409 saying the change "conflicts with an existing record" —
    which is what a duplicate address says, and is not what happened.
    """

    def _member(self, client: TestClient, auth_headers: dict) -> dict:
        response = client.post(
            f"{API}/auth/practitioners",
            headers=auth_headers,
            json={
                "full_name": "Temp Staff",
                "email": "temp@sharma-ca.in",
                "password": "temp-password-123",
                "role": "junior",
                "phone": "+919876500011",
                "membership_number": "123456",
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    @pytest.mark.parametrize("field", ["full_name", "role", "is_active"])
    def test_a_null_is_refused_by_name(
        self, client: TestClient, auth_headers: dict, field: str
    ):
        member = self._member(client, auth_headers)

        response = client.patch(
            f"{API}/auth/practitioners/{member['id']}",
            headers=auth_headers,
            json={field: None},
        )

        assert response.status_code == 422, response.text
        assert response.json()["error"]["fields"][0]["field"] == field
        assert "null" in response.json()["detail"]

    def test_the_member_is_left_exactly_as_they_were(
        self, client: TestClient, auth_headers: dict
    ):
        """The refusal is total — nothing in the same body is applied.

        Which matters here more than elsewhere: ``is_active`` is what decides
        whether this person can sign in at all.
        """
        member = self._member(client, auth_headers)

        response = client.patch(
            f"{API}/auth/practitioners/{member['id']}",
            headers=auth_headers,
            json={"full_name": "Renamed", "is_active": None},
        )
        assert response.status_code == 422, response.text

        login = client.post(
            f"{API}/auth/login",
            json={"email": "temp@sharma-ca.in", "password": "temp-password-123"},
        )
        assert login.status_code == 200, login.text
        assert login.json()["practitioner"]["full_name"] == "Temp Staff"

    @pytest.mark.parametrize("field", ["phone", "membership_number"])
    def test_the_genuinely_optional_fields_stay_clearable(
        self, client: TestClient, auth_headers: dict, field: str
    ):
        member = self._member(client, auth_headers)

        response = client.patch(
            f"{API}/auth/practitioners/{member['id']}",
            headers=auth_headers,
            json={field: None},
        )

        assert response.status_code == 200, response.text
        assert response.json()[field] is None

    def test_omitting_a_required_field_still_leaves_it_alone(
        self, client: TestClient, auth_headers: dict
    ):
        """``mode="before"`` on a named field only runs when the caller sent
        it, so an absent field is untouched — which is the whole distinction."""
        member = self._member(client, auth_headers)

        response = client.patch(
            f"{API}/auth/practitioners/{member['id']}",
            headers=auth_headers,
            json={"phone": "+919876500022"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["full_name"] == "Temp Staff"
        assert response.json()["role"] == "junior"
        assert response.json()["is_active"] is True
