"""Audit-trail read schemas.

The trail itself is append-only and written by ``app.services.audit``; nothing
here can create or amend an entry, which is the point of having one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.schemas.common import ORMModel, SanitizedModel


class AuditLogOut(ORMModel):
    id: uuid.UUID
    firm_id: uuid.UUID | None
    actor_practitioner_id: uuid.UUID | None
    # "Anita Sharma <anita@…>" for a practitioner, or a label naming a client
    # acting through the portal or a background job.
    actor_label: str | None
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    summary: str | None
    # {"before": {...}, "after": {...}}, holding only the fields that changed.
    changes: dict
    ip_address: str | None
    user_agent: str | None
    created_at: datetime


class AuditActionOut(SanitizedModel):
    action: str
    count: int


class AuditActionsResponse(SanitizedModel):
    """The actions this firm has actually recorded, for a filter list.

    Enumerating the codebase's action strings would offer filters that return
    nothing; these are the ones with entries behind them.
    """

    actions: list[AuditActionOut]
    entity_types: list[str]
