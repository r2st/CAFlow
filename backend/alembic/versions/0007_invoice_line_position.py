"""Keep an invoice's lines in the order they were arranged in.

An invoice is a document of record, and the order of its lines is part of what
the client is holding a copy of. Nothing said what that order was: the
relationship loading ``invoices.lines`` emitted a bare ``SELECT`` with no
``ORDER BY``, and the row order that comes back from PostgreSQL for such a
query is unspecified. It is stable enough in practice to hide the problem —
until the table is vacuumed, a row is updated and moves, or the planner picks a
different scan — and then two reads of one invoice disagree about the order of
the same lines.

The order is the one thing about the lines a practitioner sets by hand, so
every reader losing it costs something: the rendered invoice, the client's copy
in the portal, and the editor, which reloads a draft's lines into rows keyed by
their position in the list.

Backfilled from ``id`` rather than left at the default, so existing invoices
keep *an* order instead of collapsing to a single tie broken elsewhere. That is
not necessarily the order they were entered in — nothing recorded it, and this
migration cannot invent it — but it is deterministic and stable from here on,
which is the property that was missing. ``id`` is a UUID, so this is arbitrary
with respect to entry order; for rows created before this column existed there
is nothing better to be had, and the alternative is leaving every line of every
historical invoice tied at zero.

New rows get their position from the caller's submitted order; see
``billing.set_lines`` and ``billing.build_invoice``. ``Invoice.lines`` orders by
``position, id`` so the ``id`` tiebreak also closes the order for any row this
backfill left sharing a value.

Revision ID: 0007_line_position
Revises: 0006_task_withdrawn
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_line_position"
down_revision: str | None = "0006_task_withdrawn"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Added with a server default so the column can be NOT NULL without a
    # separate backfill pass for rows being inserted while this runs.
    op.add_column(
        "invoice_lines",
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )

    # Number each invoice's existing lines from zero, in id order. Written as a
    # correlated subquery rather than an UPDATE ... FROM (SELECT row_number())
    # so it runs on SQLite as well as PostgreSQL.
    op.execute(
        """
        UPDATE invoice_lines AS l
        SET position = (
            SELECT COUNT(*)
            FROM invoice_lines AS earlier
            WHERE earlier.invoice_id = l.invoice_id
              AND earlier.id < l.id
        )
        """
    )

    # The default has done its job — it existed so the column could be NOT NULL
    # while rows were still arriving — and dropping it leaves the application
    # as the only thing that decides a line's position. The ORM still fills a
    # missing one from the model's own default, so this does not turn a
    # forgotten position into an error; what it prevents is the column carrying
    # a second, silent answer at the database level that nothing in the
    # application agrees to.
    op.alter_column("invoice_lines", "position", server_default=None)


def downgrade() -> None:
    op.drop_column("invoice_lines", "position")
