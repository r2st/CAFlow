"""Split an invoice's GST into CGST/SGST/IGST and record the place of supply.

An invoice carried one ``tax_paise`` and the rate it was struck at, which is
enough to total a bill and not enough to be a tax invoice. GST is levied either
as CGST plus SGST, when the supplier and the place of supply are in one state,
or as IGST when they are not — and which of the two it was is not recoverable
from a lump sum afterwards.

Three things needed it and none of them could have it: Rule 46 of the CGST
Rules requires the components and the place of supply on the face of the
document; GSTR-1 reports them separately and reconciles per invoice, so the
firm's own return could not be produced from its own ledger; and the client
claims input tax credit against the components, where CGST credit cannot offset
IGST. A client billed under the wrong head claims the wrong credit and hears
about it as a notice months later.

Backfilled as intra-state, which is what the existing rows are. Every one of
them was raised before a place of supply was resolved at all, so nothing on the
record says otherwise, and a practice's invoices are overwhelmingly to clients
in its own state — a CA firm's book of business is local almost by definition.
The alternative is leaving historical rows with a zero tax split against a
non-zero tax total, which is a document that does not add up. ``place_of_supply``
is deliberately left null on those rows rather than guessed at: it is the marker
saying this invoice was raised before the determination existed, and it is
distinguishable from a row where the determination ran and found nothing.

Revision ID: 0008_gst_split
Revises: 0007_line_position
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_gst_split"
down_revision: str | None = "0007_line_position"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Server defaults so each column can be NOT NULL without a separate backfill
    # pass for rows arriving while this runs; dropped below once they have done
    # that job, leaving the application as the only thing deciding a split.
    op.add_column(
        "invoices",
        sa.Column("cgst_paise", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "invoices",
        sa.Column("sgst_paise", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "invoices",
        sa.Column("igst_paise", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column("invoices", sa.Column("place_of_supply", sa.String(length=2), nullable=True))
    op.add_column(
        "invoices",
        sa.Column(
            "supply_type",
            sa.String(length=16),
            nullable=False,
            server_default="intra_state",
        ),
    )

    # Halve each existing tax total into CGST and SGST, giving the odd paise to
    # SGST exactly as ``gst.split_tax`` does, so a row backfilled here and a row
    # written by the application are indistinguishable. ``tax_paise`` is an
    # integer column and both backends divide integers as integers, so ``/``
    # truncates; deriving SGST by subtracting the truncated CGST back off the
    # total is what keeps the pair summing to ``tax_paise`` on either one.
    op.execute(
        """
        UPDATE invoices
        SET cgst_paise = tax_paise / 2,
            sgst_paise = tax_paise - (tax_paise / 2)
        WHERE tax_paise <> 0
        """
    )

    for column in ("cgst_paise", "sgst_paise", "igst_paise", "supply_type"):
        op.alter_column("invoices", column, server_default=None)


def downgrade() -> None:
    # ``tax_paise`` is unchanged by the upgrade and remains the sum of the three,
    # so dropping them loses the split but not a rupee of the total.
    op.drop_column("invoices", "supply_type")
    op.drop_column("invoices", "place_of_supply")
    op.drop_column("invoices", "igst_paise")
    op.drop_column("invoices", "sgst_paise")
    op.drop_column("invoices", "cgst_paise")
