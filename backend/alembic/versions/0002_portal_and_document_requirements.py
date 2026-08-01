"""Client portal access and document requirement tracking

Adds passwordless portal access to clients (enable/disable plus a revocation
cut-off for already-issued magic links), and the fields on documents that let a
checklist know which requirement an upload answered and whether the client may
see it.

Revision ID: 0002_portal
Revises: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0002_portal"
down_revision: str | None = "0001_initial"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("portal_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "clients", sa.Column("portal_token_valid_from", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "clients", sa.Column("portal_last_seen_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column(
        "documents",
        sa.Column(
            "satisfies_requirements",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "uploaded_via_portal", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "is_shared_with_client", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )

    # The server defaults exist only to backfill existing rows; the application
    # supplies these values on insert, so drop them again. SQLite has no
    # ALTER COLUMN, and a lingering default there is harmless — the columns are
    # NOT NULL and always written explicitly — so this is Postgres-only.
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column("clients", "portal_enabled", server_default=None)
    op.alter_column("documents", "satisfies_requirements", server_default=None)
    op.alter_column("documents", "uploaded_via_portal", server_default=None)
    op.alter_column("documents", "is_shared_with_client", server_default=None)


def downgrade() -> None:
    op.drop_column("documents", "is_shared_with_client")
    op.drop_column("documents", "uploaded_via_portal")
    op.drop_column("documents", "satisfies_requirements")
    op.drop_column("clients", "portal_last_seen_at")
    op.drop_column("clients", "portal_token_valid_from")
    op.drop_column("clients", "portal_enabled")
