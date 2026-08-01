"""Shared schema base classes and validators."""

from __future__ import annotations

import re
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")


class ORMModel(BaseModel):
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
