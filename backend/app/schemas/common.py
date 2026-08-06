"""Shared schema base classes and validators."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Any, Generic, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
)

from app.core.security import MAX_PASSWORD_BYTES

T = TypeVar("T")

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")

# C0/C1 controls except tab and newline, plus DEL. These have no business in a
# client name or an invoice note: they corrupt log lines, CSV exports and
# terminal output, and a stray NUL byte is rejected outright by PostgreSQL's
# text type. Newline and tab survive because notes and addresses are multiline.
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Zero-width and bidirectional-override characters: invisible in a UI, but
# they let one string render as another (the "Trojan Source" trick).
INVISIBLE_CHARS = re.compile(r"[​-‏‪-‮⁦-⁩﻿]")

# Whitespace is meaningful in a secret, so these are passed through untouched.
UNSANITISED_FIELDS = frozenset({"password", "owner_password", "new_password", "token"})

# ₹1,000 crore, as an upper bound on any single amount a client can send.
#
# This is not a business rule — no CA bills that on one line — it is what keeps
# an absurd number from reaching a BIGINT column. Money columns are 64-bit, and
# an invoice multiplies an amount by a quantity (up to 10,000) across up to 200
# lines before adding GST. At this ceiling the worst case is ~2.4e18, inside the
# 9.2e18 a BIGINT holds; without it, an out-of-range amount is a 500 from the
# database driver rather than a 422 naming the field.
MAX_AMOUNT_PAISE = 1_000_000_000_000


def sanitize_text(value: str) -> str:
    """Strip characters that are invisible, control, or display-spoofing.

    Applied to every inbound string field. Unicode is normalised to NFC first
    so visually identical inputs compare and store identically.
    """
    cleaned = unicodedata.normalize("NFC", value)
    cleaned = CONTROL_CHARS.sub("", cleaned)
    cleaned = INVISIBLE_CHARS.sub("", cleaned)
    return cleaned.strip()


class SanitizedModel(BaseModel):
    """Base for every schema: cleans string input before field validation.

    ``mode="before"`` matters — the sanitised value is what length limits and
    the PAN/GSTIN patterns then see, so " AAACS1234F\\u200b" is accepted as the
    PAN it looks like rather than rejected for characters nobody can see.
    """

    @field_validator("*", mode="before")
    @classmethod
    def _sanitize_strings(cls, value: Any, info: ValidationInfo) -> Any:
        if isinstance(value, str) and info.field_name not in UNSANITISED_FIELDS:
            return sanitize_text(value)
        return value


class ORMModel(SanitizedModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class Message(BaseModel):
    detail: str


def refuse_null(value: Any, info: ValidationInfo) -> Any:
    """Refuse an explicit ``null`` on a field that has no cleared state.

    Every PATCH body is a model of optionals, and the optionality carries two
    different meanings that nothing separated. ``pan`` is optional because a
    client may not have one — sending ``null`` clears it, which is an
    instruction. ``name`` is optional because a PATCH need not name it — the
    column is ``NOT NULL``, so ``null`` is not an instruction at all, it is a
    value the record cannot hold.

    ``exclude_unset`` cannot tell them apart: a field explicitly set to ``null``
    is set, so it reached the handler and was written. What came back depended
    only on which column it was:

    * ``PATCH /compliance/items/{id}`` with ``{"status": "filed", "due_date":
      null}`` cleared the deadline in memory and then compared the filing date
      against it to decide filed-versus-delayed — ``date > None`` — so the
      request came back a 500 with a request id and nothing naming the field.
      ``PATCH /tasks/{id}`` with ``{"status": null}`` did the same on the way
      into the audit summary;
    * ``PATCH /clients/{id}`` with ``{"name": null}`` or ``{"is_active": null}``
      reached the database and came back a 409 saying the change "conflicts with
      an existing record" — which is what a duplicate PAN says, and is not what
      happened. A caller reading it goes looking for the record it collided
      with.

    Neither is a shape a UI sends on purpose; both are what a client library
    serialising an absent field as ``null`` produces, which is the ordinary way
    to reach this. Answered as a 422 naming the field, the way every other
    validation failure is, and the field's own name says which one it was.
    """
    if value is None:
        raise ValueError(
            f"{info.field_name} cannot be set to null — omit it to leave it unchanged"
        )
    return value


def not_clearable(*fields: str):
    """A validator refusing ``null`` on each of ``fields``. See :func:`refuse_null`.

    Assigned into a PATCH schema alongside its other validators. ``mode="before"``
    on a *named* field only runs when the caller supplied that field, so an
    omitted field is untouched and the default still applies — which is the
    distinction this exists to draw.
    """
    return field_validator(*fields, mode="before")(refuse_null)


def validate_pan(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    value = value.strip().upper()
    if not PAN_RE.match(value):
        raise ValueError("PAN must look like AAAAA9999A")
    return value


def validate_gstin(value: str | None) -> str | None:
    """Check a GSTIN's shape, its state code, and its own check digit.

    The pattern alone accepts a great many strings the GST portal does not. Two
    of them are worth refusing here, because both are what a mistyped GSTIN
    looks like and neither is discoverable afterwards from anything the firm
    holds:

    * the leading two digits are a *state code*, and only some numbers are one.
      ``00`` and ``40`` match the pattern and name no state — and this field is
      not merely stored: it is what decides whether a supply to this client is
      taxed as CGST+SGST or as IGST, so a code naming no state silently takes
      the invoice down the undetermined path;
    * the fifteenth character is a check digit over the other fourteen. It
      exists precisely so that a transcription error is caught at the point of
      entry, and checking it catches every single-character slip and every
      transposition of two adjacent characters.

    Both matter more than they look, because a wrong GSTIN is not a wrong label
    on a record. It goes onto the firm's GSTR-1 as the counterparty, where it
    either fails validation at the portal — after the return is prepared — or
    matches nobody, and the client cannot then claim the credit for a bill they
    have already paid. The firm hears about it from the client.

    The message names which of the two failed, since a wrong state code and a
    wrong character are different mistakes to go looking for.
    """
    if value is None or value == "":
        return None
    value = value.strip().upper()
    if not GSTIN_RE.match(value):
        raise ValueError("GSTIN must be a valid 15-character GST identification number")

    # Imported here rather than at module scope: the schemas are imported by the
    # models' own consumers, and app.services.gst imports app.models.base.
    from app.services.gst import STATE_CODES, gstin_checksum_valid

    if value[:2] not in STATE_CODES:
        raise ValueError(
            f"GSTIN starts with {value[:2]}, which is not a GST state code — "
            "the first two digits are the state the client is registered in"
        )
    if not gstin_checksum_valid(value):
        raise ValueError(
            "GSTIN failed its own check digit — the last character does not match "
            "the other fourteen, so at least one of them has been mistyped"
        )
    return value


def validate_tan(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    value = value.strip().upper()
    if not TAN_RE.match(value):
        raise ValueError("TAN must look like AAAA99999A")
    return value


def within_bcrypt_limit(value: str) -> str:
    """Refuse a password bcrypt could not hash whole.

    bcrypt takes at most 72 *bytes* and truncates silently past that, so
    :func:`~app.core.security.hash_password` refuses a longer one outright. A
    character limit is a different limit: UTF-8 spends three bytes on every
    Devanagari letter and two on every accented Latin one, so a 30-character
    Hindi passphrase is ninety bytes — well inside a 72-character cap and well
    outside what can be hashed.

    Nothing checked the bytes before the hash was attempted, so that
    passphrase reached ``hash_password``, raised, and came back as a 500 with
    the generic "something went wrong on our side" and a request id. Nothing
    named the field, nothing said a shorter one would work, and the firm being
    turned away was signing up. The one class of user it hit is the one whose
    password is not in ASCII — which, for a product sold to Indian
    accountants, is not an edge.

    Checked here rather than only in the hasher because this is where a caller
    is told which field is wrong and why. The hasher keeps its own check: it
    is what makes silent truncation impossible for any caller, including one
    that never went through a schema.
    """
    if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes. Accented and "
            "Indic characters count as two or three bytes each, so this one is "
            "longer than it looks — please shorten it."
        )
    return value


# Every field a caller sets a password through. The byte check rides on the
# type rather than being attached per field, so a new password field cannot be
# added without it.
Password = Annotated[
    str,
    Field(min_length=8, max_length=MAX_PASSWORD_BYTES),
    AfterValidator(within_bcrypt_limit),
]


# A client's per-service fee overrides: {"<compliance_type_code>": <paise>}.
#
# This was the one money field with no bound on it, and it is not a field the
# caller merely stores — ``generate_compliance_items`` copies the amount onto
# every filing it materialises for that client, and from there it reaches the
# dashboard's unbilled total and the invoice line the client is billed for.
#
# All three halves were open:
#
# * the *amount* had no ceiling, so ``{"GSTR3B_MONTHLY": 10**25}`` reached a
#   BIGINT column and came back a 500 from the database driver — precisely
#   what ``MAX_AMOUNT_PAISE`` exists to turn into a 422 naming the field. It
#   also had no floor, and a negative fee is worse than an oversized one
#   because nothing rejects it: it lands on a year of filings, and the
#   dashboard's "unbilled" figure then *subtracts* it from what the firm is
#   owed, so the number a practice reads to find its own missing revenue is
#   quietly wrong in the direction of looking fine;
# * the *key* was free text, so a megabyte of it could be stored per entry in
#   a JSON column read on every visit to the client screen. A code is this
#   system's own vocabulary — the ``ComplianceType.code`` values — and nothing
#   longer than the column that holds them can ever match one;
# * the *dict* had no size limit at all, so twenty thousand entries went in on
#   one request and were read back on every one after it.
#
# Bounded as a type rather than per field, so both the create and the update
# path get it and a third cannot be added without.
MAX_SERVICE_FEE_OVERRIDES = 200
MAX_SERVICE_CODE_LENGTH = 64

ServiceFeeCode = Annotated[
    str,
    Field(min_length=1, max_length=MAX_SERVICE_CODE_LENGTH, pattern=r"^[A-Za-z0-9._-]+$"),
]
ServiceFeeAmount = Annotated[int, Field(ge=0, le=MAX_AMOUNT_PAISE)]
ServiceFees = Annotated[
    dict[ServiceFeeCode, ServiceFeeAmount],
    Field(max_length=MAX_SERVICE_FEE_OVERRIDES),
]


# The facts a practitioner adds to an AI-drafted message, and the message body
# itself. Both are free-form on purpose, and neither had any bound at all.
#
# ``extra_context`` is the sharper of the two, because it is not merely stored.
# ``draft_client_message`` renders every entry into the prompt as
# ``- <key>: <value>`` and posts it to OpenRouter, so an unbounded dict is an
# unbounded *outbound* request — as large as the body limit allows, twenty-five
# megabytes, on a paid API, from any authenticated practitioner including a
# junior, at whatever rate the default bucket permits. Nothing downstream
# noticed: the model is asked for at most 400 tokens back, so the cost and the
# latency sit entirely on the side nothing was counting. A nested structure did
# the same in less space, since ``{key}: {value}`` renders a whole object.
#
# So the shape is pinned as well as the size: a context entry is a fact about a
# filing or an invoice — a date, a number, a name — and a scalar is what that
# is. A list or an object arriving here is not a caller filling in details, and
# the two server-built contexts (:mod:`app.services.reminders`) are assembled
# after validation and are unaffected either way.
#
# ``body`` is the plainer half. It is a ``TEXT`` column, so it took whatever
# arrived and the dispatcher then handed it to SMTP: a twenty-five megabyte
# reminder is a row that is read back on every listing and a message no relay
# will accept. Generous rather than tight — a practitioner pasting a long
# statement of account into a message is doing ordinary work.
MAX_DRAFT_CONTEXT_ENTRIES = 25
MAX_DRAFT_CONTEXT_KEY = 64
MAX_DRAFT_CONTEXT_VALUE = 500
MAX_MESSAGE_BODY = 20_000

DraftContextKey = Annotated[str, Field(min_length=1, max_length=MAX_DRAFT_CONTEXT_KEY)]
DraftContextValue = (
    Annotated[str, Field(max_length=MAX_DRAFT_CONTEXT_VALUE)] | int | float | bool | None
)
DraftContext = Annotated[
    dict[DraftContextKey, DraftContextValue],
    Field(max_length=MAX_DRAFT_CONTEXT_ENTRIES),
]
MessageBody = Annotated[str, Field(max_length=MAX_MESSAGE_BODY)]
