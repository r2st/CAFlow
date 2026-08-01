"""Rules deciding which compliance types apply to which client.

Each compliance type carries an ``applicability_rule`` key; the predicate
registered under that key is evaluated against the client's registration flags.
Unknown keys are treated as "not applicable" rather than raising, so a firm can
add a custom type without a matching predicate and simply attach items manually.
"""

from __future__ import annotations

from collections.abc import Callable

from app.models.base import EntityType, GSTFilingFrequency
from app.models.client import Client

Predicate = Callable[[Client], bool]

_COMPANY_TYPES = frozenset({EntityType.PRIVATE_LIMITED, EntityType.PUBLIC_LIMITED})
_ADVANCE_TAX_EXEMPT_TYPES = frozenset({EntityType.INDIVIDUAL, EntityType.HUF})


def _gst_monthly(client: Client) -> bool:
    return client.gst_registered and client.gst_filing_frequency == GSTFilingFrequency.MONTHLY


def _gst_quarterly(client: Client) -> bool:
    return client.gst_registered and client.gst_filing_frequency == GSTFilingFrequency.QUARTERLY


def _advance_tax(client: Client) -> bool:
    if not client.income_tax_applicable:
        return False
    return client.tax_audit_applicable or client.entity_type not in _ADVANCE_TAX_EXEMPT_TYPES


APPLICABILITY_RULES: dict[str, Predicate] = {
    "always": lambda client: True,
    "never": lambda client: False,
    "gst_registered": lambda client: client.gst_registered,
    "gst_monthly": _gst_monthly,
    "gst_quarterly": _gst_quarterly,
    "tds_applicable": lambda client: client.tds_applicable,
    "income_tax": lambda client: client.income_tax_applicable,
    "income_tax_non_audit": lambda client: (
        client.income_tax_applicable and not client.tax_audit_applicable
    ),
    "income_tax_audit": lambda client: (
        client.income_tax_applicable and client.tax_audit_applicable
    ),
    "advance_tax": _advance_tax,
    "roc": lambda client: client.roc_required,
    "roc_company": lambda client: client.roc_required and client.entity_type in _COMPANY_TYPES,
    "roc_llp": lambda client: client.entity_type == EntityType.LLP,
    "payroll": lambda client: client.payroll_applicable,
}


def applies_to(rule: str, client: Client) -> bool:
    predicate = APPLICABILITY_RULES.get(rule)
    return bool(predicate and predicate(client))
