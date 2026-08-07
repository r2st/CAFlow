"""Document intake: upload, categorisation, checklists and the chase list.

Uploads go to a throwaway storage directory configured in ``conftest``.
"""

from __future__ import annotations

import io
import logging
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.exc import OperationalError

from app.core import clock
from app.models.base import DocumentCategory
from app.models.document import Document
from app.services import documents as document_service
from app.services import storage
from tests.conftest import first_item_of_type, make_client_payload, paged_order_by

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
            params={
                "from_date": (clock.today() - timedelta(days=180)).isoformat(),
                "to_date": (clock.today() + timedelta(days=180)).isoformat(),
            },
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


class TestTheChaseListWindowIsBounded:
    """The last landing-page read still shaped like "load the table and reduce it here".

    A checklist is not an aggregate: every filing in the window is hydrated,
    every document attached to any of them is read, and a
    requirement-by-requirement checklist is built and serialised for each —
    with no ``limit`` on the result. The window was the only thing bounding
    that work, and it was the caller's.
    """

    def _window(self, client, auth_headers, *, days):
        return client.get(
            "/api/v1/documents/outstanding",
            params={
                "from_date": clock.today().isoformat(),
                "to_date": (clock.today() + timedelta(days=days)).isoformat(),
            },
            headers=auth_headers,
        )

    def test_a_decade_wide_window_is_refused(self, client, auth_headers, client_id):
        """A firm's entire calendar, past and pre-generated, in one body."""
        response = client.get(
            "/api/v1/documents/outstanding",
            params={"from_date": "1990-01-01", "to_date": "2099-12-31"},
            headers=auth_headers,
        )
        assert response.status_code == 422
        assert "at most" in response.json()["detail"]

    def test_the_refusal_names_both_dates(self, client, auth_headers):
        response = self._window(client, auth_headers, days=400)
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert f"{clock.today():%d %b %Y}" in detail

    def test_a_year_is_still_answered(self, client, auth_headers, client_id):
        """The calendar is only generated a year ahead, so a year is the whole
        of what there is to be waiting for."""
        assert self._window(client, auth_headers, days=366).status_code == 200

    def test_the_default_window_needs_no_dates_at_all(
        self, client, auth_headers, client_id
    ):
        response = client.get("/api/v1/documents/outstanding", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["total_items"] > 0


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


class TestPagingDocumentsUploadedTogether:
    """``created_at`` alone does not settle the order of a batch of uploads.

    A page taken in an order that is not total repeats rows and drops others;
    see :func:`tests.conftest.paged_order_by`.
    """

    def _upload_a_batch(self, client, auth_headers, client_id, count=6) -> int:
        for index in range(count):
            response = client.post(
                "/api/v1/documents/upload",
                files={
                    "file": (
                        f"statement-{index:02d}.pdf",
                        io.BytesIO(b"%PDF-1.4\n% a statement\n"),
                        "application/pdf",
                    )
                },
                data={"client_id": client_id},
                headers=auth_headers,
            )
            assert response.status_code == 201, response.text
        return count

    def test_paging_shows_every_document_exactly_once(
        self, client, auth_headers, client_id
    ):
        uploaded = self._upload_a_batch(client, auth_headers, client_id)

        seen: list[str] = []
        offset = 0
        while True:
            page = client.get(
                "/api/v1/documents",
                params={"limit": 2, "offset": offset},
                headers=auth_headers,
            ).json()
            seen.extend(row["id"] for row in page["items"])
            offset += 2
            if offset >= page["total"]:
                break

        assert len(seen) == uploaded
        assert len(set(seen)) == uploaded, "a document appeared on two pages"

    def test_the_order_the_page_is_taken_in_settles_every_pair_of_rows(
        self, client, auth_headers, client_id, recorded_sql
    ):
        self._upload_a_batch(client, auth_headers, client_id, count=2)

        recorded_sql.clear()
        assert client.get("/api/v1/documents", headers=auth_headers).status_code == 200

        assert paged_order_by(recorded_sql, "documents").endswith("documents.id DESC")


class TestReachingOneDocument:
    """The single-document reads, and the two ways they answer nothing."""

    def test_a_document_can_be_read_on_its_own(self, client, auth_headers, client_id):
        uploaded = upload(client, auth_headers, client_id=client_id).json()["document"]
        response = client.get(
            f"/api/v1/documents/{uploaded['id']}", headers=auth_headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == uploaded["id"]
        assert body["client_name"] == "Nimbus Textiles Pvt Ltd"

    def test_a_document_this_firm_cannot_reach_is_a_404(self, client, auth_headers):
        assert (
            client.get(
                f"/api/v1/documents/{uuid.uuid4()}", headers=auth_headers
            ).status_code
            == 404
        )

    def test_a_checklist_for_a_filing_this_firm_cannot_reach_is_a_404(
        self, client, auth_headers
    ):
        assert (
            client.get(
                f"/api/v1/documents/checklist/{uuid.uuid4()}", headers=auth_headers
            ).status_code
            == 404
        )

    def test_a_download_whose_bytes_have_gone_is_a_410(
        self, client, auth_headers, client_id, db
    ):
        """The row outlives the file if the volume is swapped, restored or swept.

        Reported as gone rather than as a server fault: there is nothing wrong
        with the request, and a 500 would send a practitioner to support over a
        file that is simply not there any more.
        """
        from app.models.document import Document

        uploaded = upload(client, auth_headers, client_id=client_id).json()["document"]
        row = db.get(Document, uuid.UUID(uploaded["id"]))
        storage.resolve_stored(row.storage_path).unlink()

        response = client.get(
            f"/api/v1/documents/{uploaded['id']}/download", headers=auth_headers
        )
        assert response.status_code == 410, response.text
        assert "no longer available" in response.json()["detail"]


class TestNarrowingTheDocumentList:
    def test_by_the_filing_a_document_belongs_to(
        self, client, auth_headers, client_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        upload(
            client,
            auth_headers,
            client_id=client_id,
            filename="linked.pdf",
            compliance_item_id=item["id"],
        )
        upload(client, auth_headers, client_id=client_id, filename="loose.pdf")

        listed = client.get(
            "/api/v1/documents",
            params={"compliance_item_id": item["id"]},
            headers=auth_headers,
        ).json()
        assert listed["total"] == 1
        assert listed["items"][0]["original_filename"] == "linked.pdf"
        assert listed["items"][0]["compliance_label"].startswith("GSTR-3B")

    def test_by_who_sent_it(self, client, auth_headers, client_id):
        """The firm's own working papers and what the client sent are different piles."""
        upload(client, auth_headers, client_id=client_id, filename="workings.pdf")
        token = client.post(
            f"/api/v1/clients/{client_id}/portal-link", json={}, headers=auth_headers
        ).json()["token"]
        client.post(
            "/api/v1/portal/documents",
            files={"file": ("from-client.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
            headers={"Authorization": f"Bearer {token}"},
        )

        from_client = client.get(
            "/api/v1/documents",
            params={"uploaded_via_portal": True},
            headers=auth_headers,
        ).json()
        assert [row["original_filename"] for row in from_client["items"]] == [
            "from-client.pdf"
        ]

        ours = client.get(
            "/api/v1/documents",
            params={"uploaded_via_portal": False},
            headers=auth_headers,
        ).json()
        assert [row["original_filename"] for row in ours["items"]] == ["workings.pdf"]


class TestRelinkingADocumentToAFiling:
    """Attaching a document to the return it belongs to, after the fact.

    A practitioner uploads first and files second at least as often as the
    reverse, so the link is an edit. The filing has to be one this firm can
    reach *and* one belonging to the same client — a document is the client's,
    and moving it onto another client's return would satisfy their checklist
    with somebody else's paperwork.
    """

    def test_the_link_satisfies_the_filings_checklist(
        self, client, auth_headers, client_id
    ):
        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        uploaded = upload(
            client, auth_headers, client_id=client_id, filename="bank.pdf"
        ).json()["document"]

        response = client.patch(
            f"/api/v1/documents/{uploaded['id']}",
            json={"compliance_item_id": item["id"]},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["compliance_item_id"] == item["id"]

        checklist = client.get(
            f"/api/v1/documents/checklist/{item['id']}", headers=auth_headers
        ).json()
        satisfied = [
            state["requirement"] for state in checklist["requirements"] if state["satisfied"]
        ]
        assert "bank_statement" in satisfied

    def test_linking_to_another_clients_filing_is_a_400(
        self, client, auth_headers, client_id
    ):
        other_id = client.post(
            "/api/v1/clients",
            json=make_client_payload(name="Second Client", pan="BBBPC1234D"),
            headers=auth_headers,
        ).json()["client"]["id"]
        theirs = client.get(
            "/api/v1/compliance/calendar",
            params={
                "from_date": "2020-01-01",
                "to_date": "2035-12-31",
                "client_id": other_id,
                "limit": 1,
            },
            headers=auth_headers,
        ).json()["items"][0]
        uploaded = upload(client, auth_headers, client_id=client_id).json()["document"]

        response = client.patch(
            f"/api/v1/documents/{uploaded['id']}",
            json={"compliance_item_id": theirs["id"]},
            headers=auth_headers,
        )
        assert response.status_code == 400, response.text
        assert "different client" in response.json()["detail"]

    def test_linking_to_a_filing_this_firm_cannot_reach_is_a_404(
        self, client, auth_headers, client_id
    ):
        uploaded = upload(client, auth_headers, client_id=client_id).json()["document"]
        response = client.patch(
            f"/api/v1/documents/{uploaded['id']}",
            json={"compliance_item_id": str(uuid.uuid4())},
            headers=auth_headers,
        )
        assert response.status_code == 404, response.text


class TestTheChaseListNarrowsToOneClient:
    def test_only_that_clients_filings_are_returned(
        self, client, auth_headers, client_id
    ):
        other_id = client.post(
            "/api/v1/clients",
            json=make_client_payload(name="Second Client", pan="BBBPC1234D"),
            headers=auth_headers,
        ).json()["client"]["id"]

        whole_firm = client.get(
            "/api/v1/documents/outstanding", headers=auth_headers
        ).json()
        assert {row["client_id"] for row in whole_firm["checklists"]} == {
            client_id,
            other_id,
        }

        narrowed = client.get(
            "/api/v1/documents/outstanding",
            params={"client_id": client_id},
            headers=auth_headers,
        ).json()
        assert {row["client_id"] for row in narrowed["checklists"]} == {client_id}
        assert narrowed["total_items"] < whole_firm["total_items"]


class TestTheSmallPartsOfTheDocumentService:
    def test_a_missing_content_type_stays_missing(self):
        """An upload with no declared type records none, rather than an empty string."""
        assert document_service.bounded_content_type(None) is None
        assert document_service.bounded_content_type("   ") is None
        assert document_service.bounded_content_type("application/pdf") == "application/pdf"

    def test_a_checklist_counts_what_it_has_as_well_as_what_it_wants(
        self, client, auth_headers, client_id, db
    ):
        """``satisfied_count`` is what a progress reading is built from."""
        from app.models.compliance import ComplianceItem

        item = first_item_of_type(client, auth_headers, "GSTR3B_MONTHLY")
        row = db.get(ComplianceItem, uuid.UUID(item["id"]))

        before = document_service.checklist_for_item(db, row)
        assert before.satisfied_count == 0
        assert not before.is_complete

        upload(
            client,
            auth_headers,
            client_id=client_id,
            filename="bank.pdf",
            compliance_item_id=item["id"],
        )
        after = document_service.checklist_for_item(db, row)
        assert after.satisfied_count == len(after.requirements) - len(after.missing)
        assert after.satisfied_count > 0


class TestDeletingBytesThatAreNotThere:
    """``delete_stored`` answers rather than raises, because the caller has nothing to do.

    The row is already gone by the time it runs — see
    :func:`~app.api.routes.documents.delete_document` — so a file that is
    missing, or a path that does not resolve inside the storage root, is a
    false rather than an exception the endpoint would have to swallow.
    """

    def test_a_file_that_has_already_gone(self, client, auth_headers, client_id, db):
        from app.models.document import Document

        uploaded = upload(client, auth_headers, client_id=client_id).json()["document"]
        path = db.get(Document, uuid.UUID(uploaded["id"])).storage_path

        assert storage.delete_stored(path) is True
        assert storage.delete_stored(path) is False

    def test_a_path_that_escapes_the_storage_root(self):
        assert storage.delete_stored("../../etc/passwd") is False


def member_headers(client, auth_headers, *, role: str, email: str) -> dict[str, str]:
    """A second practitioner of the firm, signed in.

    Built by the same two calls a real one is, rather than by minting a token:
    the role gate reads the practitioner's row on every request, so a token
    handed out here would prove nothing about what the row says.
    """
    password = "another-long-password"
    created = client.post(
        "/api/v1/auth/practitioners",
        json={"full_name": "Team Member", "email": email, "password": password, "role": role},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    token = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestWhoMayDestroyADocument:
    """Deleting a document is irreversible and was open to every role.

    The row goes and the bytes go with it — ``delete_stored`` unlinks the file
    from the storage volume — so there is nothing to undo and nothing to
    restore from. Every other irreversible path in the firm is already a
    manager's: deactivating a client, cancelling an invoice, deleting a task.

    It is also the client's statutory record, and it is what the checklist
    reads to say a requirement was met, so a deletion silently reopens the
    chase and the client is emailed for paperwork they have already sent.
    """

    def test_a_junior_cannot_delete_a_document(self, client, auth_headers, client_id):
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        junior = member_headers(
            client, auth_headers, role="junior", email="junior-del@sharma-ca.in"
        )

        response = client.delete(f"/api/v1/documents/{document['id']}", headers=junior)
        assert response.status_code == 403
        assert "permissions" in response.json()["detail"]

        # And the document is still there to be found.
        still = client.get(f"/api/v1/documents/{document['id']}", headers=auth_headers)
        assert still.status_code == 200

    def test_the_bytes_survive_the_refusal(self, client, auth_headers, client_id, db):
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        path = db.get(Document, uuid.UUID(document["id"])).storage_path
        junior = member_headers(
            client, auth_headers, role="junior", email="junior-bytes@sharma-ca.in"
        )

        client.delete(f"/api/v1/documents/{document['id']}", headers=junior)
        assert storage.open_stored(path) == PDF_BYTES

    def test_a_manager_may_delete_a_document(self, client, auth_headers, client_id):
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        manager = member_headers(
            client, auth_headers, role="manager", email="manager-del@sharma-ca.in"
        )

        response = client.delete(f"/api/v1/documents/{document['id']}", headers=manager)
        assert response.status_code == 204


class TestWhoMayShareADocumentWithTheClient:
    """``is_shared_with_client`` is the one field on a document that leaves the firm.

    It is what ``/portal/me`` lists and what ``portal_download`` serves, so
    setting it hands a file to a party the firm does not employ — and it cannot
    be recalled, because the client has downloaded it by the time anyone
    notices. A client's folder holds the firm's working papers beside the
    client's own documents, one toggle apart.

    Every other decision about that channel was already a manager's: minting a
    magic link, enabling the portal, revoking it. This was the one with the
    content in it, and it was open to every role.
    """

    def test_a_junior_cannot_share_a_document(self, client, auth_headers, client_id):
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        assert document["is_shared_with_client"] is False
        junior = member_headers(
            client, auth_headers, role="junior", email="junior-share@sharma-ca.in"
        )

        response = client.patch(
            f"/api/v1/documents/{document['id']}",
            json={"is_shared_with_client": True},
            headers=junior,
        )
        assert response.status_code == 403
        assert "managers" in response.json()["detail"]

        unchanged = client.get(
            f"/api/v1/documents/{document['id']}", headers=auth_headers
        ).json()
        assert unchanged["is_shared_with_client"] is False

    def test_a_junior_cannot_share_one_on_the_way_in_either(
        self, client, auth_headers, client_id
    ):
        """The upload form carries the same flag, and a gate on one door is neither."""
        junior = member_headers(
            client, auth_headers, role="junior", email="junior-upshare@sharma-ca.in"
        )
        response = upload(client, junior, client_id=client_id, share_with_client=True)
        assert response.status_code == 403

    def test_a_junior_may_still_upload_and_recategorise(
        self, client, auth_headers, client_id
    ):
        """The day-to-day work the role exists for is untouched."""
        junior = member_headers(
            client, auth_headers, role="junior", email="junior-work@sharma-ca.in"
        )
        uploaded = upload(client, junior, client_id=client_id)
        assert uploaded.status_code == 201

        recategorised = client.patch(
            f"/api/v1/documents/{uploaded.json()['document']['id']}",
            json={"category": DocumentCategory.BANK_STATEMENT.value},
            headers=junior,
        )
        assert recategorised.status_code == 200
        assert recategorised.json()["category"] == DocumentCategory.BANK_STATEMENT.value

    def test_a_junior_may_re_send_the_flag_unchanged(self, client, auth_headers, client_id):
        """A round-trip is not an instruction.

        The editor sends the whole document back, so a junior correcting the
        category of an already-shared document carries ``is_shared_with_client``
        along with it. Refusing that would refuse the edit they are entitled to
        make; only a change of the flag is the decision being gated.
        """
        document = upload(
            client, auth_headers, client_id=client_id, share_with_client=True
        ).json()["document"]
        assert document["is_shared_with_client"] is True
        junior = member_headers(
            client, auth_headers, role="junior", email="junior-roundtrip@sharma-ca.in"
        )

        response = client.patch(
            f"/api/v1/documents/{document['id']}",
            json={
                "is_shared_with_client": True,
                "category": DocumentCategory.BANK_STATEMENT.value,
            },
            headers=junior,
        )
        assert response.status_code == 200
        assert response.json()["is_shared_with_client"] is True

    def test_a_junior_cannot_unshare_one_either(self, client, auth_headers, client_id):
        """Both directions. Withdrawing a document the client has been told to
        expect is as much a decision about that channel as releasing one."""
        document = upload(
            client, auth_headers, client_id=client_id, share_with_client=True
        ).json()["document"]
        junior = member_headers(
            client, auth_headers, role="junior", email="junior-unshare@sharma-ca.in"
        )

        response = client.patch(
            f"/api/v1/documents/{document['id']}",
            json={"is_shared_with_client": False},
            headers=junior,
        )
        assert response.status_code == 403

    def test_a_manager_may_share_a_document(self, client, auth_headers, client_id):
        document = upload(client, auth_headers, client_id=client_id).json()["document"]
        manager = member_headers(
            client, auth_headers, role="manager", email="manager-share@sharma-ca.in"
        )

        response = client.patch(
            f"/api/v1/documents/{document['id']}",
            json={"is_shared_with_client": True},
            headers=manager,
        )
        assert response.status_code == 200
        assert response.json()["is_shared_with_client"] is True
