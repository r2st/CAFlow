"""Indexes on the columns business queries actually filter and sort by.

Every listing in the app narrows by ``firm_id`` (the tenant boundary) and then
by one of status, due date or created-at. The single-column indexes from the
initial schema get the planner to the right tenant but leave it sorting or
re-scanning from there; these composites carry the sort key as well.

Created CONCURRENTLY on PostgreSQL so applying this to a live database does
not take an ACCESS EXCLUSIVE lock on tables the API is reading. That requires
running outside a transaction, hence the autocommit block below.

Revision ID: 0003_indexes
Revises: 0002_portal
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_indexes"
down_revision: str | None = "0002_portal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (index name, table, columns)
INDEXES: list[tuple[str, str, list[str]]] = [
    # --- compliance items: the calendar, the dashboard, the portal ---
    ("ix_compliance_item_firm_status_due", "compliance_items", ["firm_id", "status", "due_date"]),
    ("ix_compliance_item_client_due", "compliance_items", ["client_id", "due_date"]),
    (
        "ix_compliance_item_assignee_due",
        "compliance_items",
        ["assigned_practitioner_id", "due_date"],
    ),
    # --- documents: newest-first lists and checklist resolution ---
    ("ix_document_firm_created", "documents", ["firm_id", "created_at"]),
    ("ix_document_client_created", "documents", ["client_id", "created_at"]),
    ("ix_document_firm_category", "documents", ["firm_id", "category"]),
    ("ix_document_item_category", "documents", ["compliance_item_id", "category"]),
    # --- tasks ---
    ("ix_task_firm_due", "tasks", ["firm_id", "due_date"]),
    ("ix_task_firm_client_status", "tasks", ["firm_id", "client_id", "status"]),
    # --- invoices: ageing and per-client balance ---
    ("ix_invoice_firm_due", "invoices", ["firm_id", "due_date"]),
    ("ix_invoice_client_status", "invoices", ["client_id", "status"]),
    # --- reminders ---
    ("ix_reminder_firm_status_scheduled", "reminders", ["firm_id", "status", "scheduled_for"]),
    ("ix_reminder_client_type", "reminders", ["client_id", "reminder_type", "scheduled_for"]),
    # --- clients ---
    ("ix_client_firm_active", "clients", ["firm_id", "is_active"]),
    ("ix_client_firm_portal", "clients", ["firm_id", "portal_enabled"]),
]

# Sign-in compares lower(email); a plain index on email cannot serve it.
EMAIL_LOWER_INDEX = "ix_practitioner_email_lower"


def upgrade() -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"

    if postgres:
        # CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
        with op.get_context().autocommit_block():
            for name, table, columns in INDEXES:
                op.create_index(name, table, columns, postgresql_concurrently=True)
            op.create_index(
                EMAIL_LOWER_INDEX,
                "practitioners",
                [sa.text("lower(email)")],
                postgresql_concurrently=True,
            )
        return

    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)
    op.create_index(EMAIL_LOWER_INDEX, "practitioners", [sa.text("lower(email)")])


def downgrade() -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"

    if postgres:
        with op.get_context().autocommit_block():
            op.drop_index(EMAIL_LOWER_INDEX, table_name="practitioners", postgresql_concurrently=True)
            for name, table, _ in reversed(INDEXES):
                op.drop_index(name, table_name=table, postgresql_concurrently=True)
        return

    op.drop_index(EMAIL_LOWER_INDEX, table_name="practitioners")
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
