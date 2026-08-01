"""Tests for the OpenRouter-backed AI helpers.

No network is touched: ``httpx.post`` is stubbed. The behaviour that matters
for a CA practice is that every AI call *degrades gracefully* — a missing API
key, a dead model or a malformed answer must never fail the caller, it must
fall back to the deterministic heuristic or template.
"""

from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest

from app.models.base import DocumentCategory
from app.services import ai
from app.services.ai import (
    CategorisationResult,
    OpenRouterClient,
    OpenRouterError,
    categorise_document,
    draft_client_message,
    extract_json,
    heuristic_category,
    regex_extract,
)

# --------------------------------------------------------------- stub plumbing --


class _StubResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self) -> dict:
        return self._payload


def _content(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


@pytest.fixture
def capture_posts(monkeypatch):
    """Record every outbound call and reply with queued responses."""
    calls: list[dict] = []
    queue: list[object] = []

    def _post(url, **kwargs):
        # The client reuses one payload dict across models, so snapshot it.
        calls.append(
            {
                "url": url,
                "json": deepcopy(kwargs.get("json")),
                "headers": kwargs.get("headers"),
            }
        )
        result = queue.pop(0) if queue else _StubResponse(_content("{}"))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ai.httpx, "post", _post)
    return calls, queue


# ------------------------------------------------------------ heuristic layer --


class TestHeuristicCategory:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("HDFC bank statement Apr-2026.pdf", DocumentCategory.BANK_STATEMENT),
            ("Form16_Ravi_FY2025-26.pdf", DocumentCategory.FORM_16),
            ("form_16_partA.pdf", DocumentCategory.FORM_16),
            ("26AS_AY2026-27.txt", DocumentCategory.FORM_26AS),
            ("AIS-download.json", DocumentCategory.AIS_TIS),
            ("salary register march.xlsx", DocumentCategory.SALARY_REGISTER),
            ("payroll-summary.csv", DocumentCategory.SALARY_REGISTER),
            ("purchase register Q1.xlsx", DocumentCategory.PURCHASE_INVOICE),
            ("GSTR3B-July.pdf", DocumentCategory.GST_RETURN),
            ("TDS challan 281.pdf", DocumentCategory.TDS_CHALLAN),
            ("Balance Sheet 2025-26.pdf", DocumentCategory.BALANCE_SHEET),
            ("profit and loss.pdf", DocumentCategory.PROFIT_AND_LOSS),
            ("aadhaar-front.jpg", DocumentCategory.AADHAAR),
            ("MOA-incorporation.pdf", DocumentCategory.INCORPORATION_DOC),
        ],
    )
    def test_recognises_indian_document_names(self, filename, expected):
        category, confidence = heuristic_category(filename)
        assert category is expected
        assert confidence == pytest.approx(0.55)

    def test_unknown_filename_falls_back_to_other_with_low_confidence(self):
        category, confidence = heuristic_category("scan_0012.pdf")
        assert category is DocumentCategory.OTHER
        assert confidence == pytest.approx(0.2)

    def test_matching_is_case_insensitive(self):
        assert heuristic_category("BANK.PDF")[0] is DocumentCategory.BANK_STATEMENT


class TestRegexExtract:
    def test_extracts_pan_gstin_and_tan(self):
        text = "PAN AABCN2345P, TAN PNEN12345B, GSTIN 27AABCN2345P1Z5 for the quarter"
        found = regex_extract(text)
        assert found["pan"] == "AABCN2345P"
        assert found["tan"] == "PNEN12345B"
        assert found["gstin"] == "27AABCN2345P1Z5"

    def test_pan_embedded_in_a_gstin_is_not_reported_separately(self):
        """A GSTIN contains a PAN; reporting it as a standalone PAN is wrong."""
        found = regex_extract("GSTIN: 27AABCN2345P1Z5")
        assert found["gstin"] == "27AABCN2345P1Z5"
        assert "pan" not in found

    def test_standalone_pan_still_found_alongside_a_gstin(self):
        found = regex_extract("GSTIN 27AABCN2345P1Z5 and the director's PAN ABCPR1234F")
        assert found["pan"] == "ABCPR1234F"

    def test_collects_every_distinct_gstin(self):
        found = regex_extract("27AABCN2345P1Z5 and 29AABCN2345P1Z1 and 27AABCN2345P1Z5")
        assert found["gstins"] == ["27AABCN2345P1Z5", "29AABCN2345P1Z1"]

    def test_lowercase_input_is_normalised(self):
        assert regex_extract("pan aabcn2345p")["pan"] == "AABCN2345P"

    def test_no_identifiers_returns_empty(self):
        assert regex_extract("Invoice for professional services rendered") == {}


class TestExtractJson:
    def test_reads_a_fenced_block(self):
        assert extract_json('```json\n{"category": "form_16"}\n```') == {"category": "form_16"}

    def test_reads_an_unfenced_object(self):
        assert extract_json('Sure! {"category": "other"}') == {"category": "other"}

    def test_reads_a_fenced_block_without_a_language_tag(self):
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_raises_when_there_is_no_object(self):
        with pytest.raises(ValueError, match="No JSON object"):
            extract_json("I am afraid I cannot help with that.")

    def test_raises_on_malformed_json(self):
        with pytest.raises(json.JSONDecodeError):
            extract_json('{"category": }')


# ----------------------------------------------------------- OpenRouter client --


class TestOpenRouterClient:
    def test_disabled_without_an_api_key(self):
        assert OpenRouterClient(api_key="").enabled is False

    def test_enabled_with_a_key(self):
        assert OpenRouterClient(api_key="sk-or-test").enabled is True

    def test_complete_refuses_when_disabled(self):
        with pytest.raises(OpenRouterError, match="not configured"):
            OpenRouterClient(api_key="").complete("sys", "user")

    def test_complete_returns_message_content(self, capture_posts):
        calls, queue = capture_posts
        queue.append(_StubResponse(_content("hello from the model")))

        answer = OpenRouterClient(api_key="sk-or-test", model="model-a").complete("sys", "user")

        assert answer == "hello from the model"
        assert len(calls) == 1
        assert calls[0]["json"]["model"] == "model-a"
        assert calls[0]["json"]["messages"][0]["content"] == "sys"
        assert calls[0]["headers"]["Authorization"] == "Bearer sk-or-test"

    def test_falls_back_to_the_second_model(self, capture_posts):
        """A free model that is rate-limited must not take the feature down."""
        calls, queue = capture_posts
        queue.append(httpx.ConnectError("free tier busy"))
        queue.append(_StubResponse(_content("answer from the fallback")))

        answer = OpenRouterClient(api_key="sk-or-test", model="primary").complete("sys", "user")

        assert answer == "answer from the fallback"
        assert [call["json"]["model"] for call in calls] == [
            "primary",
            ai.settings.openrouter_fallback_model,
        ]

    def test_raises_when_every_model_fails(self, capture_posts):
        _, queue = capture_posts
        queue.extend([httpx.ConnectError("down"), httpx.ConnectError("also down")])

        with pytest.raises(OpenRouterError, match="All OpenRouter models failed"):
            OpenRouterClient(api_key="sk-or-test").complete("sys", "user")

    def test_a_malformed_payload_is_treated_as_a_failure(self, capture_posts):
        _, queue = capture_posts
        queue.extend([_StubResponse({"unexpected": True}), _StubResponse({"unexpected": True})])

        with pytest.raises(OpenRouterError):
            OpenRouterClient(api_key="sk-or-test").complete("sys", "user")


# ------------------------------------------------------------- categorisation --


class TestCategoriseDocument:
    def test_uses_heuristics_when_no_key_is_configured(self):
        result = categorise_document(
            "HDFC bank statement.pdf",
            "PAN AABCN2345P",
            client=OpenRouterClient(api_key=""),
        )
        assert isinstance(result, CategorisationResult)
        assert result.category is DocumentCategory.BANK_STATEMENT
        assert result.source == "heuristic"
        assert result.extracted["pan"] == "AABCN2345P"

    def test_uses_the_model_answer_when_available(self, capture_posts):
        calls, queue = capture_posts
        queue.append(
            _StubResponse(
                _content(
                    json.dumps(
                        {
                            "category": "form_16",
                            "confidence": 0.93,
                            "pan": None,
                            "gstin": None,
                            "period": "FY2025-26",
                            "total_amount_inr": 845000,
                        }
                    )
                )
            )
        )

        result = categorise_document(
            "scan_0012.pdf", "Salary certificate", client=OpenRouterClient(api_key="sk-or-test")
        )

        assert result.category is DocumentCategory.FORM_16
        assert result.confidence == pytest.approx(0.93)
        assert result.source == "llm"
        assert result.extracted["period"] == "FY2025-26"
        assert result.extracted["total_amount_inr"] == 845000
        # The filename and an excerpt are both handed to the model.
        assert "scan_0012.pdf" in calls[0]["json"]["messages"][1]["content"]

    def test_regex_findings_win_over_the_model(self, capture_posts):
        """Deterministic extraction is more trustworthy than a free model."""
        _, queue = capture_posts
        queue.append(
            _StubResponse(_content(json.dumps({"category": "other", "pan": "ZZZZZ9999Z"})))
        )

        result = categorise_document(
            "doc.pdf", "PAN AABCN2345P", client=OpenRouterClient(api_key="sk-or-test")
        )

        assert result.extracted["pan"] == "AABCN2345P"

    def test_unknown_category_from_the_model_falls_back(self, capture_posts):
        _, queue = capture_posts
        queue.append(_StubResponse(_content(json.dumps({"category": "not_a_real_category"}))))

        result = categorise_document(
            "bank statement.pdf", client=OpenRouterClient(api_key="sk-or-test")
        )

        assert result.category is DocumentCategory.BANK_STATEMENT
        assert result.source == "heuristic"

    def test_non_json_answer_falls_back(self, capture_posts):
        _, queue = capture_posts
        queue.append(_StubResponse(_content("I think it is a Form 16.")))

        result = categorise_document(
            "form16.pdf", client=OpenRouterClient(api_key="sk-or-test")
        )

        assert result.category is DocumentCategory.FORM_16
        assert result.source == "heuristic"

    def test_transport_failure_falls_back(self, capture_posts):
        _, queue = capture_posts
        queue.extend([httpx.ConnectError("down"), httpx.ConnectError("down")])

        result = categorise_document(
            "GSTR3B.pdf", client=OpenRouterClient(api_key="sk-or-test")
        )

        assert result.category is DocumentCategory.GST_RETURN
        assert result.source == "heuristic"

    @pytest.mark.parametrize(
        ("reported", "expected"),
        [(1.7, 1.0), (-0.4, 0.0), (None, 0.6)],
    )
    def test_confidence_is_clamped(self, capture_posts, reported, expected):
        _, queue = capture_posts
        queue.append(
            _StubResponse(
                _content(json.dumps({"category": "other", "confidence": reported}))
            )
        )

        result = categorise_document("x.pdf", client=OpenRouterClient(api_key="sk-or-test"))

        assert result.confidence == pytest.approx(expected)


# ------------------------------------------------------------ message drafting --


class TestDraftClientMessage:
    def test_document_request_template_lists_the_documents(self):
        message = draft_client_message(
            purpose="document_request",
            client_name="Nimbus Textiles Pvt Ltd",
            context={
                "compliance": "GSTR-3B (Monthly)",
                "documents": ["sales_invoice", "purchase_invoice"],
            },
            llm=OpenRouterClient(api_key=""),
        )
        assert message.startswith("Dear Nimbus Textiles Pvt Ltd,")
        assert "GSTR-3B (Monthly)" in message
        assert "sales_invoice" in message
        assert "purchase_invoice" in message

    def test_filing_confirmation_template_includes_the_acknowledgement(self):
        message = draft_client_message(
            purpose="filing_confirmation",
            client_name="Ravi Traders",
            context={
                "compliance": "GSTR-1",
                "period": "2026-07",
                "acknowledgement_number": "AA2707260012345",
            },
            llm=OpenRouterClient(api_key=""),
        )
        assert "filed successfully" in message
        assert "AA2707260012345" in message
        assert "2026-07" in message

    def test_fee_reminder_template_includes_the_invoice(self):
        message = draft_client_message(
            purpose="fee_reminder",
            client_name="Ravi Traders",
            context={"invoice_number": "INV-0042", "amount_inr": "25,000"},
            llm=OpenRouterClient(api_key=""),
        )
        assert "INV-0042" in message
        assert "25,000" in message

    def test_unknown_purpose_uses_the_generic_template(self):
        message = draft_client_message(
            purpose="something_else",
            client_name="Ravi Traders",
            context={"compliance": "TDS 26Q", "due_date": "2026-10-31"},
            llm=OpenRouterClient(api_key=""),
        )
        assert "TDS 26Q" in message
        assert "2026-10-31" in message

    def test_uses_the_model_when_configured(self, capture_posts):
        calls, queue = capture_posts
        queue.append(_StubResponse(_content("  Dear Sir, kindly share the invoices.  ")))

        message = draft_client_message(
            purpose="document_request",
            client_name="Nimbus Textiles Pvt Ltd",
            context={"compliance": "GSTR-1"},
            channel="whatsapp",
            llm=OpenRouterClient(api_key="sk-or-test"),
        )

        assert message == "Dear Sir, kindly share the invoices."
        assert "whatsapp" in calls[0]["json"]["messages"][1]["content"]

    def test_model_failure_falls_back_to_the_template(self, capture_posts):
        _, queue = capture_posts
        queue.extend([httpx.ConnectError("down"), httpx.ConnectError("down")])

        message = draft_client_message(
            purpose="fee_reminder",
            client_name="Ravi Traders",
            context={"invoice_number": "INV-0042"},
            llm=OpenRouterClient(api_key="sk-or-test"),
        )

        assert "INV-0042" in message
        assert message.startswith("Dear Ravi Traders,")
