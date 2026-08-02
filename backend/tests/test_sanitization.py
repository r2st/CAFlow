"""Input sanitisation and upload content checks."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.schemas.auth import LoginRequest
from app.schemas.client import ClientCreate
from app.schemas.common import sanitize_text
from app.services import storage


class TestSanitizeText:
    def test_it_removes_null_bytes(self):
        # PostgreSQL rejects NUL in text outright, so this would be a 500.
        assert sanitize_text("Sharma\x00 & Co") == "Sharma & Co"

    def test_it_removes_ansi_escape_sequences(self):
        assert "\x1b" not in sanitize_text("\x1b[31mALERT\x1b[0m")

    def test_it_removes_zero_width_characters(self):
        assert sanitize_text("Ac​me") == "Acme"

    def test_it_removes_bidirectional_overrides(self):
        # The "Trojan Source" trick: text that renders as something else.
        assert sanitize_text("invoice‮gpj.exe") == "invoicegpj.exe"

    def test_it_keeps_newlines_and_tabs(self):
        assert sanitize_text("line one\nline two\tindented") == "line one\nline two\tindented"

    def test_it_trims_surrounding_whitespace(self):
        assert sanitize_text("  Acme Traders  ") == "Acme Traders"

    def test_it_keeps_ordinary_unicode(self):
        assert sanitize_text("शर्मा एंड कंपनी") == "शर्मा एंड कंपनी"

    def test_it_normalises_equivalent_forms(self):
        # Composed and decomposed forms must not become two different clients.
        assert sanitize_text("Amélie") == sanitize_text("Amélie")


class TestSchemaSanitization:
    def test_control_characters_are_stripped_from_a_client_name(self):
        assert ClientCreate(name="Acme\x07 Traders").name == "Acme Traders"

    def test_a_pan_survives_invisible_characters(self):
        # Sanitising before validation means a copy-paste with a zero-width
        # space is accepted rather than rejected for characters nobody sees.
        assert ClientCreate(name="Acme", pan=" aaacs1234f​").pan == "AAACS1234F"

    def test_passwords_are_left_exactly_as_typed(self):
        # Trimming a password would silently change the credential.
        assert LoginRequest(email="a@example.com", password="  spaced  ").password == (
            "  spaced  "
        )


class TestUploadSignatures:
    def test_a_windows_executable_is_refused(self):
        with pytest.raises(storage.UnsupportedFileType, match="Windows executable"):
            storage.validate_upload("application/pdf", 4, b"MZ\x90\x00payload")

    def test_a_linux_executable_is_refused(self):
        with pytest.raises(storage.UnsupportedFileType, match="Linux executable"):
            storage.validate_upload("image/png", 4, b"\x7fELF\x02\x01\x01")

    def test_a_shell_script_is_refused(self):
        with pytest.raises(storage.UnsupportedFileType, match="shell script"):
            storage.validate_upload("text/plain", 20, b"#!/bin/sh\nrm -rf /\n")

    def test_an_empty_file_is_refused(self):
        with pytest.raises(storage.UnsupportedFileType, match="empty"):
            storage.validate_upload("application/pdf", 3, b"   ")

    def test_a_real_pdf_is_accepted(self):
        storage.validate_upload("application/pdf", 8, b"%PDF-1.7\n...")

    def test_an_office_document_is_accepted(self):
        # .docx / .xlsx are zip containers and must not trip the archive rule.
        storage.validate_upload(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            8,
            b"PK\x03\x04\x14\x00\x06\x00",
        )

    def test_the_size_limit_still_applies_first(self):
        with pytest.raises(storage.UploadTooLarge):
            storage.validate_upload("application/pdf", 10**12, b"%PDF-1.7")


class TestLegacyOfficeUploads:
    """.doc and .xls are OLE containers, and OLE containers carry macros.

    A CA's client sends these constantly — bank statements, Tally exports,
    Form 26AS — so the refusal has to say what to do instead, and nothing in
    the app may invite one in the first place.
    """

    # D0CF11E0A1B11AE1, then the 16-byte CLSID that is zero in an ordinary
    # Word or Excel document.
    OLE_HEADER = bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 16

    def test_a_legacy_excel_file_is_refused_however_it_is_declared(self):
        for declared in ("application/vnd.ms-excel", "application/octet-stream", None):
            with pytest.raises(storage.UnsupportedFileType, match="legacy Word or Excel"):
                storage.validate_upload(declared, 64, self.OLE_HEADER + b"workbook")

    def test_the_refusal_says_what_to_send_instead(self):
        # "an OLE container" is true and useless: it leaves a client holding a
        # bank statement with nowhere to go.
        with pytest.raises(storage.UnsupportedFileType) as refusal:
            storage.validate_upload("application/msword", 64, self.OLE_HEADER + b"document")

        message = str(refusal.value)
        assert ".doc or .xls" in message
        assert ".docx, .xlsx or PDF" in message

    def test_the_allow_list_does_not_promise_what_the_bytes_refuse(self):
        # Listing these accepted an upload the signature check then turned
        # away, which is a promise the server cannot keep.
        assert "application/msword" not in storage.ALLOWED_CONTENT_TYPES
        assert "application/vnd.ms-excel" not in storage.ALLOWED_CONTENT_TYPES

    def test_the_modern_formats_are_still_accepted(self):
        # .docx / .xlsx are zip containers, and the point of refusing OLE is to
        # push senders here rather than to refuse spreadsheets.
        storage.validate_upload(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            8,
            b"PK\x03\x04\x14\x00\x06\x00",
        )

    def test_an_archive_is_refused_with_somewhere_to_go_too(self):
        with pytest.raises(storage.UnsupportedFileType, match="rather than an archive"):
            storage.validate_upload("application/zip", 64, b"Rar!\x1a\x07\x00payload")


class TestRefusalOrder:
    def test_the_bytes_are_read_before_the_declared_type_is_believed(self):
        """The declaration is a claim; the signature is evidence.

        Checking the claim first would answer a legacy .xls with "files of type
        application/vnd.ms-excel are not accepted" — naming a MIME type the
        sender never chose and cannot change, instead of the file they picked.
        """
        with pytest.raises(storage.UnsupportedFileType, match="legacy Word or Excel"):
            storage.validate_upload(
                "application/vnd.ms-excel",
                64,
                TestLegacyOfficeUploads.OLE_HEADER + b"workbook",
            )

    def test_an_unknown_type_with_no_signature_is_still_refused_by_type(self):
        with pytest.raises(storage.UnsupportedFileType, match="video/mp4"):
            storage.validate_upload("video/mp4", 16, b"not a known signature")


class TestContentTypeSniffing:
    def test_a_pdf_is_recognised_from_its_bytes(self):
        assert storage.sniff_content_type(b"%PDF-1.4 ...") == "application/pdf"

    def test_a_png_is_recognised_from_its_bytes(self):
        assert storage.sniff_content_type(b"\x89PNG\r\n\x1a\nrest") == "image/png"

    def test_riff_without_a_webp_tag_is_not_claimed(self):
        assert storage.sniff_content_type(b"RIFF....AVI ") is None

    def test_the_signature_overrides_a_wrong_declaration(self):
        assert storage.effective_content_type("text/csv", b"%PDF-1.4") == "application/pdf"

    def test_the_declaration_stands_when_there_is_no_signature(self):
        assert storage.effective_content_type("text/csv", b"date,amount\n") == "text/csv"


class TestUploadEndpoint:
    def test_an_executable_upload_is_rejected_with_415(
        self, client: TestClient, registered_firm, created_client
    ):
        response = client.post(
            "/api/v1/documents/upload",
            headers={"Authorization": f"Bearer {registered_firm['access_token']}"},
            files={"file": ("statement.pdf", b"MZ\x90\x00 payload", "application/pdf")},
            data={"client_id": created_client["id"]},
        )
        assert response.status_code == 415
        assert response.json()["error"]["code"] == "unsupported_media_type"
