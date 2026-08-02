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


class TestAModelThatIgnoresTheSchema:
    """The models here are the free tier, so the schema is a request, not a promise.

    Categorising a document is the one part of an upload allowed to be wrong.
    It is not allowed to lose the file: categorisation runs inline while the
    upload is being stored, so anything raising out of it becomes a 500 and the
    client never gets their document in.
    """

    @pytest.mark.parametrize(
        "reported",
        ["high", {"level": "high"}, ["0.9"], True, float("nan"), "", "null"],
    )
    def test_a_confidence_that_is_not_a_number_does_not_lose_the_upload(
        self, capture_posts, reported
    ):
        _, queue = capture_posts
        queue.append(
            _StubResponse(
                _content(json.dumps({"category": "bank_statement", "confidence": reported}))
            )
        )

        result = categorise_document(
            "statement.pdf", client=OpenRouterClient(api_key="sk-or-test")
        )

        # The model still classified the document, so its answer stands; only
        # the unusable confidence is replaced.
        assert result.category is DocumentCategory.BANK_STATEMENT
        assert result.confidence == pytest.approx(0.6)

    def test_a_boolean_confidence_is_not_read_as_certainty(self, capture_posts):
        # json true is not 100% confidence; it is a model that ignored the
        # schema, and a document shown as certain is never queried by hand.
        _, queue = capture_posts
        queue.append(
            _StubResponse(_content(json.dumps({"category": "other", "confidence": True})))
        )

        result = categorise_document("x.pdf", client=OpenRouterClient(api_key="sk-or-test"))

        assert result.confidence == pytest.approx(0.6)

    @pytest.mark.parametrize("reported", ["not-a-pan", "ABCDE1234", "12345", "see attached"])
    def test_a_malformed_pan_is_dropped_rather_than_stored(self, capture_posts, reported):
        """A hallucinated identifier reaching a client record is worse than none.

        The regexes guarantee a well-formed identifier and the model guarantees
        nothing, but both land in the same field — so a practitioner reading
        "PAN: ABCDE1234F" cannot tell which one produced it.
        """
        _, queue = capture_posts
        queue.append(
            _StubResponse(
                _content(json.dumps({"category": "other", "confidence": 0.9, "pan": reported}))
            )
        )

        result = categorise_document(
            "doc.pdf", client=OpenRouterClient(api_key="sk-or-test")
        )

        assert "pan" not in result.extracted

    def test_a_well_formed_identifier_is_kept_and_normalised(self, capture_posts):
        _, queue = capture_posts
        queue.append(
            _StubResponse(
                _content(
                    json.dumps(
                        {
                            "category": "other",
                            "confidence": 0.9,
                            "pan": " aabcn2345p ",
                            "gstin": "27aabcn2345p1zv",
                        }
                    )
                )
            )
        )

        result = categorise_document(
            "doc.pdf", client=OpenRouterClient(api_key="sk-or-test")
        )

        assert result.extracted["pan"] == "AABCN2345P"
        assert result.extracted["gstin"] == "27AABCN2345P1ZV"

    def test_a_malformed_gstin_is_dropped(self, capture_posts):
        _, queue = capture_posts
        queue.append(
            _StubResponse(
                _content(json.dumps({"category": "other", "gstin": "27AABCN2345P"}))
            )
        )

        result = categorise_document("doc.pdf", client=OpenRouterClient(api_key="sk-or-test"))

        assert "gstin" not in result.extracted

    @pytest.mark.parametrize("reported", ["Rs 1,23,456", "about eight lakh", True])
    def test_an_amount_that_is_not_a_number_is_not_stored_as_one(
        self, capture_posts, reported
    ):
        # The field is asked for as a number and read as one downstream, so it
        # holds a number or nothing.
        _, queue = capture_posts
        queue.append(
            _StubResponse(
                _content(
                    json.dumps(
                        {"category": "other", "total_amount_inr": reported}
                    )
                )
            )
        )

        result = categorise_document("doc.pdf", client=OpenRouterClient(api_key="sk-or-test"))

        assert "total_amount_inr" not in result.extracted

    def test_a_numeric_amount_sent_as_a_string_is_kept(self, capture_posts):
        _, queue = capture_posts
        queue.append(
            _StubResponse(
                _content(json.dumps({"category": "other", "total_amount_inr": "845000"}))
            )
        )

        result = categorise_document("doc.pdf", client=OpenRouterClient(api_key="sk-or-test"))

        assert result.extracted["total_amount_inr"] == pytest.approx(845000)


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


# ------------------------------------------------------------ drafting budget --


class TestDraftingBudgetKeepsABatchInsideItsTimeLimit:
    """A nightly run has ten minutes; the wording must not be what spends them.

    Each reminder is drafted over a blocking HTTP call that can take the full
    OpenRouter timeout twice — model, then fallback. A firm's filings cluster
    on one offset day, so the run drafts as many messages as the firm has
    clients. Nothing bounded that against Celery's limit, and the run is one
    transaction: a firm large enough had the task killed and every reminder it
    had built discarded. Nothing was queued, nothing was logged as failing, and
    ``days_left in offsets`` matches a single day, so that reminder never went
    out at all.
    """

    def test_wording_stops_at_the_allowance_and_the_message_still_arrives(
        self, capture_posts
    ):
        calls, queue = capture_posts
        queue.append(_StubResponse(_content("Kindly share the invoices.")))
        budget = ai.DraftingBudget(seconds=0)

        message = draft_client_message(
            purpose="fee_reminder",
            client_name="Ravi Traders",
            context={"invoice_number": "INV-0042", "amount_inr": "25,000"},
            llm=OpenRouterClient(api_key="sk-or-test"),
            budget=budget,
        )

        # The model was never dialled...
        assert calls == []
        # ...and the client is still told what they owe and on which invoice.
        assert "INV-0042" in message
        assert "25,000" in message
        assert budget.templated == 1
        assert budget.drafted == 0

    def test_an_allowance_with_time_left_still_uses_the_model(self, capture_posts):
        calls, queue = capture_posts
        queue.append(_StubResponse(_content("Kindly share the invoices.")))
        budget = ai.DraftingBudget(seconds=600)

        message = draft_client_message(
            purpose="fee_reminder",
            client_name="Ravi Traders",
            context={"invoice_number": "INV-0042"},
            llm=OpenRouterClient(api_key="sk-or-test"),
            budget=budget,
        )

        assert message == "Kindly share the invoices."
        assert len(calls) == 1
        assert budget.drafted == 1

    def test_the_time_a_failing_provider_burns_counts_against_the_run(
        self, capture_posts
    ):
        """A provider that is timing out is when this matters most.

        The fallback to the template already made a dead model harmless per
        message. It is per *run* that it was not: two timed-out requests per
        reminder is the slowest the drafting ever gets, which is exactly when
        the allowance has to stop the run reaching for it again.
        """
        _, queue = capture_posts
        queue.extend([httpx.ConnectError("down"), httpx.ConnectError("down")])
        budget = ai.DraftingBudget(seconds=600)

        draft_client_message(
            purpose="fee_reminder",
            client_name="Ravi Traders",
            context={"invoice_number": "INV-0042"},
            llm=OpenRouterClient(api_key="sk-or-test"),
            budget=budget,
        )

        assert budget.templated == 1
        assert budget.drafted == 0

    def test_a_caller_with_no_allowance_is_unchanged(self, capture_posts):
        """Interactive drafting of one message has nothing to run out of."""
        calls, queue = capture_posts
        queue.append(_StubResponse(_content("Kindly share the invoices.")))

        message = draft_client_message(
            purpose="fee_reminder",
            client_name="Ravi Traders",
            context={"invoice_number": "INV-0042"},
            llm=OpenRouterClient(api_key="sk-or-test"),
        )

        assert message == "Kindly share the invoices."
        assert len(calls) == 1

    def test_an_unconfigured_key_never_consults_the_allowance(self):
        """No key means the template regardless, and no clock to read."""
        budget = ai.DraftingBudget(seconds=0)
        message = draft_client_message(
            purpose="fee_reminder",
            client_name="Ravi Traders",
            context={"invoice_number": "INV-0042"},
            llm=OpenRouterClient(api_key=""),
            budget=budget,
        )
        assert "INV-0042" in message
        assert budget.templated == 0

    def test_the_default_allowance_sits_well_inside_the_task_time_limit(self):
        """The number only helps if it is smaller than the limit it guards.

        Celery kills the queueing tasks at ten minutes. An allowance at or
        above that would be a setting that reads like a guard and is not one.
        """
        from app.worker.celery_app import TASK_SOFT_TIME_LIMIT_SECONDS

        budget = ai.DraftingBudget()
        assert 0 < budget.seconds < TASK_SOFT_TIME_LIMIT_SECONDS
        # And with room left for one request already in flight when it runs out,
        # plus the database work the run still has to finish.
        from app.config import settings

        overrun = budget.seconds + 2 * settings.openrouter_timeout_seconds
        assert overrun < TASK_SOFT_TIME_LIMIT_SECONDS

    def test_the_summary_says_how_the_run_actually_went(self):
        budget = ai.DraftingBudget(seconds=600)
        budget._record(drafted=True)
        budget._record(drafted=False)
        summary = budget.summary()
        assert "1 message(s) drafted by the model" in summary
        assert "1 from the template" in summary


class TestTheTemplateSaysTheRightThing:
    """The deterministic wording is the ordinary path, not the exceptional one.

    ``OPENROUTER_API_KEY`` is empty by default, so a deployment that has not
    configured a model sends exactly this to every client, every time.
    """

    def _draft(self, purpose: str, context: dict, firm_name: str | None = None) -> str:
        return draft_client_message(
            purpose=purpose,
            client_name="Ravi Traders",
            context=context,
            firm_name=firm_name,
            llm=OpenRouterClient(api_key=""),
        )

    def test_a_fee_reminder_asks_for_payment_not_for_documents(self):
        message = self._draft("fee_reminder", {"invoice_number": "INV-0042"})

        assert "Please arrange payment" in message
        # The document chase's closing, which used to end all four.
        assert "share them" not in message

    def test_a_filing_confirmation_does_not_then_ask_for_documents(self):
        message = self._draft(
            "filing_confirmation", {"compliance": "GSTR-1", "period": "2026-07"}
        )

        assert "filed successfully" in message
        assert "No action is needed" in message
        assert "share them" not in message

    def test_a_document_request_still_asks_for_the_documents(self):
        message = self._draft(
            "document_request", {"compliance": "GSTR-3B", "documents": ["Bank statement"]}
        )

        assert "Please share them at your earliest convenience." in message

    def test_a_deadline_reminder_offers_help_rather_than_asking_for_files(self):
        message = self._draft("something_else", {"compliance": "TDS 26Q"})

        assert "share them" not in message
        assert "let us know" in message

    @pytest.mark.parametrize(
        "purpose", ["document_request", "fee_reminder", "filing_confirmation", "anything"]
    )
    def test_the_firm_signs_its_own_messages(self, purpose: str):
        message = self._draft(purpose, {}, firm_name="Sharma & Associates")

        assert message.endswith("Regards,\nSharma & Associates")
        # The client is the CA's client and has never heard of the product.
        assert "CAFlow" not in message

    def test_without_a_firm_it_signs_generically_rather_than_as_the_product(self):
        message = self._draft("fee_reminder", {})

        assert message.endswith("Regards,\nYour Chartered Accountant")
        assert "CAFlow" not in message

    def test_the_model_is_told_who_it_is_writing_for(self, capture_posts):
        calls, queue = capture_posts
        queue.append(_StubResponse(_content("Dear Sir, kindly settle the invoice.")))

        draft_client_message(
            purpose="fee_reminder",
            client_name="Ravi Traders",
            context={"invoice_number": "INV-0042"},
            firm_name="Sharma & Associates",
            llm=OpenRouterClient(api_key="sk-or-test"),
        )

        prompt = calls[0]["json"]["messages"][1]["content"]
        assert "Sharma & Associates" in prompt

    def test_a_model_failure_falls_back_to_the_firms_own_signature(self, capture_posts):
        _, queue = capture_posts
        queue.extend([httpx.ConnectError("down"), httpx.ConnectError("down")])

        message = draft_client_message(
            purpose="fee_reminder",
            client_name="Ravi Traders",
            context={"invoice_number": "INV-0042"},
            firm_name="Sharma & Associates",
            llm=OpenRouterClient(api_key="sk-or-test"),
        )

        assert message.endswith("Regards,\nSharma & Associates")
