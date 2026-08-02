"""Shared schema base classes and validators."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

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


PasswordField = Field(min_length=8, max_length=72)
