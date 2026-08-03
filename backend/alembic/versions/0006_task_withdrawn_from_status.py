"""Remember what a task was before its filing stopped being owed.

A task is the human job of discharging one compliance item. Closing the item —
off-boarding the client, or dropping a registration the client no longer holds
— left the task open: on someone's queue, counted in the workload, and going
overdue against a deadline nobody owes.

Cancelling it needs the same memory ``compliance_items.offboarded_from_status``
holds, and for the same reason. Task generation skips a compliance item that
already has *any* task, open or closed, so a cancelled task is never replaced.
Without recording that this cancellation was ours, a client taken back on gets
their filings back with no work raised against them for good, and a cancelled
task is otherwise indistinguishable from one a manager cancelled deliberately.

Nullable with no backfill, for the same reason 0005 had none: tasks already
cancelled predate the distinction, and reopening them would resurrect work a
manager decided against.

Revision ID: 0006_task_withdrawn
Revises: 0005_offboarded
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_task_withdrawn"
down_revision: str | None = "0005_offboarded"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("withdrawn_from_status", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "withdrawn_from_status")
