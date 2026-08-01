"""Document intake: upload, categorisation, checklists and the chase list.

Uploads go to a throwaway storage directory configured in ``conftest``.
"""

from __future__ import annotations

import io

import pytest

from app.models.base import DocumentCategory
from app.services import documents as document_service
from app.services import storage
from tests.conftest import first_item_of_type

PDF_BYTES = b"%PDF-1.4\n% a pretend bank statement\n"


def upload(client, headers, *, client_id, filename="bank_statement.pdf", **form):
    body = {"client_id": client_id, **{k: str(v) for k, v in form.items() if v is not None}}
    return client.post(
        "/api/v1/documents/upload",
        files={"file": (filename, io.BytesIO(PDF_BYTES), "application/pdf")},
        data=body,
        headers=headers,
    )


class TestSafeFilename:
    @pytest.mark.parametrize(
        ("supplied", "expected"),
        [
            ("bank.pdf", "bank.pdf"),
            ("../../../etc/passwd", "passwd"),
            ("/absolute/path/form16.pdf", "form16.pdf"),
            ("a b c.pdf", "a_b_c.pdf"),
            ("", "upload"),
            ("...", "upload"),
        ],
    )
    def test_filenames_are_reduced_to_a_safe_basename(self, supplied, expected):
        assert storage.safe_filename(supplied) == expected

    def test_a_stored_path_cannot_escape_the_storage_root(self):
        with pytest.raises(ValueError, match="escapes the storage root"):
            storage.resolve_stored("../../etc/passwd")


class TestUpload:
    def test_uploads_and_categorises_a_document(self, client, auth_headers, client_id):
        response = upload(client, auth_headers, client_id=client_id)
        assert response.status_code == 201, response.text

        document = response.json()["document"]
        assert document["original_filename"] == "bank_statement.pdf"
        assert document["size_bytes"] == len(PDF_BYTES)
        assert document["status"] == "processed"
        # No API key in tests, so the filename heuristic decides.
        assert document["category"] == DocumentCategory.BANK_STATEMENT.value
        assert document["uploaded_via_portal"] is False

    def test_an_explicit_category_overrides_the_classifier(
        self, client, auth_headers, client_id
    ):
        response = upload(
            client,
            auth_headers,
            client_id=client_id,
            filename="bank_statement.pdf",
            category="form_16",
        )
        document = response.json()["document"]
        assert document["category"] == "form_16"
        assert document["is_category_confirmed"] is True
        assert document["category_confidence"] is None

    def test_the_file_actually_lands_on_disk(self, client, auth_headers, client_id, db):
        from app.models.document import Document

        upload(client, auth_headers, client_id=client_id)
        stored = db.query(Document).one()
        assert storage.open_stored(stored.storage_path) == PDF_BYTES

    def test_identifiers_are_extracted_from_text_uploads(
        self, client, auth_headers, client_id
    ):
        content = b"Invoice\nPAN: ABCDE1234F\nGSTIN: 27AABCN2345P1Z5\n"
        response = client.post(
            "/api/v1/documents/upload",
            files={"file": ("sales.csv", io.BytesIO(content), "text/csv")},
            data={"client_id": client_id},
            headers=auth_headers,
        )
        extracted = response.json()["document"]["extracted_data"]
        assert extracted["pan"] == "ABCDE1234F"
        assert extracted["gstin"] == "27AABCN2345P1Z5"

    def test_an_oversized_upload_is_refused(
        self, client, auth_headers, client_id, monkeypatch
    ):
        monkeypatch.setattr("app.config.settings.max_upload_bytes", 10)
        response = upload(client, auth_headers, client_id=client_id)
        assert response.status_code == 413
        assert "upload limit" in response.json()["detail"]

    def test_an_unsupported_file_type_is_refused(self, client, auth_headers, client_id):
        response = client.post(
            "/api/v1/documents/upload",
            files={"file": ("run.sh", io.BytesIO(b"#!/bin/sh"), "application/x-sh")},
            data={"client_id": client_id},
            headers=auth_headers,
        )
        assert response.status_code == 415

    def test_uploading_for_another_firms_client_is_a_404(
        self, client, auth_headers, client_id
    ):
        import uuid

        response = upload(client, auth_headers, client_id=str(uuid.uuid4()))
        assert response.status_code == 404

    def test_a_filing_belonging_to_another_client_is_rejected(
        self, client, auth_headers, client_id
    ):
        from tests.conftest import make_client_payload

        other = client.post(
            "/api/v1/clients",
            json=make_client_payload(name="Other Ltd", pan="AAACO1234K", gstin=None,
                                     gst_registered=False),
            headers=auth_headers,
        ).json()["client"]
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")

        response = upload(
            client,
            auth_headers,
            client_id=other["id"],
            compliance_item_id=item["id"],
        )
        assert response.status_code == 400
        assert "different client" in response.json()["detail"]


class TestChecklist:
    def test_a_filing_starts_with_everything_outstanding(
        self, client, auth_headers, client_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        response = client.get(
            f"/api/v1/documents/checklist/{item['id']}", headers=auth_headers
        )
        assert response.status_code == 200

        checklist = response.json()
        assert checklist["is_complete"] is False
        # GSTR-3B needs sales invoices, purchase invoices and a bank statement.
        assert set(checklist["missing"]) == {
            "sales_invoice",
            "purchase_invoice",
            "bank_statement",
        }
        assert all(req["satisfied"] is False for req in checklist["requirements"])

    def test_an_upload_satisfies_its_requirement(self, client, auth_headers, client_id):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        response = upload(
            client,
            auth_headers,
            client_id=client_id,
            compliance_item_id=item["id"],
            requirement="bank_statement",
        )
        checklist = response.json()["checklist"]
        assert "bank_statement" not in checklist["missing"]
        assert set(checklist["missing"]) == {"sales_invoice", "purchase_invoice"}

    def test_a_requirement_with_no_category_is_satisfied_explicitly(
        self, client, auth_headers, client_id
    ):
        """"export_invoices" is not a DocumentCategory, so it needs the marker."""
        item = first_item_of_type(client, auth_headers, "GSTR1_MONTHLY")
        response = upload(
            client,
            auth_headers,
            client_id=client_id,
            filename="exports.pdf",
            compliance_item_id=item["id"],
            requirement="export_invoices",
        )
        document = response.json()["document"]
        assert document["satisfies_requirements"] == ["export_invoices"]
        assert "export_invoices" not in response.json()["checklist"]["missing"]

    def test_a_matching_category_satisfies_without_a_requirement_marker(
        self, client, auth_headers, client_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        response = upload(
            client,
            auth_headers,
            client_id=client_id,
            filename="my_bank_statement_july.pdf",
            compliance_item_id=item["id"],
        )
        assert "bank_statement" not in response.json()["checklist"]["missing"]

    def test_completing_every_requirement_marks_the_checklist_complete(
        self, client, auth_headers, client_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        for requirement in ("sales_invoice", "purchase_invoice", "bank_statement"):
            response = upload(
                client,
                auth_headers,
                client_id=client_id,
                filename=f"{requirement}.pdf",
                compliance_item_id=item["id"],
                requirement=requirement,
            )
        assert response.json()["checklist"]["is_complete"] is True


class TestOutstanding:
    def test_lists_filings_that_are_still_missing_documents(
        self, client, auth_headers, client_id
    ):
        response = client.get("/api/v1/documents/outstanding", headers=auth_headers)
        assert response.status_code == 200

        body = response.json()
        assert body["total_items"] > 0
        assert body["total_missing"] > 0
        assert all(not c["is_complete"] for c in body["checklists"])

    def test_a_completed_filing_drops_off_the_chase_list(
        self, client, auth_headers, client_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        for requirement in ("sales_invoice", "purchase_invoice", "bank_statement"):
            upload(
                client,
                auth_headers,
                client_id=client_id,
                filename=f"{requirement}.pdf",
                compliance_item_id=item["id"],
                requirement=requirement,
            )

        body = client.get(
            "/api/v1/documents/outstanding",
            params={"from_date": "2020-01-01", "to_date": "2035-12-31"},
            headers=auth_headers,
        ).json()
        assert item["id"] not in [c["compliance_item_id"] for c in body["checklists"]]

    def test_an_inverted_window_is_rejected(self, client, auth_headers):
        response = client.get(
            "/api/v1/documents/outstanding",
            params={"from_date": "2026-06-01", "to_date": "2026-01-01"},
            headers=auth_headers,
        )
        assert response.status_code == 422


class TestDocumentCrud:
    def test_lists_and_filters_documents(self, client, auth_headers, client_id):
        upload(client, auth_headers, client_id=client_id, filename="form16.pdf")
        upload(client, auth_headers, client_id=client_id, filename="bank.pdf")

        everything = client.get("/api/v1/documents", headers=auth_headers).json()
        assert everything["total"] == 2

        filtered = client.get(
            "/api/v1/documents", params={"category": "form_16"}, headers=auth_headers
        ).json()
        assert filtered["total"] == 1
        assert filtered["items"][0]["category"] == "form_16"

    def test_correcting_a_category_confirms_it(self, client, auth_headers, client_id):
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        response = client.patch(
            f"/api/v1/documents/{document['id']}",
            json={"category": "form_26as"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["category"] == "form_26as"
        assert response.json()["is_category_confirmed"] is True
        assert response.json()["category_confidence"] is None

    def test_downloads_the_original_bytes(self, client, auth_headers, client_id):
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        response = client.get(
            f"/api/v1/documents/{document['id']}/download", headers=auth_headers
        )
        assert response.status_code == 200
        assert response.content == PDF_BYTES
        assert "bank_statement.pdf" in response.headers["content-disposition"]

    def test_deleting_removes_the_row_and_the_file(
        self, client, auth_headers, client_id, db
    ):
        from app.models.document import Document

        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        path = db.query(Document).one().storage_path

        assert client.delete(
            f"/api/v1/documents/{document['id']}", headers=auth_headers
        ).status_code == 204
        assert db.query(Document).count() == 0
        assert not storage.resolve_stored(path).exists()

    def test_documents_require_authentication(self, client):
        assert client.get("/api/v1/documents").status_code == 401


class TestRequirementLabels:
    @pytest.mark.parametrize(
        ("requirement", "expected"),
        [
            ("bank_statement", "Bank statement"),
            ("form_16", "Form 16"),
            ("export_invoices", "Export invoices"),
            ("credit_debit_notes", "Credit / debit notes"),
        ],
    )
    def test_requirements_get_readable_labels(self, requirement, expected):
        assert document_service.requirement_label(requirement) == expected

    def test_category_requirements_are_recognised(self):
        assert document_service.is_category_requirement("bank_statement") is True
        assert document_service.is_category_requirement("export_invoices") is False
