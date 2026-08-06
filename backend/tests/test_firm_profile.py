"""The firm's own profile, and the tax that follows from it.

Two of these fields are not description. A firm's GSTIN and its state are what
:func:`~app.services.gst.resolve_supply` compares against the client's to decide
whether an invoice carries CGST plus SGST or IGST, and until this endpoint
existed there was no way to set either after sign-up — which asked for neither
GSTIN nor address. Every firm therefore reached its first invoice with an
undetermined supply, and a practice billing across a state line issued the wrong
tax head on a document its client claims credit against.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.models.base import SupplyType
from app.models.invoice import Invoice
from tests.conftest import FIRM_REGISTRATION, make_client_payload

API = "/api/v1"

# A checksum-valid GSTIN in Maharashtra, and one in Karnataka, wrapping the PAN
# the registration fixture already uses. Two states, because the whole subject
# is what happens when the firm's and the client's differ.
FIRM_GSTIN_MH = "27AAACS1234F1ZS"
FIRM_GSTIN_KA = "29AAACS1234F1ZO"


def _member_headers(client: TestClient, auth_headers: dict, role: str) -> dict[str, str]:
    """Sign in as a newly added team member of ``role``."""
    email = f"{role}@sharma-ca.in"
    created = client.post(
        f"{API}/auth/practitioners",
        headers=auth_headers,
        json={
            "full_name": f"{role.title()} Member",
            "email": email,
            "password": "a-perfectly-good-password",
            "role": role,
        },
    )
    assert created.status_code == 201, created.text
    token = client.post(
        f"{API}/auth/login",
        json={"email": email, "password": "a-perfectly-good-password"},
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestTheGstinCanFinallyBeEntered:
    """The gap this endpoint closes.

    Sign-up never asked for a GSTIN and nothing could add one afterwards, so
    the field that decides the tax head on every invoice was permanently empty
    on every firm.
    """

    def test_the_firm_starts_without_one(self, registered_firm: dict):
        assert registered_firm["firm"]["gstin"] is None

    def test_it_can_be_set(self, client: TestClient, auth_headers: dict):
        response = client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"gstin": FIRM_GSTIN_MH}
        )
        assert response.status_code == 200, response.text
        assert response.json()["gstin"] == FIRM_GSTIN_MH

    def test_it_survives_being_read_back(self, client: TestClient, auth_headers: dict):
        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"gstin": FIRM_GSTIN_MH})
        assert (
            client.get(f"{API}/auth/firm", headers=auth_headers).json()["gstin"]
            == FIRM_GSTIN_MH
        )

    def test_a_malformed_one_is_refused(self, client: TestClient, auth_headers: dict):
        response = client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"gstin": "27AAACS1234F1ZZ"}
        )
        assert response.status_code == 422, response.text
        assert "check digit" in response.text

    def test_the_sign_up_form_now_takes_one_too(self, client: TestClient):
        """So a firm entering it at sign-up need not find this screen at all."""
        response = client.post(
            f"{API}/auth/register",
            json={
                **FIRM_REGISTRATION,
                "firm_email": "new@example-ca.in",
                "owner_email": "new-owner@example-ca.in",
                "gstin": FIRM_GSTIN_MH,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["firm"]["gstin"] == FIRM_GSTIN_MH


class TestWhatTheGstinChangesOnAnInvoice:
    """The point of the whole thing, asserted where it lands.

    Not that the column holds a string, but that an invoice raised afterwards
    is taxed under the right heads — because that is the number the client
    claims input credit against.
    """

    def _client_in(self, client: TestClient, auth_headers: dict, state: str, gstin: str) -> str:
        response = client.post(
            f"{API}/clients",
            json=make_client_payload(state=state, gstin=gstin, pan=gstin[2:12]),
            headers=auth_headers,
        )
        assert response.status_code == 201, response.text
        return response.json()["client"]["id"]

    def _invoice(self, client: TestClient, auth_headers: dict, client_id: str) -> dict:
        response = client.post(
            f"{API}/invoices",
            headers=auth_headers,
            json={
                "client_id": client_id,
                "lines": [{"description": "Annual retainer", "unit_price_paise": 10_000_00}],
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    def test_a_firm_with_no_gstin_taxes_everything_as_intra_state(
        self, client: TestClient, auth_headers: dict
    ):
        """The old behaviour, and why it was wrong.

        The firm's typed state is "Maharashtra", so a Karnataka client is an
        inter-state supply — but with the firm's own state resolvable only from
        that free-text field this is the *best* case. A firm that typed nothing
        recognisable got the same answer for every client it has.
        """
        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"state": None})
        karnataka = self._client_in(client, auth_headers, "Karnataka", "29AAPFU0939F1ZR")
        invoice = self._invoice(client, auth_headers, karnataka)

        assert invoice["supply_type"] == SupplyType.INTRA_STATE.value
        assert invoice["igst_paise"] == 0
        assert invoice["place_of_supply_label"] == "29-Karnataka"

    def test_with_the_firms_gstin_set_an_out_of_state_client_gets_igst(
        self, client: TestClient, auth_headers: dict
    ):
        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"gstin": FIRM_GSTIN_MH})
        karnataka = self._client_in(client, auth_headers, "Karnataka", "29AAPFU0939F1ZR")
        invoice = self._invoice(client, auth_headers, karnataka)

        assert invoice["supply_type"] == SupplyType.INTER_STATE.value
        assert invoice["igst_paise"] == invoice["tax_paise"]
        assert invoice["cgst_paise"] == 0 and invoice["sgst_paise"] == 0

    def test_a_client_in_the_firms_own_state_still_gets_cgst_and_sgst(
        self, client: TestClient, auth_headers: dict
    ):
        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"gstin": FIRM_GSTIN_MH})
        maharashtra = self._client_in(client, auth_headers, "Maharashtra", "27AAPFU0939F1ZV")
        invoice = self._invoice(client, auth_headers, maharashtra)

        assert invoice["supply_type"] == SupplyType.INTRA_STATE.value
        assert invoice["igst_paise"] == 0
        assert invoice["cgst_paise"] + invoice["sgst_paise"] == invoice["tax_paise"]

    def test_the_firms_gstin_outranks_its_typed_state(
        self, client: TestClient, auth_headers: dict
    ):
        """A registration number is stated; an address field is interpreted.

        The registration fixture types "Maharashtra". A firm registered in
        Karnataka and billing a Karnataka client is making a local supply, and
        the stale free-text state must not turn it into an inter-state one.
        """
        client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"gstin": FIRM_GSTIN_KA}
        )
        karnataka = self._client_in(client, auth_headers, "Karnataka", "29AAPFU0939F1ZR")
        invoice = self._invoice(client, auth_headers, karnataka)

        assert invoice["supply_type"] == SupplyType.INTRA_STATE.value

    def test_an_invoice_already_issued_is_not_re_taxed(
        self, client: TestClient, auth_headers: dict, db
    ):
        """A sent invoice is a document of record.

        Raised while the firm's state was unresolvable, so it carries the
        intra-state fallback. Entering the GSTIN afterwards — which is the
        ordinary thing to do, and the whole reason this endpoint exists — must
        not silently restate the tax on a bill the client is holding a copy of.
        """
        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"state": None})
        karnataka = self._client_in(client, auth_headers, "Karnataka", "29AAPFU0939F1ZR")
        invoice_id = self._invoice(client, auth_headers, karnataka)["id"]
        sent = client.post(f"{API}/invoices/{invoice_id}/send", headers=auth_headers)
        assert sent.json()["supply_type"] == SupplyType.INTRA_STATE.value

        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"gstin": FIRM_GSTIN_MH})

        issued = client.get(f"{API}/invoices/{invoice_id}", headers=auth_headers).json()
        assert issued["supply_type"] == SupplyType.INTRA_STATE.value
        assert issued["igst_paise"] == 0
        assert db.get(Invoice, uuid.UUID(invoice_id)).igst_paise == 0

    def test_a_draft_does_follow_the_correction(
        self, client: TestClient, auth_headers: dict
    ):
        """A draft has not gone out, so correcting the firm corrects the draft.

        The re-resolution happens when the draft is next edited, which is what
        ``update_invoice`` already does for a client's own GSTIN.
        """
        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"state": None})
        karnataka = self._client_in(client, auth_headers, "Karnataka", "29AAPFU0939F1ZR")
        invoice_id = self._invoice(client, auth_headers, karnataka)["id"]
        assert (
            client.get(f"{API}/invoices/{invoice_id}", headers=auth_headers).json()[
                "supply_type"
            ]
            == SupplyType.INTRA_STATE.value
        )

        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"gstin": FIRM_GSTIN_MH})
        edited = client.patch(
            f"{API}/invoices/{invoice_id}",
            headers=auth_headers,
            json={"notes": "Retainer for FY"},
        )

        assert edited.status_code == 200, edited.text
        assert edited.json()["supply_type"] == SupplyType.INTER_STATE.value
        assert edited.json()["igst_paise"] == edited.json()["tax_paise"]


class TestWhatTheFirmIsToldItsTaxPositionIs:
    """``place_of_supply_label`` on the firm, so it is visible before an invoice.

    A firm should not learn that its supplies were undetermined from a client
    who could not claim the credit.
    """

    def test_it_is_empty_when_nothing_names_a_state(
        self, client: TestClient, auth_headers: dict
    ):
        response = client.patch(f"{API}/auth/firm", headers=auth_headers, json={"state": None})
        assert response.json()["place_of_supply_label"] is None

    def test_a_typed_state_resolves_it(self, client: TestClient, auth_headers: dict):
        assert (
            client.get(f"{API}/auth/firm", headers=auth_headers).json()[
                "place_of_supply_label"
            ]
            == "27-Maharashtra"
        )

    def test_a_gstin_resolves_it_and_wins(self, client: TestClient, auth_headers: dict):
        response = client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"gstin": FIRM_GSTIN_KA}
        )
        assert response.json()["place_of_supply_label"] == "29-Karnataka"

    def test_a_renamed_state_still_resolves(self, client: TestClient, auth_headers: dict):
        """A firm that typed "Orissa" years ago means Odisha."""
        response = client.patch(f"{API}/auth/firm", headers=auth_headers, json={"state": "Orissa"})
        assert response.json()["place_of_supply_label"] == "21-Odisha"

    def test_it_is_on_the_sign_in_response_too(
        self, client: TestClient, registered_firm: dict
    ):
        """The web app reads the firm off the token response, not a second call."""
        response = client.post(
            f"{API}/auth/login",
            json={
                "email": FIRM_REGISTRATION["owner_email"],
                "password": FIRM_REGISTRATION["owner_password"],
            },
        )
        assert "place_of_supply_label" in response.json()["firm"]


class TestTheSupplierAddressATaxInvoiceNeeds:
    """Rule 46(b): the supplier's address and PIN are mandatory particulars.

    The columns existed; nothing ever asked for them or could set them.
    """

    def test_the_address_can_be_set(self, client: TestClient, auth_headers: dict):
        response = client.patch(
            f"{API}/auth/firm",
            headers=auth_headers,
            json={
                "address_line1": "204 Kumar Chambers",
                "address_line2": "Fergusson College Road",
                "pincode": "411004",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["address_line1"] == "204 Kumar Chambers"
        assert body["address_line2"] == "Fergusson College Road"
        assert body["pincode"] == "411004"

    def test_sign_up_takes_the_address_as_well(self, client: TestClient):
        response = client.post(
            f"{API}/auth/register",
            json={
                **FIRM_REGISTRATION,
                "firm_email": "addressed@example-ca.in",
                "owner_email": "addressed-owner@example-ca.in",
                "address_line1": "204 Kumar Chambers",
                "pincode": "411004",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["firm"]["pincode"] == "411004"

    @pytest.mark.parametrize("pincode", ["41100", "4110045", "011004", "41100A", "abcdef"])
    def test_a_pin_that_is_not_one_is_refused(
        self, client: TestClient, auth_headers: dict, pincode: str
    ):
        response = client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"pincode": pincode}
        )
        assert response.status_code == 422, response.text

    def test_a_pin_typed_with_a_space_is_accepted(
        self, client: TestClient, auth_headers: dict
    ):
        """"411 004" is how it is written on letterhead."""
        response = client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"pincode": "411 004"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["pincode"] == "411004"

    def test_the_address_can_be_emptied(self, client: TestClient, auth_headers: dict):
        client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"address_line2": "Suite 4"}
        )
        response = client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"address_line2": None}
        )
        assert response.status_code == 200, response.text
        assert response.json()["address_line2"] is None


class TestWhatAFirmMayNotSetOnItself:
    """The two fields whose absence from the schema is the security property."""

    def test_the_plan_cannot_be_patched(self, client: TestClient, auth_headers: dict):
        """``plan`` is where the client and user limits are read from.

        Accepting it here would let any firm admin lift their own limits by
        sending a field. Pydantic ignores what the schema does not declare, so
        the request succeeds and the plan does not move — which is the
        behaviour worth pinning down, since the failure mode is silent.
        """
        before = client.get(f"{API}/auth/firm", headers=auth_headers).json()
        assert before["plan"] == "practice"

        response = client.patch(f"{API}/auth/firm", headers=auth_headers, json={"plan": "firm"})

        assert response.status_code == 200, response.text
        assert response.json()["plan"] == "practice"
        assert response.json()["client_limit"] == before["client_limit"]

    def test_the_firms_standing_cannot_be_patched(
        self, client: TestClient, auth_headers: dict
    ):
        """Every sign-in checks ``is_active``; a firm switching itself off
        would lock out its own owner with nobody left able to undo it."""
        response = client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"is_active": False}
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_active"] is True

        # And the account still signs in.
        assert (
            client.post(
                f"{API}/auth/login",
                json={
                    "email": FIRM_REGISTRATION["owner_email"],
                    "password": FIRM_REGISTRATION["owner_password"],
                },
            ).status_code
            == 200
        )

    def test_the_name_cannot_be_cleared(self, client: TestClient, auth_headers: dict):
        response = client.patch(f"{API}/auth/firm", headers=auth_headers, json={"name": None})
        assert response.status_code == 422, response.text

    def test_the_contact_email_cannot_be_cleared(
        self, client: TestClient, auth_headers: dict
    ):
        response = client.patch(f"{API}/auth/firm", headers=auth_headers, json={"email": None})
        assert response.status_code == 422, response.text


class TestWhoMayEditTheFirm:
    """Owners and partners, which is where firm administration already sits."""

    def test_the_owner_may(self, client: TestClient, auth_headers: dict):
        assert (
            client.patch(
                f"{API}/auth/firm", headers=auth_headers, json={"city": "Mumbai"}
            ).status_code
            == 200
        )

    def test_a_partner_may(self, client: TestClient, auth_headers: dict):
        headers = _member_headers(client, auth_headers, "partner")
        assert (
            client.patch(f"{API}/auth/firm", headers=headers, json={"city": "Mumbai"}).status_code
            == 200
        )

    @pytest.mark.parametrize("role", ["manager", "junior"])
    def test_nobody_below_a_partner_may(
        self, client: TestClient, auth_headers: dict, role: str
    ):
        """A manager commits the firm to one invoice at a time. This decides how
        every invoice is taxed and what address is printed on all of them."""
        headers = _member_headers(client, auth_headers, role)
        response = client.patch(f"{API}/auth/firm", headers=headers, json={"city": "Mumbai"})
        assert response.status_code == 403, response.text

    def test_a_manager_can_still_read_it(self, client: TestClient, auth_headers: dict):
        headers = _member_headers(client, auth_headers, "manager")
        assert client.get(f"{API}/auth/firm", headers=headers).status_code == 200

    def test_an_anonymous_caller_may_not(self, client: TestClient):
        assert client.patch(f"{API}/auth/firm", json={"city": "Mumbai"}).status_code == 401

    def test_a_portal_token_may_not(
        self, client: TestClient, client_id: str, firm_id: str
    ):
        """A client's magic link reaches ``/portal/*`` and nothing else.

        The firm profile is the sharpest case of that: it is the firm's own
        registration and address, and the token belongs to one of its clients.
        """
        from app.core.security import create_magic_link_token

        token = create_magic_link_token(
            client_id=uuid.UUID(client_id), firm_id=uuid.UUID(firm_id)
        )
        response = client.patch(
            f"{API}/auth/firm",
            headers={"Authorization": f"Bearer {token}"},
            json={"city": "Mumbai"},
        )
        assert response.status_code in (401, 403), response.text


class TestTheFirmsOwnPanAndGstin:
    """The same pair check every client record gets. A firm mistypes too."""

    def test_a_gstin_contradicting_the_stored_pan_is_refused(
        self, client: TestClient, auth_headers: dict
    ):
        """The registration fixture's PAN is AAACS1234F; this GSTIN wraps another."""
        response = client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"gstin": "27AAPFU0939F1ZV"}
        )
        assert response.status_code == 422, response.text
        assert "belongs to somebody else" in response.text

    def test_moving_both_together_is_allowed(self, client: TestClient, auth_headers: dict):
        response = client.patch(
            f"{API}/auth/firm",
            headers=auth_headers,
            json={"pan": "AAPFU0939F", "gstin": "27AAPFU0939F1ZV"},
        )
        assert response.status_code == 200, response.text

    def test_a_gstin_matching_the_stored_pan_is_accepted(
        self, client: TestClient, auth_headers: dict
    ):
        response = client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"gstin": FIRM_GSTIN_MH}
        )
        assert response.status_code == 200, response.text


class TestTheRestOfTheProfile:
    def test_the_name_and_contact_details_can_be_corrected(
        self, client: TestClient, auth_headers: dict
    ):
        response = client.patch(
            f"{API}/auth/firm",
            headers=auth_headers,
            json={
                "name": "Sharma & Co LLP",
                "email": "Accounts@Sharma-CA.in",
                "phone": "+912025551234",
                "icai_registration_number": "012345W",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "Sharma & Co LLP"
        assert body["phone"] == "+912025551234"
        assert body["icai_registration_number"] == "012345W"

    def test_the_contact_email_is_stored_folded(
        self, client: TestClient, auth_headers: dict
    ):
        """As registration already does, so one firm has one spelling of it."""
        response = client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"email": "Accounts@Sharma-CA.in"}
        )
        assert response.json()["email"] == "accounts@sharma-ca.in"

    def test_an_empty_patch_changes_nothing(self, client: TestClient, auth_headers: dict):
        before = client.get(f"{API}/auth/firm", headers=auth_headers).json()
        response = client.patch(f"{API}/auth/firm", headers=auth_headers, json={})
        assert response.status_code == 200, response.text
        assert response.json() == before

    def test_a_field_left_out_is_left_alone(self, client: TestClient, auth_headers: dict):
        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"city": "Mumbai"})
        response = client.patch(f"{API}/auth/firm", headers=auth_headers, json={"phone": "+912011112222"})
        assert response.json()["city"] == "Mumbai"

    def test_a_name_too_short_to_be_one_is_refused(
        self, client: TestClient, auth_headers: dict
    ):
        response = client.patch(f"{API}/auth/firm", headers=auth_headers, json={"name": "A"})
        assert response.status_code == 422, response.text

    def test_an_address_that_is_not_one_is_refused(
        self, client: TestClient, auth_headers: dict
    ):
        response = client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"email": "not-an-address"}
        )
        assert response.status_code == 422, response.text


class TestTheEditIsOnTheRecord:
    """Firm particulars decide the tax on every invoice, so who changed them
    and to what belongs in the trail as much as any client edit does."""

    def _entries(self, client: TestClient, auth_headers: dict) -> list[dict]:
        response = client.get(f"{API}/audit", headers=auth_headers, params={"limit": 100})
        assert response.status_code == 200, response.text
        return response.json()["items"]

    def test_the_change_is_recorded(self, client: TestClient, auth_headers: dict):
        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"gstin": FIRM_GSTIN_MH})
        assert any(
            entry["action"] == "firm.update" for entry in self._entries(client, auth_headers)
        )

    def _firm_update(self, client: TestClient, auth_headers: dict) -> dict:
        return next(
            entry
            for entry in self._entries(client, auth_headers)
            if entry["action"] == "firm.update"
        )

    def test_it_records_the_before_and_after(self, client: TestClient, auth_headers: dict):
        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"city": "Mumbai"})
        changes = self._firm_update(client, auth_headers)["changes"]
        assert changes["before"]["city"] == "Pune"
        assert changes["after"]["city"] == "Mumbai"

    def test_a_field_that_did_not_move_is_not_recorded(
        self, client: TestClient, auth_headers: dict
    ):
        """Re-sending what is already stored is not a change to the firm."""
        client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"city": "Pune", "phone": "+912011112222"}
        )
        changes = self._firm_update(client, auth_headers)["changes"]
        assert "city" not in changes["after"]
        assert changes["after"]["phone"] == "+912011112222"

    def test_it_names_who_made_it(self, client: TestClient, auth_headers: dict):
        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"city": "Mumbai"})
        entry = self._firm_update(client, auth_headers)
        assert FIRM_REGISTRATION["owner_full_name"] in entry["actor_label"]

    def test_the_stored_email_is_recorded_rather_than_the_one_sent(
        self, client: TestClient, auth_headers: dict
    ):
        """The trail is the record of what the firm holds, not of what was typed.

        The address is folded on the way in, so diffing the payload would log
        a value the row never held — the same reason ``audit.snapshot`` reads
        both sides off the record everywhere else.
        """
        client.patch(
            f"{API}/auth/firm", headers=auth_headers, json={"email": "Accounts@Sharma-CA.in"}
        )
        changes = self._firm_update(client, auth_headers)["changes"]
        assert changes["after"]["email"] == "accounts@sharma-ca.in"


class TestOneFirmsEditReachesOnlyItsOwn:
    def test_another_firms_profile_is_untouched(self, client: TestClient, auth_headers: dict):
        other = client.post(
            f"{API}/auth/register",
            json={
                **FIRM_REGISTRATION,
                "firm_name": "Iyer & Co",
                "firm_email": "office@iyer-ca.in",
                "owner_email": "raghav@iyer-ca.in",
                "pan": None,
                "city": "Chennai",
            },
        )
        assert other.status_code == 201, other.text
        other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

        client.patch(f"{API}/auth/firm", headers=auth_headers, json={"city": "Mumbai"})

        assert (
            client.get(f"{API}/auth/firm", headers=other_headers).json()["city"] == "Chennai"
        )
