"""Helper for writing audit-trail entries."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.firm import Practitioner


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    actor: Practitioner | None = None,
    actor_label: str | None = None,
    firm_id: uuid.UUID | None = None,
    summary: str | None = None,
    changes: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    """Append one entry. ``actor_label`` names a non-practitioner actor —
    a client acting through the portal, or a background job."""
    entry = AuditLog(
        firm_id=firm_id or (actor.firm_id if actor else None),
        actor_practitioner_id=actor.id if actor else None,
        actor_label=(
            f"{actor.full_name} <{actor.email}>" if actor else actor_label
        ),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
        changes=changes or {},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(entry)
    return entry


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Reduce two snapshots to only the fields that actually changed."""
    changed_before, changed_after = {}, {}
    for key in set(before) | set(after):
        old, new = before.get(key), after.get(key)
        if old != new:
            changed_before[key] = _jsonable(old)
            changed_after[key] = _jsonable(new)
    if not changed_after:
        return {}
    return {"before": changed_before, "after": changed_after}


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool, type(None), dict, list)):
        return value
    return str(value)
