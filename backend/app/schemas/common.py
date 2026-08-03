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


def validate_pan(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    value = value.strip().upper()
    if not PAN_RE.match(value):
        raise ValueError("PAN must look like AAAAA9999A")
    return value


def validate_gstin(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    value = value.strip().upper()
    if not GSTIN_RE.match(value):
        raise ValueError("GSTIN must be a valid 15-character GST identification number")
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
