"""OpenRouter client for the AI features (free models only).

Used for document categorisation, field extraction and drafting client
messages. Every call degrades gracefully: if no API key is configured or the
model is unavailable, callers fall back to deterministic heuristics rather than
failing the request.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import settings
from app.models.base import DocumentCategory

logger = logging.getLogger(__name__)

# Filename hints used both as a fallback and as a prior for the LLM.
FILENAME_HINTS: dict[str, DocumentCategory] = {
    "bank": DocumentCategory.BANK_STATEMENT,
    "statement": DocumentCategory.BANK_STATEMENT,
    "form16": DocumentCategory.FORM_16,
    "form_16": DocumentCategory.FORM_16,
    "26as": DocumentCategory.FORM_26AS,
    "ais": DocumentCategory.AIS_TIS,
    "salary": DocumentCategory.SALARY_REGISTER,
    "payroll": DocumentCategory.SALARY_REGISTER,
    "purchase": DocumentCategory.PURCHASE_INVOICE,
    "sales": DocumentCategory.SALES_INVOICE,
    "invoice": DocumentCategory.SALES_INVOICE,
    "gstr": DocumentCategory.GST_RETURN,
    "challan": DocumentCategory.TDS_CHALLAN,
    "balance": DocumentCategory.BALANCE_SHEET,
    "p&l": DocumentCategory.PROFIT_AND_LOSS,
    "profit": DocumentCategory.PROFIT_AND_LOSS,
    "pan": DocumentCategory.PAN_CARD,
    "aadhaar": DocumentCategory.AADHAAR,
    "moa": DocumentCategory.INCORPORATION_DOC,
    "incorporation": DocumentCategory.INCORPORATION_DOC,
}

PAN_PATTERN = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
GSTIN_PATTERN = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b")
TAN_PATTERN = re.compile(r"\b[A-Z]{4}[0-9]{5}[A-Z]\b")


@dataclass
class CategorisationResult:
    category: DocumentCategory
    confidence: float
    extracted: dict[str, Any] = field(default_factory=dict)
    source: str = "heuristic"  # "llm" when the model answered


class OpenRouterError(RuntimeError):
    pass


class OpenRouterClient:
    """Thin wrapper over the OpenRouter chat-completions endpoint."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.openrouter_api_key
        self.model = model or settings.openrouter_model
        self.fallback_model = settings.openrouter_fallback_model

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str, *, max_tokens: int = 700) -> str:
        if not self.enabled:
            raise OpenRouterError("OPENROUTER_API_KEY is not configured")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://caflow.aiknol.com",
            "X-Title": "CAFlow",
        }

        last_error: Exception | None = None
        for model in (self.model, self.fallback_model):
            payload["model"] = model
            try:
                response = httpx.post(
                    f"{settings.openrouter_base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=settings.openrouter_timeout_seconds,
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                logger.warning("OpenRouter call failed on model %s: %s", model, exc)
                last_error = exc
        raise OpenRouterError(f"All OpenRouter models failed: {last_error}")


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of an LLM response (which may be fenced)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = brace.group(0) if brace else None
    if candidate is None:
        raise ValueError("No JSON object found in model response")
    return json.loads(candidate)


def confidence_or(value: Any, default: float = 0.6) -> float:
    """Read a model-supplied confidence, clamped to 0-1.

    This cannot be a bare ``float()``. The models here are the free tier, and
    they answer ``"high"``, ``true`` or an object often enough to matter — and
    the conversion used to sit *after* the try block that catches everything
    else the model gets wrong, so a one-word confidence raised out of
    categorisation, out of the upload endpoint, and into a 500. Categorising a
    document is the one part of an upload allowed to be wrong; it is not
    allowed to lose the file.
    """
    if isinstance(value, bool):
        # json true is not 100% confidence; it is a model that ignored the schema.
        return default
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    if confidence != confidence:  # NaN, which clamps to itself
        return default
    return min(max(confidence, 0.0), 1.0)


# The model is asked for these, and what comes back is checked the way the
# deterministic extraction is: both land in the same field, so a practitioner
# reading "PAN: ABCDE1234F" cannot tell which produced it. A hallucinated or
# document-injected identifier that reaches a client record is worse than an
# absent one.
LLM_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "pan": PAN_PATTERN,
    "gstin": GSTIN_PATTERN,
}


def clean_llm_field(key: str, value: Any) -> Any | None:
    """Normalise one model-supplied field, or None if it cannot be trusted."""
    if value in (None, "", "null"):
        return None

    if pattern := LLM_FIELD_PATTERNS.get(key):
        candidate = str(value).strip().upper()
        return candidate if pattern.fullmatch(candidate) else None

    if key == "total_amount_inr":
        # Asked for as a number; stored as one or not at all, so that nothing
        # downstream has to guess whether this field holds "₹1,23,456".
        if isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return value


def heuristic_category(filename: str) -> tuple[DocumentCategory, float]:
    lowered = filename.lower()
    for hint, category in FILENAME_HINTS.items():
        if hint in lowered:
            return category, 0.55
    return DocumentCategory.OTHER, 0.2


def regex_extract(text: str) -> dict[str, Any]:
    """Cheap, deterministic extraction of the identifiers CAs care about."""
    upper = text.upper()
    found: dict[str, Any] = {}
    if gstins := GSTIN_PATTERN.findall(upper):
        found["gstin"] = gstins[0]
        found["gstins"] = sorted(set(gstins))
    # A GSTIN embeds a PAN, so blank the GSTINs out before looking for one:
    # a PAN is only reported when it is stated somewhere outside a GSTIN.
    # (Filtering on substring instead would drop the PAN of every GST-registered
    # client, since their documents quote both.)
    if pans := PAN_PATTERN.findall(GSTIN_PATTERN.sub(" ", upper)):
        found["pan"] = pans[0]
    if tans := TAN_PATTERN.findall(upper):
        found["tan"] = tans[0]
    return found


CATEGORISE_SYSTEM = (
    "You are a document classifier for an Indian chartered accountancy practice. "
    "Reply with a single JSON object and nothing else."
)

CATEGORISE_TEMPLATE = """Classify this document into exactly one category.

Categories: {categories}

Filename: {filename}
Extract of contents:
---
{excerpt}
---

Respond with JSON:
{{"category": "<one category>", "confidence": <0-1>, "pan": "<or null>",
  "gstin": "<or null>", "period": "<or null>", "total_amount_inr": <number or null>}}"""


def categorise_document(
    filename: str, text_excerpt: str = "", client: OpenRouterClient | None = None
) -> CategorisationResult:
    """Categorise a document, using the LLM when available and heuristics otherwise."""
    fallback_category, fallback_confidence = heuristic_category(filename)
    extracted = regex_extract(text_excerpt) if text_excerpt else {}

    client = client or OpenRouterClient()
    if not client.enabled:
        return CategorisationResult(fallback_category, fallback_confidence, extracted)

    prompt = CATEGORISE_TEMPLATE.format(
        categories=", ".join(c.value for c in DocumentCategory),
        filename=filename,
        excerpt=text_excerpt[:4000] or "(no text extracted)",
    )
    try:
        raw = client.complete(CATEGORISE_SYSTEM, prompt)
        data = extract_json(raw)
        category = DocumentCategory(data["category"])
    except (OpenRouterError, ValueError, KeyError) as exc:
        logger.info("Falling back to heuristic categorisation: %s", exc)
        return CategorisationResult(fallback_category, fallback_confidence, extracted)

    for key in ("pan", "gstin", "period", "total_amount_inr"):
        # setdefault: a value the regexes found in the document itself outranks
        # anything the model reports.
        if (value := clean_llm_field(key, data.get(key))) is not None:
            extracted.setdefault(key, value)

    return CategorisationResult(
        category, confidence_or(data.get("confidence")), extracted, source="llm"
    )


DRAFT_SYSTEM = (
    "You draft short, polite, professional messages from an Indian CA firm to its clients. "
    "Use Indian business English. Never invent figures or dates that were not supplied."
)


class DraftingBudget:
    """A wall-clock allowance for model-drafted wording across one batch job.

    The nightly queueing runs draft one message per reminder, over a blocking
    HTTP call that can take the full ``openrouter_timeout_seconds`` twice —
    once for the model and once for the fallback. A firm's filings cluster on
    the same offset day (every GST client is due on the 20th), so a run drafts
    as many messages as the firm has clients, not a handful.

    Celery gives the task ten minutes. Nothing bounded the drafting against
    that, and the whole run is one transaction, so a firm large enough to
    exceed the limit had the task killed and *every* reminder it had built
    thrown away — no rows, no error a firm would ever see, and the offset day
    gone. ``days_left in offsets`` matches one day exactly, so the reminder for
    that offset is not queued late; it is not queued at all.

    So the wording gets an allowance and the reminder does not. Once the
    allowance is spent the rest of the run uses the deterministic template,
    which says the same things in plainer words. Checked before each call
    rather than interrupting one in flight: the overrun is then at most one
    request, and a message half-received is not a message.
    """

    def __init__(self, seconds: float | None = None) -> None:
        self.seconds = settings.ai_draft_budget_seconds if seconds is None else seconds
        self._started = time.monotonic()
        self.drafted = 0
        self.templated = 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def exhausted(self) -> bool:
        return self.elapsed >= self.seconds

    def _record(self, *, drafted: bool) -> None:
        if drafted:
            self.drafted += 1
        else:
            self.templated += 1

    def summary(self) -> str:
        return (
            f"{self.drafted} message(s) drafted by the model, "
            f"{self.templated} from the template, in {self.elapsed:.1f}s"
        )


def draft_client_message(
    *,
    purpose: str,
    client_name: str,
    context: dict[str, Any],
    channel: str = "email",
    firm_name: str | None = None,
    llm: OpenRouterClient | None = None,
    budget: DraftingBudget | None = None,
) -> str:
    """Draft a reminder/confirmation message. Falls back to a template offline.

    ``firm_name`` is who the message is from. The client is the CA's client,
    not ours — they are hearing from their accountant, and the mail already
    carries the firm's sender name and reply-to, so the body has to agree.

    ``budget`` bounds how long a batch of these may spend on the model in
    total; see :class:`DraftingBudget`. Interactive callers drafting a single
    message pass none, because there is nothing to run out of.
    """
    llm = llm or OpenRouterClient()
    if not llm.enabled:
        return _template_message(purpose, client_name, context, firm_name)
    if budget is not None and budget.exhausted:
        budget._record(drafted=False)
        return _template_message(purpose, client_name, context, firm_name)

    sender = firm_name or DEFAULT_SIGNATORY
    details = "\n".join(f"- {key}: {value}" for key, value in context.items())
    prompt = (
        f"Draft a {channel} message to {client_name}, from {sender}.\n"
        f"Purpose: {purpose}\n"
        f"Details:\n{details}\n\n"
        f"Sign off as {sender}. Keep it under 120 words. "
        "Return only the message body."
    )
    try:
        message = llm.complete(DRAFT_SYSTEM, prompt, max_tokens=400).strip()
    except OpenRouterError as exc:
        logger.info("Falling back to template message: %s", exc)
        if budget is not None:
            # Counted against the run either way: the time was spent whether or
            # not the model answered, and a provider that is timing out is
            # exactly when the allowance has to stop the run reaching for it.
            budget._record(drafted=False)
        return _template_message(purpose, client_name, context, firm_name)
    if budget is not None:
        budget._record(drafted=True)
    return message


# The signature on a message with no firm behind it. Never the product name:
# this is a message from a CA to their own client, delivered under the firm's
# sender name and reply-to, and a client has no idea what CAFlow is.
DEFAULT_SIGNATORY = "Your Chartered Accountant"


def _template_message(
    purpose: str,
    client_name: str,
    context: dict[str, Any],
    firm_name: str | None = None,
) -> str:
    """The deterministic wording, used whenever the model is unavailable.

    Which is the ordinary case, not the exceptional one: ``OPENROUTER_API_KEY``
    is empty by default, so a deployment that has not configured a model sends
    exactly this to every client, every time.

    The closing line is per purpose. One line closed all four, and it was the
    document chase's — so a fee reminder asked the client to "share them", and
    a confirmation that their return had been filed successfully asked them to
    send documents for it anyway.
    """
    lines = [f"Dear {client_name},", ""]
    match purpose:
        case "document_request":
            lines.append(
                "We need a few documents to proceed with your upcoming filing"
                f" ({context.get('compliance', 'compliance')})."
            )
            if documents := context.get("documents"):
                lines.append("")
                lines.extend(f"  • {doc}" for doc in documents)
            closing = "Please share them at your earliest convenience."
        case "filing_confirmation":
            lines.append(
                f"Your {context.get('compliance', 'return')} for"
                f" {context.get('period', 'the period')} has been filed successfully."
            )
            if ack := context.get("acknowledgement_number"):
                lines.append(f"Acknowledgement number: {ack}")
            closing = "No action is needed from you on this filing."
        case "fee_reminder":
            lines.append(
                f"This is a gentle reminder about invoice {context.get('invoice_number', '')}"
                f" for ₹{context.get('amount_inr', '—')}, which is now due."
            )
            closing = "Please arrange payment at your earliest convenience."
        case _:
            lines.append(
                f"This is a reminder regarding {context.get('compliance', 'your compliance')}"
                f" due on {context.get('due_date', 'the upcoming deadline')}."
            )
            closing = "Please let us know if you need anything from us to meet it."
    lines += ["", closing, "", "Regards,", firm_name or DEFAULT_SIGNATORY]
    return "\n".join(lines)
