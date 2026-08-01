"""Idempotent seeding of reference data."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.compliance import ComplianceType
from app.seeds.compliance_types import COMPLIANCE_TYPE_SEEDS

SEED_FIELDS = (
    "name",
    "description",
    "category",
    "frequency",
    "form_number",
    "statutory_reference",
    "due_day",
    "due_month_offset",
    "due_overrides",
    "applicability_rule",
    "default_fee_paise",
    "reminder_offsets_days",
    "required_documents",
)


def seed_compliance_types(db: Session, *, update_existing: bool = True) -> tuple[int, int]:
    """Insert (or refresh) the system compliance calendar.

    Returns ``(created, updated)``. Safe to run on every deploy.
    """
    existing = {
        ct.code: ct
        for ct in db.scalars(select(ComplianceType).where(ComplianceType.firm_id.is_(None))).all()
    }
    created = updated = 0

    for seed in COMPLIANCE_TYPE_SEEDS:
        values = {key: seed.get(key) for key in SEED_FIELDS}
        values["due_overrides"] = seed.get("due_overrides") or {}
        values["reminder_offsets_days"] = seed.get("reminder_offsets_days") or []
        values["required_documents"] = seed.get("required_documents") or []

        current = existing.get(seed["code"])
        if current is None:
            db.add(ComplianceType(code=seed["code"], is_system=True, is_active=True, **values))
            created += 1
            continue

        if update_existing:
            changed = False
            for key, value in values.items():
                if getattr(current, key) != value:
                    setattr(current, key, value)
                    changed = True
            if changed:
                updated += 1

    db.flush()
    return created, updated
