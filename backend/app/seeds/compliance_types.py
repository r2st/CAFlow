"""The pre-loaded Indian statutory compliance calendar.

Each entry is a system compliance type (``firm_id IS NULL``) visible to every
firm. ``applicability_rule`` decides which clients get items generated for it
(see ``app.services.applicability``).

Due dates are expressed as: day ``due_day`` of the month ``due_month_offset``
months after the period ends, with per-period overrides where the statute
deviates (e.g. TDS Q4, TDS payment for March).

NOTE: these are the ordinary statutory dates. CBIC/CBDT extensions are
common; a firm can edit any date on the generated compliance item.
"""

from __future__ import annotations

from typing import Any

COMPLIANCE_TYPE_SEEDS: list[dict[str, Any]] = [
    # ------------------------------------------------------------------ GST --
    {
        "code": "GSTR1_MONTHLY",
        "name": "GSTR-1 (Monthly) — Outward supplies",
        "description": "Monthly statement of outward supplies for regular GST taxpayers.",
        "category": "gst",
        "frequency": "monthly",
        "form_number": "GSTR-1",
        "statutory_reference": "Sec 37, CGST Act 2017 / Rule 59",
        "due_day": 11,
        "due_month_offset": 1,
        "applicability_rule": "gst_monthly",
        "default_fee_paise": 150_000,
        "reminder_offsets_days": [7, 3, 1],
        "required_documents": ["sales_invoice", "credit_debit_notes", "export_invoices"],
    },
    {
        "code": "GSTR3B_MONTHLY",
        "name": "GSTR-3B (Monthly) — Summary return & tax payment",
        "description": "Monthly summary return with self-assessed tax payment.",
        "category": "gst",
        "frequency": "monthly",
        "form_number": "GSTR-3B",
        "statutory_reference": "Sec 39, CGST Act 2017 / Rule 61",
        "due_day": 20,
        "due_month_offset": 1,
        "applicability_rule": "gst_monthly",
        "default_fee_paise": 200_000,
        "reminder_offsets_days": [10, 5, 2, 1],
        "required_documents": ["sales_invoice", "purchase_invoice", "bank_statement"],
    },
    {
        "code": "GSTR1_QUARTERLY",
        "name": "GSTR-1 (Quarterly, QRMP) — Outward supplies",
        "description": "Quarterly outward supplies statement under the QRMP scheme.",
        "category": "gst",
        "frequency": "quarterly",
        "form_number": "GSTR-1",
        "statutory_reference": "Rule 59(1), CGST Rules — QRMP scheme",
        "due_day": 13,
        "due_month_offset": 1,
        "applicability_rule": "gst_quarterly",
        "default_fee_paise": 300_000,
        "reminder_offsets_days": [10, 5, 2],
        "required_documents": ["sales_invoice", "credit_debit_notes"],
    },
    {
        "code": "GSTR3B_QUARTERLY",
        "name": "GSTR-3B (Quarterly, QRMP) — Summary return",
        "description": "Quarterly summary return under the QRMP scheme.",
        "category": "gst",
        "frequency": "quarterly",
        "form_number": "GSTR-3B",
        "statutory_reference": "Rule 61, CGST Rules — QRMP scheme",
        "due_day": 22,
        "due_month_offset": 1,
        "applicability_rule": "gst_quarterly",
        "default_fee_paise": 350_000,
        "reminder_offsets_days": [10, 5, 2],
        "required_documents": ["sales_invoice", "purchase_invoice", "bank_statement"],
    },
    {
        "code": "GSTR9_ANNUAL",
        "name": "GSTR-9 — Annual GST return",
        "description": "Annual consolidated GST return.",
        "category": "gst",
        "frequency": "annual",
        "form_number": "GSTR-9",
        "statutory_reference": "Sec 44, CGST Act 2017",
        "due_day": 31,
        "due_month_offset": 9,  # FY ends 31 Mar -> due 31 Dec
        "applicability_rule": "gst_registered",
        "default_fee_paise": 1_500_000,
        "reminder_offsets_days": [30, 15, 7, 3],
        "required_documents": ["gst_return", "balance_sheet", "profit_and_loss"],
    },
    # ------------------------------------------------------------------ TDS --
    {
        "code": "TDS_PAYMENT_MONTHLY",
        "name": "TDS payment — monthly challan",
        "description": "Deposit of tax deducted at source for the month.",
        "category": "tds",
        "frequency": "monthly",
        "form_number": "ITNS-281",
        "statutory_reference": "Rule 30, Income-tax Rules 1962",
        "due_day": 7,
        "due_month_offset": 1,
        # March deductions get an extended 30 April deadline.
        "due_overrides": {"03": {"month_offset": 1, "day": 30}},
        "applicability_rule": "tds_applicable",
        "default_fee_paise": 100_000,
        "reminder_offsets_days": [5, 2, 1],
        "required_documents": ["tds_challan", "bank_statement"],
    },
    {
        "code": "TDS_RETURN_24Q",
        "name": "TDS return 24Q — salaries (quarterly)",
        "description": "Quarterly TDS statement for salary payments.",
        "category": "tds",
        "frequency": "quarterly",
        "form_number": "24Q",
        "statutory_reference": "Rule 31A, Income-tax Rules 1962",
        "due_day": 31,
        "due_month_offset": 1,
        # Q1 -> 31 Jul, Q2 -> 31 Oct, Q3 -> 31 Jan, Q4 -> 31 May.
        "due_overrides": {"Q4": {"month_offset": 2, "day": 31}},
        "applicability_rule": "tds_applicable",
        "default_fee_paise": 400_000,
        "reminder_offsets_days": [15, 7, 3, 1],
        "required_documents": ["salary_register", "tds_challan", "form_16"],
    },
    {
        "code": "TDS_RETURN_26Q",
        "name": "TDS return 26Q — non-salary (quarterly)",
        "description": "Quarterly TDS statement for payments other than salary.",
        "category": "tds",
        "frequency": "quarterly",
        "form_number": "26Q",
        "statutory_reference": "Rule 31A, Income-tax Rules 1962",
        "due_day": 31,
        "due_month_offset": 1,
        "due_overrides": {"Q4": {"month_offset": 2, "day": 31}},
        "applicability_rule": "tds_applicable",
        "default_fee_paise": 400_000,
        "reminder_offsets_days": [15, 7, 3, 1],
        "required_documents": ["tds_challan", "purchase_invoice"],
    },
    # ----------------------------------------------------------- Income tax --
    {
        "code": "ITR_NON_AUDIT",
        "name": "Income tax return — non-audit cases",
        "description": "Annual income tax return where a tax audit is not applicable.",
        "category": "income_tax",
        "frequency": "annual",
        "form_number": "ITR",
        "statutory_reference": "Sec 139(1), Income-tax Act 1961",
        "due_day": 31,
        "due_month_offset": 4,  # FY ends 31 Mar -> due 31 Jul of the assessment year
        "applicability_rule": "income_tax_non_audit",
        "default_fee_paise": 500_000,
        "reminder_offsets_days": [45, 30, 15, 7, 3],
        "required_documents": ["form_16", "form_26as", "ais_tis", "bank_statement"],
    },
    {
        "code": "ITR_AUDIT",
        "name": "Income tax return — audit cases",
        "description": "Annual income tax return where a tax audit under Sec 44AB applies.",
        "category": "income_tax",
        "frequency": "annual",
        "form_number": "ITR",
        "statutory_reference": "Sec 139(1), Income-tax Act 1961",
        "due_day": 31,
        "due_month_offset": 7,  # 31 Oct
        "applicability_rule": "income_tax_audit",
        "default_fee_paise": 2_500_000,
        "reminder_offsets_days": [45, 30, 15, 7, 3],
        "required_documents": ["balance_sheet", "profit_and_loss", "form_26as", "bank_statement"],
    },
    {
        "code": "TAX_AUDIT_3CD",
        "name": "Tax audit report (Form 3CA/3CB-3CD)",
        "description": "Tax audit report under Sec 44AB.",
        "category": "audit",
        "frequency": "annual",
        "form_number": "3CD",
        "statutory_reference": "Sec 44AB, Income-tax Act 1961",
        "due_day": 30,
        "due_month_offset": 6,  # 30 Sep
        "applicability_rule": "income_tax_audit",
        "default_fee_paise": 3_500_000,
        "reminder_offsets_days": [45, 30, 15, 7],
        "required_documents": ["balance_sheet", "profit_and_loss", "bank_statement"],
    },
    {
        "code": "ADVANCE_TAX",
        "name": "Advance tax instalment",
        "description": "Quarterly advance tax instalment (15%/45%/75%/100% cumulative).",
        "category": "income_tax",
        "frequency": "quarterly",
        "form_number": "ITNS-280",
        "statutory_reference": "Sec 211, Income-tax Act 1961",
        "due_day": 15,
        "due_month_offset": 0,  # falls inside the quarter: 15 Jun / Sep / Dec / Mar
        "applicability_rule": "advance_tax",
        "default_fee_paise": 150_000,
        "reminder_offsets_days": [10, 5, 2],
        "required_documents": ["bank_statement", "profit_and_loss"],
    },
    # ------------------------------------------------------------------ ROC --
    {
        "code": "ROC_AOC4",
        "name": "ROC annual filing — AOC-4 (financial statements)",
        "description": "Filing of financial statements with the Registrar of Companies.",
        "category": "roc",
        "frequency": "annual",
        "form_number": "AOC-4",
        "statutory_reference": "Sec 137, Companies Act 2013",
        "due_day": 30,
        "due_month_offset": 7,  # 30 Oct — 30 days after a 30 Sep AGM
        "applicability_rule": "roc_company",
        "default_fee_paise": 1_200_000,
        "reminder_offsets_days": [45, 30, 15, 7],
        "required_documents": ["balance_sheet", "profit_and_loss", "incorporation_doc"],
    },
    {
        "code": "ROC_MGT7",
        "name": "ROC annual filing — MGT-7 (annual return)",
        "description": "Annual return filing with the Registrar of Companies.",
        "category": "roc",
        "frequency": "annual",
        "form_number": "MGT-7",
        "statutory_reference": "Sec 92, Companies Act 2013",
        "due_day": 29,
        "due_month_offset": 8,  # 29 Nov — 60 days after a 30 Sep AGM
        "applicability_rule": "roc_company",
        "default_fee_paise": 1_200_000,
        "reminder_offsets_days": [45, 30, 15, 7],
        "required_documents": ["incorporation_doc"],
    },
    {
        "code": "ROC_LLP_FORM11",
        "name": "LLP annual return — Form 11",
        "description": "Annual return of an LLP.",
        "category": "roc",
        "frequency": "annual",
        "form_number": "Form 11",
        "statutory_reference": "Sec 35, LLP Act 2008",
        "due_day": 30,
        "due_month_offset": 2,  # 30 May
        "applicability_rule": "roc_llp",
        "default_fee_paise": 800_000,
        "reminder_offsets_days": [30, 15, 7],
        "required_documents": ["incorporation_doc"],
    },
    {
        "code": "ROC_LLP_FORM8",
        "name": "LLP statement of account & solvency — Form 8",
        "description": "Annual statement of account and solvency of an LLP.",
        "category": "roc",
        "frequency": "annual",
        "form_number": "Form 8",
        "statutory_reference": "Sec 34, LLP Act 2008",
        "due_day": 30,
        "due_month_offset": 7,  # 30 Oct
        "applicability_rule": "roc_llp",
        "default_fee_paise": 800_000,
        "reminder_offsets_days": [30, 15, 7],
        "required_documents": ["balance_sheet", "profit_and_loss"],
    },
    # -------------------------------------------------------------- Payroll --
    {
        "code": "PF_MONTHLY",
        "name": "Provident Fund — monthly ECR",
        "description": "Monthly PF contribution and electronic challan-cum-return.",
        "category": "payroll",
        "frequency": "monthly",
        "form_number": "ECR",
        "statutory_reference": "Para 38, EPF Scheme 1952",
        "due_day": 15,
        "due_month_offset": 1,
        "applicability_rule": "payroll",
        "default_fee_paise": 150_000,
        "reminder_offsets_days": [7, 3, 1],
        "required_documents": ["salary_register", "bank_statement"],
    },
    {
        "code": "ESI_MONTHLY",
        "name": "ESI — monthly contribution",
        "description": "Monthly Employees' State Insurance contribution.",
        "category": "payroll",
        "frequency": "monthly",
        "form_number": "ESIC",
        "statutory_reference": "Reg 31, ESI (General) Regulations 1950",
        "due_day": 15,
        "due_month_offset": 1,
        "applicability_rule": "payroll",
        "default_fee_paise": 100_000,
        "reminder_offsets_days": [7, 3, 1],
        "required_documents": ["salary_register", "bank_statement"],
    },
]


def seed_by_code(code: str) -> dict[str, Any]:
    for seed in COMPLIANCE_TYPE_SEEDS:
        if seed["code"] == code:
            return seed
    raise KeyError(code)
