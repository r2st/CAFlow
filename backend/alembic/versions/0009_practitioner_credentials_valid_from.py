"""Give a practitioner's sessions a revocation cut-off.

A password could not be changed at all before this, so nothing ever needed to
end the sessions a previous one had opened. Now that it can be — by the member
themselves, or by a firm admin resetting an account someone is locked out of —
the rotation has to reach the tokens already issued: an access token lives
twelve hours, so a password taken and used to sign in keeps that session for the
rest of the day whatever is done about the password afterwards.

Revoked without a blacklist, exactly as ``clients.portal_token_valid_from``
already revokes magic links: this column is a cut-off instant, and a token is
accepted only when it was minted at or after it.

Null on every existing row, which is the honest starting value — it means no
password change has happened on that account, so no session is out of date.
Nullable rather than defaulted to "now", which would have signed every
practitioner on the deployment out at the moment of the deploy.

Revision ID: 0009_cred_cutoff
Revises: 0008_gst_split
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_cred_cutoff"
down_revision: str | None = "0008_gst_split"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "practitioners",
        sa.Column("credentials_valid_from", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Dropping it un-revokes nothing that is still live: an access token expires
    # within twelve hours of being minted, so the longest a session outlasts the
    # rollback is one token lifetime.
    op.drop_column("practitioners", "credentials_valid_from")
