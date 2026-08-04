"""Document intake: upload, categorisation, checklists and the chase list.

Uploads go to a throwaway storage directory configured in ``conftest``.
"""

from __future__ import annotations

import io
import logging

import pytest
from sqlalchemy.exc import OperationalError

from app.models.base import DocumentCategory
from app.models.document import Document
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

    def test_a_zero_byte_upload_is_refused(self, client, auth_headers, client_id):
        """An empty file is not the paperwork, and storing one says it arrived."""
        response = client.post(
            "/api/v1/documents/upload",
            files={"file": ("bank_statement.pdf", io.BytesIO(b""), "application/pdf")},
            data={"client_id": client_id},
            headers=auth_headers,
        )
        assert response.status_code == 415
        assert "empty" in response.json()["detail"]

        listed = client.get(
            "/api/v1/documents", params={"client_id": client_id}, headers=auth_headers
        ).json()["items"]
        assert listed == []

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

    def test_a_corrected_category_does_not_lose_the_row_that_was_answered(
        self, client, auth_headers, client_id
    ):
        """Answering a row and fixing the category are two separate statements.

        The requirement used to be recorded from the requirement key alone,
        before the category was settled: any key that happened to name a
        category was left to the category to cover. So an uploader who did both
        — attached a file against the "sales invoices" row and corrected its
        category to bank statement — answered neither row that the file was
        filed against, the filing stayed on the chase list, and the client went
        on being emailed for paperwork the firm was already holding.
        """
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        response = upload(
            client,
            auth_headers,
            client_id=client_id,
            filename="ledger.pdf",
            compliance_item_id=item["id"],
            requirement="sales_invoice",
            category="bank_statement",
        )
        document = response.json()["document"]
        assert document["category"] == "bank_statement"
        assert document["satisfies_requirements"] == ["sales_invoice"]

        missing = response.json()["checklist"]["missing"]
        assert "sales_invoice" not in missing
        assert "bank_statement" not in missing

    def test_a_category_that_already_answers_the_row_is_not_recorded_twice(
        self, client, auth_headers, client_id
    ):
        """The marker exists for rows a category cannot cover, and only those."""
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        response = upload(
            client,
            auth_headers,
            client_id=client_id,
            compliance_item_id=item["id"],
            requirement="bank_statement",
            category="bank_statement",
        )
        document = response.json()["document"]
        assert document["satisfies_requirements"] == []
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


class TestDeletingADocumentCannotLoseTheFileAndKeepTheRow:
    """Two stores, one of which does not roll back.

    A delete removes a database row and a file on a volume. If the file goes
    first and the transaction then fails — a dropped connection, a statement
    timeout, a deadlock — the row survives describing bytes that no longer
    exist: still listed, still offered to the client in the portal, and
    downloading 410 for ever with nothing to restore. Committing the record
    first turns that into an orphaned file, which costs disk and can be swept.
    """

    def stored_path(self, db) -> str:
        return db.query(Document).one().storage_path

    def drop_the_connection(self, monkeypatch, db) -> None:
        """Make the commit fail the way a database restart makes it fail."""

        def dropped(*args, **kwargs):
            raise OperationalError("DELETE FROM documents", {}, Exception("server closed"))

        monkeypatch.setattr(db, "commit", dropped)

    def test_a_failed_commit_leaves_the_file_where_the_row_still_points(
        self, client, auth_headers, client_id, db, monkeypatch
    ):
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        path = self.stored_path(db)
        self.drop_the_connection(monkeypatch, db)

        response = client.delete(
            f"/api/v1/documents/{document['id']}", headers=auth_headers
        )

        assert response.status_code == 503
        assert storage.resolve_stored(path).read_bytes() == PDF_BYTES

    def test_a_failed_commit_keeps_the_row(
        self, client, auth_headers, client_id, db, monkeypatch
    ):
        """The other half of the pair: neither store may move without the other."""
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        self.drop_the_connection(monkeypatch, db)

        client.delete(f"/api/v1/documents/{document['id']}", headers=auth_headers)

        db.rollback()
        assert db.query(Document).count() == 1

    def test_a_file_that_cannot_be_removed_does_not_fail_the_delete(
        self, client, auth_headers, client_id, db, monkeypatch
    ):
        """The record is gone, which is what was asked for.

        Reporting a failure here would be a lie the caller cannot act on: a
        second DELETE only finds a 404, and the row is not coming back.
        """
        document = upload(client, auth_headers, client_id=client_id).json()["document"]

        def read_only_volume(storage_path):
            raise PermissionError(f"Read-only file system: {storage_path}")

        monkeypatch.setattr(storage, "delete_stored", read_only_volume)

        response = client.delete(
            f"/api/v1/documents/{document['id']}", headers=auth_headers
        )

        assert response.status_code == 204
        assert db.query(Document).count() == 0

    def test_the_orphan_it_leaves_behind_is_logged(
        self, client, auth_headers, client_id, db, monkeypatch, caplog
    ):
        """Costing disk silently is how a volume fills up unexplained."""
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        path = self.stored_path(db)

        monkeypatch.setattr(
            storage,
            "delete_stored",
            lambda storage_path: (_ for _ in ()).throw(PermissionError("read-only")),
        )

        with caplog.at_level(logging.WARNING, logger="app.api.routes.documents"):
            client.delete(f"/api/v1/documents/{document['id']}", headers=auth_headers)

        assert path in caplog.text
        assert str(document["id"]) in caplog.text

    def test_the_bytes_are_gone_once_the_delete_has_committed(
        self, client, auth_headers, client_id, db
    ):
        """Ordering is the fix, not skipping the unlink."""
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        path = self.stored_path(db)

        client.delete(f"/api/v1/documents/{document['id']}", headers=auth_headers)

        assert not storage.resolve_stored(path).exists()


class TestRequirementLabels:
    @pytest.mark.parametrize(
        ("requirement", "expected"),
        [
            ("bank_statement", "Bank statement"),
            ("form_16", "Form 16"),
            ("export_invoices", "Export invoices"),
            ("credit_debit_notes", "Credit / debit notes"),
            # Statutory acronyms a client would recognise. Capitalising the key
            # would render these "Tds challan" and "Gst return" — in the portal
            # checklist and in the document-request email.
            ("tds_challan", "TDS challan"),
            ("gst_return", "GST return"),
            ("form_26as", "Form 26AS"),
            ("ais_tis", "AIS / TIS"),
            ("pan_card", "PAN card"),
        ],
    )
    def test_requirements_get_readable_labels(self, requirement, expected):
        assert document_service.requirement_label(requirement) == expected

    def test_every_seeded_requirement_has_a_deliberate_label(self):
        """No statutory requirement should reach a client as a mangled key."""
        from app.seeds.compliance_types import COMPLIANCE_TYPE_SEEDS

        seeded = {
            requirement
            for seed in COMPLIANCE_TYPE_SEEDS
            for requirement in seed.get("required_documents", [])
        }
        assert seeded
        mangled = {
            requirement
            for requirement in seeded
            # A key of two or three letters before the first underscore is an
            # acronym (tds, gst, ais); title case would be wrong for all of them.
            if len(requirement.split("_")[0]) <= 3
            and requirement not in document_service.REQUIREMENT_LABELS
        }
        assert mangled == set(), f"needs a spelled-out label: {sorted(mangled)}"

    def test_category_requirements_are_recognised(self):
        assert document_service.is_category_requirement("bank_statement") is True
        assert document_service.is_category_requirement("export_invoices") is False


class TestRequirementKeys:
    """A requirement key is this system's vocabulary, not free text.

    It arrives as a multipart form field — the one inbound string that never
    passes through a Pydantic model — and lands in a JSON column that the
    portal, which anyone holding a magic link can reach, also writes.
    """

    @pytest.mark.parametrize(
        ("supplied", "expected"),
        [
            ("bank_statement", "bank_statement"),
            ("  bank_statement  ", "bank_statement"),
            ("Bank_Statement", "bank_statement"),
            ("form_26as", "form_26as"),
            # An empty field means "no requirement", the same as omitting it.
            ("", None),
            ("   ", None),
            (None, None),
        ],
    )
    def test_a_key_is_normalised_rather_than_taken_as_typed(self, supplied, expected):
        assert document_service.clean_requirement(supplied) == expected

    @pytest.mark.parametrize(
        "supplied",
        [
            "bank statement",  # spaces
            "bank-statement",  # hyphen
            "../../etc/passwd",
            "9_lives",  # must start with a letter
            "_leading",
            "<script>alert(1)</script>",
            "बैंक",  # not our vocabulary, however legitimate the script
        ],
    )
    def test_anything_not_shaped_like_a_key_is_refused(self, supplied):
        with pytest.raises(document_service.InvalidRequirement):
            document_service.clean_requirement(supplied)

    def test_a_key_longer_than_the_cap_is_refused(self):
        limit = document_service.MAX_REQUIREMENT_KEY_LENGTH
        assert document_service.clean_requirement("a" * limit) == "a" * limit
        with pytest.raises(document_service.InvalidRequirement, match="at most"):
            document_service.clean_requirement("a" * (limit + 1))

    def test_every_seeded_requirement_survives_the_check(self):
        """The guard must not refuse the vocabulary the seed itself uses."""
        from app.seeds.compliance_types import COMPLIANCE_TYPE_SEEDS

        seeded = {
            requirement
            for seed in COMPLIANCE_TYPE_SEEDS
            for requirement in seed.get("required_documents", [])
        }
        assert seeded
        for requirement in seeded:
            assert document_service.clean_requirement(requirement) == requirement

    def test_every_document_category_survives_the_check(self):
        for category in DocumentCategory:
            assert document_service.clean_requirement(category.value) == category.value


class TestRequirementValidationOnUpload:
    def test_free_text_is_refused_rather_than_stored(
        self, client, auth_headers, client_id
    ):
        response = upload(
            client, auth_headers, client_id=client_id, requirement="a" * 5000
        )
        assert response.status_code == 422, response.text
        assert "at most" in response.json()["detail"]

    def test_a_junk_key_names_what_a_key_looks_like(self, client, auth_headers, client_id):
        response = upload(
            client, auth_headers, client_id=client_id, requirement="not a key!"
        )
        assert response.status_code == 422
        assert "bank_statement" in response.json()["detail"]

    def test_a_miscased_key_still_satisfies_its_row(self, client, auth_headers, client_id):
        """Normalising is what keeps the fix from silently losing the upload."""
        item = first_item_of_type(client, auth_headers, "GSTR1_MONTHLY")
        response = upload(
            client,
            auth_headers,
            client_id=client_id,
            filename="exports.pdf",
            compliance_item_id=item["id"],
            requirement="Export_Invoices",
        )
        assert response.status_code == 201, response.text
        assert response.json()["document"]["satisfies_requirements"] == ["export_invoices"]
        assert "export_invoices" not in response.json()["checklist"]["missing"]

    def test_an_empty_requirement_field_falls_back_to_categorisation(
        self, client, auth_headers, client_id
    ):
        response = upload(
            client,
            auth_headers,
            client_id=client_id,
            filename="my_bank_statement.pdf",
            requirement="",
        )
        assert response.status_code == 201, response.text
        document = response.json()["document"]
        assert document["satisfies_requirements"] == []
        assert document["category"] == "bank_statement"


class TestRequirementValidationOnUpdate:
    def test_free_text_cannot_be_patched_into_the_column(
        self, client, auth_headers, client_id
    ):
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        response = client.patch(
            f"/api/v1/documents/{document['id']}",
            json={"satisfies_requirements": ["fine", "not a key"]},
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_more_keys_than_the_cap_is_refused(self, client, auth_headers, client_id):
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        over = document_service.MAX_SATISFIED_REQUIREMENTS + 1
        response = client.patch(
            f"/api/v1/documents/{document['id']}",
            json={"satisfies_requirements": [f"req_{n}" for n in range(over)]},
            headers=auth_headers,
        )
        assert response.status_code == 422
        assert str(document_service.MAX_SATISFIED_REQUIREMENTS) in response.json()["detail"]

    def test_keys_are_normalised_deduplicated_and_ordered(
        self, client, auth_headers, client_id
    ):
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        response = client.patch(
            f"/api/v1/documents/{document['id']}",
            json={"satisfies_requirements": ["Export_Invoices", "export_invoices", "", "ais_tis"]},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["satisfies_requirements"] == ["ais_tis", "export_invoices"]


class TestTheNameAndTypeAnUploadArrivesUnder:
    """Both come out of the multipart part's own headers, so both are exactly
    as long and as strange as the sender chose — and the portal upload that
    writes them is reachable by anyone holding a client's magic link.

    ``original_filename`` is a ``VARCHAR(512)`` and ``content_type`` a
    ``VARCHAR(128)``. SQLite ignores a declared width, so nothing here failed;
    PostgreSQL refuses the row outright, and it refuses it *after* the bytes
    have been written to the storage volume — a 500 for the sender and a file
    on disk with no row pointing at it.
    """

    def _upload(self, client, headers, client_id, *, filename, content_type, body=PDF_BYTES):
        return client.post(
            "/api/v1/documents/upload",
            files={"file": (filename, io.BytesIO(body), content_type)},
            data={"client_id": client_id},
            headers=headers,
        )

    def test_a_filename_longer_than_the_column_is_shortened_not_refused(
        self, client, auth_headers, client_id
    ):
        """The upload is a real document either way; losing it over the length
        of its own name would be the worse answer."""
        response = self._upload(
            client,
            auth_headers,
            client_id,
            filename="A" * 900 + ".pdf",
            content_type="application/pdf",
        )
        assert response.status_code == 201, response.text
        stored = response.json()["document"]["original_filename"]
        assert len(stored) <= document_service.MAX_ORIGINAL_FILENAME
        # The tail is kept, so the extension — the part a practitioner reads —
        # survives, exactly as ``storage.safe_filename`` keeps it.
        assert stored.endswith(".pdf")

    def test_a_content_type_padded_past_the_column_is_cut(
        self, client, auth_headers, client_id
    ):
        """The allow-list reads only the part before the semicolon, so a type
        with four hundred characters of parameters walks straight past it."""
        response = self._upload(
            client,
            auth_headers,
            client_id,
            filename="ledger.txt",
            content_type="text/plain; charset=" + "x" * 400,
            body=b"opening balance 1,00,000",
        )
        assert response.status_code == 201, response.text
        stored = response.json()["document"]["content_type"]
        assert len(stored) <= document_service.MAX_CONTENT_TYPE

    def test_a_filename_is_reduced_to_one_readable_line(self):
        """It is rendered in the portal, quoted in the audit trail and echoed
        in a download header, so the newlines and invisible characters
        ``sanitize_text`` keeps for a multiline note have no place in it.

        Asserted against the helper rather than over HTTP: the test client's
        multipart encoder percent-escapes a control character in a filename
        before it leaves, and a real sender need not.
        """
        assert (
            document_service.display_filename("bank\nstatement​\tmarch.pdf")
            == "bank statement march.pdf"
        )

    def test_a_path_is_reduced_to_its_basename(self, client, auth_headers, client_id):
        response = self._upload(
            client,
            auth_headers,
            client_id,
            filename="../../etc/form16.pdf",
            content_type="application/pdf",
        )
        assert response.status_code == 201, response.text
        assert response.json()["document"]["original_filename"] == "form16.pdf"

    def test_an_ordinary_name_is_left_exactly_as_sent(
        self, client, auth_headers, client_id
    ):
        response = self._upload(
            client,
            auth_headers,
            client_id,
            filename="Bank Statement — Mar 2026.pdf",
            content_type="application/pdf",
        )
        assert response.status_code == 201, response.text
        assert (
            response.json()["document"]["original_filename"]
            == "Bank Statement — Mar 2026.pdf"
        )

    def test_a_nameless_upload_falls_back_to_the_stored_name(self):
        assert document_service.display_filename("") == ""
        assert document_service.display_filename(None) == ""


class TestAnExplicitNullOnADocumentsRequiredFields:
    """``compliance_item_id`` is the one field here a caller may null out —
    that is how a document is unlinked from a filing. The rest back ``NOT
    NULL`` columns, and writing a null into one came back a 409 saying the
    change "conflicts with an existing record", which is what a duplicate says
    and is not what happened."""

    @pytest.mark.parametrize(
        "field", ["category", "satisfies_requirements", "is_shared_with_client"]
    )
    def test_a_null_is_refused_by_name(self, client, auth_headers, client_id, field):
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        response = client.patch(
            f"/api/v1/documents/{document['id']}",
            json={field: None},
            headers=auth_headers,
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["fields"][0]["field"] == field

    def test_unlinking_a_filing_still_works(self, client, auth_headers, client_id):
        item = first_item_of_type(client, {"Authorization": auth_headers["Authorization"]}, "GSTR3B_MONTHLY")
        document = upload(
            client, auth_headers, client_id=client_id, compliance_item_id=item["id"]
        ).json()["document"]
        assert document["compliance_item_id"] == item["id"]

        response = client.patch(
            f"/api/v1/documents/{document['id']}",
            json={"compliance_item_id": None},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["compliance_item_id"] is None
