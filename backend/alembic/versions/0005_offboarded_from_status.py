"""Remember what a filing was before off-boarding closed it.

Deactivating a client rewrites every open filing to ``not_applicable``, which
is right while the firm is not acting for them — but it is the same value a
practitioner sets by hand to say "this client does not file this", and the two
were indistinguishable afterwards. Taking the client back on could therefore
restore nothing, and a client on the books with a plan slot charged for them
had no compliance calendar at all.

This column is that distinction: non-null means off-boarding closed the item
and names the status to put back.

Nullable with no backfill, deliberately. Rows already closed by an off-boarding
that happened before this migration are indistinguishable from hand-marked ones
— that is exactly the information that was never recorded — and inventing a
value for them would resurrect filings the firm had deliberately ruled out.
They stay as they are; the firm can reopen them by hand.

Revision ID: 0005_offboarded
Revises: 0004_unique_email
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_offboarded"
down_revision: str | None = "0004_unique_email"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "compliance_items",
        sa.Column("offboarded_from_status", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("compliance_items", "offboarded_from_status")
