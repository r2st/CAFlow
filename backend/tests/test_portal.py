"""The client portal: magic-link issuing, scoping, revocation and uploads."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.config import settings
from app.core.security import create_access_token, create_magic_link_token
from app.models.client import Client
from tests.conftest import first_item_of_type, make_client_payload

PDF_BYTES = b"%PDF-1.4\n% ledger\n"


def issue_link(client, auth_headers, client_id, **payload):
    return client.post(
        f"/api/v1/clients/{client_id}/portal-link", json=payload, headers=auth_headers
    )


@pytest.fixture
def portal_headers(client, auth_headers, client_id) -> dict[str, str]:
    token = issue_link(client, auth_headers, client_id).json()["token"]
    return {"Authorization": f"Bearer {token}"}


class TestIssuingLinks:
    def test_issues_a_link_addressed_to_the_client(self, client, auth_headers, client_id):
        response = issue_link(client, auth_headers, client_id)
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["url"].startswith(settings.portal_base_url)
        assert f"token={body['token']}" in body["url"]
        assert body["delivered_to"] == "accounts@nimbustextiles.in"
        assert datetime.fromisoformat(body["expires_at"]) > datetime.now(UTC)

    def test_an_override_address_is_honoured(self, client, auth_headers, client_id):
        body = issue_link(
            client, auth_headers, client_id, send_to="director@nimbustextiles.in"
        ).json()
        assert body["delivered_to"] == "director@nimbustextiles.in"

    def test_the_token_is_never_written_to_the_audit_trail(
        self, client, auth_headers, client_id, db
    ):
        from app.models.audit import AuditLog

        token = issue_link(client, auth_headers, client_id).json()["token"]
        entries = db.query(AuditLog).filter(AuditLog.action == "portal.link_issued").all()
        assert len(entries) == 1
        assert token not in str(entries[0].changes)
        assert token not in (entries[0].summary or "")

    def test_a_disabled_portal_refuses_to_mint_links(self, client, auth_headers, client_id):
        client.post(
            f"/api/v1/clients/{client_id}/portal-access/disable", headers=auth_headers
        )
        response = issue_link(client, auth_headers, client_id)
        assert response.status_code == 400
        assert "disabled" in response.json()["detail"]

    def test_an_unknown_client_is_a_404(self, client, auth_headers):
        assert issue_link(client, auth_headers, str(uuid.uuid4())).status_code == 404

    def test_a_junior_cannot_issue_links(self, client, auth_headers, client_id):
        client.post(
            "/api/v1/auth/practitioners",
            json={
                "full_name": "Junior Jain",
                "email": "junior@sharma-ca.in",
                "password": "another-long-password",
                "role": "junior",
            },
            headers=auth_headers,
        )
        junior_token = client.post(
            "/api/v1/auth/login",
            json={"email": "junior@sharma-ca.in", "password": "another-long-password"},
        ).json()["access_token"]

        response = issue_link(
            client, {"Authorization": f"Bearer {junior_token}"}, client_id
        )
        assert response.status_code == 403


class TestPortalAccess:
    def test_the_client_sees_their_filings(self, client, portal_headers, client_id):
        response = client.get("/api/v1/portal/me", headers=portal_headers)
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["client_id"] == client_id
        assert body["client_name"] == "Nimbus Textiles Pvt Ltd"
        assert body["firm_name"] == "Sharma & Associates"
        assert body["summary"]["total"] > 0
        assert len(body["filings"]) == body["summary"]["total"]

    def test_the_overview_exposes_no_fee_information(self, client, portal_headers):
        body = client.get("/api/v1/portal/me", headers=portal_headers).json()
        assert "fee_paise" not in str(body)
        for filing in body["filings"]:
            assert "fee_paise" not in filing

    def test_outstanding_documents_are_surfaced_to_the_client(
        self, client, portal_headers
    ):
        body = client.get("/api/v1/portal/me", headers=portal_headers).json()
        assert body["summary"]["documents_outstanding"] > 0
        assert body["checklists"]
        assert all(not c["is_complete"] for c in body["checklists"])

    def test_visiting_records_the_last_seen_time(
        self, client, portal_headers, client_id, db
    ):
        assert db.get(Client, uuid.UUID(client_id)).portal_last_seen_at is None
        client.get("/api/v1/portal/me", headers=portal_headers)
        db.expire_all()
        assert db.get(Client, uuid.UUID(client_id)).portal_last_seen_at is not None


class TestPortalScoping:
    """A magic link must never widen into practitioner access."""

    def test_no_token_is_rejected(self, client):
        assert client.get("/api/v1/portal/me").status_code == 401

    def test_a_practitioner_token_cannot_use_the_portal(self, client, auth_headers):
        response = client.get("/api/v1/portal/me", headers=auth_headers)
        assert response.status_code == 401

    def test_a_portal_token_cannot_reach_practitioner_endpoints(
        self, client, portal_headers
    ):
        for path in ("/api/v1/clients", "/api/v1/compliance/dashboard", "/api/v1/invoices"):
            assert client.get(path, headers=portal_headers).status_code == 401

    def test_a_token_for_another_firms_client_is_rejected(self, client, client_id):
        forged = create_magic_link_token(client_id=client_id, firm_id=uuid.uuid4())
        response = client.get(
            "/api/v1/portal/me", headers={"Authorization": f"Bearer {forged}"}
        )
        assert response.status_code == 401

    def test_a_token_for_an_unknown_client_is_rejected(self, client, firm_id):
        stray = create_magic_link_token(client_id=uuid.uuid4(), firm_id=firm_id)
        response = client.get(
            "/api/v1/portal/me", headers={"Authorization": f"Bearer {stray}"}
        )
        assert response.status_code == 401

    def test_an_access_token_shaped_as_a_portal_token_is_rejected(
        self, client, client_id, firm_id
    ):
        """Type confusion: an access token must not pass the magic-link check."""
        access = create_access_token(
            practitioner_id=client_id, firm_id=firm_id, role="owner"
        )
        response = client.get(
            "/api/v1/portal/me", headers={"Authorization": f"Bearer {access}"}
        )
        assert response.status_code == 401

    def test_an_expired_link_is_rejected(self, client, client_id, firm_id):
        expired = jwt.encode(
            {
                "sub": str(client_id),
                "firm_id": str(firm_id),
                "type": "magic_link",
                "iat": int((datetime.now(UTC) - timedelta(days=30)).timestamp()),
                "exp": int((datetime.now(UTC) - timedelta(days=1)).timestamp()),
            },
            settings.secret_key,
            algorithm=settings.jwt_algorithm,
        )
        response = client.get(
            "/api/v1/portal/me", headers={"Authorization": f"Bearer {expired}"}
        )
        assert response.status_code == 401

    def test_a_token_signed_with_the_wrong_key_is_rejected(self, client, client_id, firm_id):
        forged = jwt.encode(
            {
                "sub": str(client_id),
                "firm_id": str(firm_id),
                "type": "magic_link",
                "exp": int((datetime.now(UTC) + timedelta(days=1)).timestamp()),
            },
            "not-the-real-signing-key",
            algorithm="HS256",
        )
        response = client.get(
            "/api/v1/portal/me", headers={"Authorization": f"Bearer {forged}"}
        )
        assert response.status_code == 401

    def test_a_deactivated_client_loses_portal_access(
        self, client, auth_headers, portal_headers, client_id
    ):
        client.delete(f"/api/v1/clients/{client_id}", headers=auth_headers)
        assert client.get("/api/v1/portal/me", headers=portal_headers).status_code == 401


class TestRevocation:
    def test_revoking_kills_links_already_issued(
        self, client, auth_headers, portal_headers, client_id
    ):
        assert client.get("/api/v1/portal/me", headers=portal_headers).status_code == 200

        response = client.post(
            f"/api/v1/clients/{client_id}/portal-access/revoke", headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json()["portal_token_valid_from"] is not None

        assert client.get("/api/v1/portal/me", headers=portal_headers).status_code == 401

    def test_a_link_issued_after_revocation_still_works(
        self, client, auth_headers, portal_headers, client_id
    ):
        client.post(
            f"/api/v1/clients/{client_id}/portal-access/revoke", headers=auth_headers
        )
        fresh = issue_link(client, auth_headers, client_id).json()["token"]
        response = client.get(
            "/api/v1/portal/me", headers={"Authorization": f"Bearer {fresh}"}
        )
        assert response.status_code == 200

    def test_disabling_the_portal_blocks_every_link(
        self, client, auth_headers, portal_headers, client_id
    ):
        client.post(
            f"/api/v1/clients/{client_id}/portal-access/disable", headers=auth_headers
        )
        assert client.get("/api/v1/portal/me", headers=portal_headers).status_code == 401

    def test_re_enabling_restores_access_for_new_links(
        self, client, auth_headers, client_id
    ):
        client.post(
            f"/api/v1/clients/{client_id}/portal-access/disable", headers=auth_headers
        )
        client.post(
            f"/api/v1/clients/{client_id}/portal-access/enable", headers=auth_headers
        )
        token = issue_link(client, auth_headers, client_id).json()["token"]
        response = client.get(
            "/api/v1/portal/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200

    def test_access_state_is_readable(self, client, auth_headers, client_id):
        response = client.get(
            f"/api/v1/clients/{client_id}/portal-access", headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json() == {
            "client_id": client_id,
            "portal_enabled": True,
            "portal_token_valid_from": None,
            "portal_last_seen_at": None,
        }


class TestPortalUploads:
    def test_a_client_can_upload_against_a_checklist_row(
        self, client, auth_headers, portal_headers, client_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        response = client.post(
            "/api/v1/portal/documents",
            files={"file": ("bank.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
            data={"compliance_item_id": item["id"], "requirement": "bank_statement"},
            headers=portal_headers,
        )
        assert response.status_code == 201, response.text
        assert response.json()["uploaded_via_portal"] is True
        assert response.json()["category"] == "bank_statement"

    def test_the_upload_clears_the_requirement_for_the_firm_too(
        self, client, auth_headers, portal_headers, client_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        client.post(
            "/api/v1/portal/documents",
            files={"file": ("bank.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
            data={"compliance_item_id": item["id"], "requirement": "bank_statement"},
            headers=portal_headers,
        )
        checklist = client.get(
            f"/api/v1/documents/checklist/{item['id']}", headers=auth_headers
        ).json()
        assert "bank_statement" not in checklist["missing"]

    def test_uploading_against_another_clients_filing_is_a_404(
        self, client, auth_headers, portal_headers
    ):
        other = client.post(
            "/api/v1/clients",
            json=make_client_payload(
                name="Other Ltd", pan="AAACO1234K", gstin=None, gst_registered=False
            ),
            headers=auth_headers,
        ).json()["client"]
        stray_item = client.get(
            "/api/v1/compliance/calendar",
            params={"client_id": other["id"], "from_date": "2020-01-01",
                    "to_date": "2035-12-31"},
            headers=auth_headers,
        ).json()["items"][0]

        response = client.post(
            "/api/v1/portal/documents",
            files={"file": ("x.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
            data={"compliance_item_id": stray_item["id"]},
            headers=portal_headers,
        )
        assert response.status_code == 404

    def test_the_portal_upload_is_attributed_in_the_audit_trail(
        self, client, portal_headers, db
    ):
        from app.models.audit import AuditLog

        client.post(
            "/api/v1/portal/documents",
            files={"file": ("bank.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
            headers=portal_headers,
        )
        entry = (
            db.query(AuditLog).filter(AuditLog.action == "portal.document_upload").one()
        )
        assert entry.actor_practitioner_id is None
        assert "client portal" in entry.actor_label


class TestPortalDownloads:
    def test_a_client_can_download_their_own_upload(self, client, portal_headers):
        document = client.post(
            "/api/v1/portal/documents",
            files={"file": ("bank.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
            headers=portal_headers,
        ).json()

        response = client.get(
            f"/api/v1/portal/documents/{document['id']}/download", headers=portal_headers
        )
        assert response.status_code == 200
        assert response.content == PDF_BYTES

    def test_an_unshared_firm_document_is_not_downloadable(
        self, client, auth_headers, portal_headers, client_id
    ):
        document = client.post(
            "/api/v1/documents/upload",
            files={"file": ("workings.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
            data={"client_id": client_id},
            headers=auth_headers,
        ).json()["document"]

        response = client.get(
            f"/api/v1/portal/documents/{document['id']}/download", headers=portal_headers
        )
        assert response.status_code == 403

    def test_a_shared_document_becomes_downloadable(
        self, client, auth_headers, portal_headers, client_id
    ):
        document = client.post(
            "/api/v1/documents/upload",
            files={"file": ("computation.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
            data={"client_id": client_id, "share_with_client": "true"},
            headers=auth_headers,
        ).json()["document"]
        assert document["is_shared_with_client"] is True

        response = client.get(
            f"/api/v1/portal/documents/{document['id']}/download", headers=portal_headers
        )
        assert response.status_code == 200

    def test_shared_documents_appear_in_the_overview(
        self, client, auth_headers, portal_headers, client_id
    ):
        client.post(
            "/api/v1/documents/upload",
            files={"file": ("computation.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
            data={"client_id": client_id, "share_with_client": "true"},
            headers=auth_headers,
        )
        body = client.get("/api/v1/portal/me", headers=portal_headers).json()
        assert [d["original_filename"] for d in body["shared_documents"]] == [
            "computation.pdf"
        ]

    def test_another_clients_document_is_a_404(
        self, client, auth_headers, portal_headers
    ):
        other = client.post(
            "/api/v1/clients",
            json=make_client_payload(
                name="Other Ltd", pan="AAACO1234K", gstin=None, gst_registered=False
            ),
            headers=auth_headers,
        ).json()["client"]
        document = client.post(
            "/api/v1/documents/upload",
            files={"file": ("theirs.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
            data={"client_id": other["id"], "share_with_client": "true"},
            headers=auth_headers,
        ).json()["document"]

        response = client.get(
            f"/api/v1/portal/documents/{document['id']}/download", headers=portal_headers
        )
        assert response.status_code == 404
