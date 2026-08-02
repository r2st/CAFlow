"""Make a practitioner's email address unique across the deployment.

``POST /auth/login`` is given an address and a password, and no firm. So the
address is the sign-in identity, and it has to name exactly one account.

It did not. The table's only uniqueness was ``(firm_id, email)``, and
``POST /auth/practitioners`` checked for duplicates inside the adding firm
alone. Two firms could therefore each hold an account for the same address —
which is not a contrived case: a part-time accountant on two firms' books, or,
far more ordinary, someone who leaves one practice and joins another, since a
departing member's row is deactivated rather than deleted.

The second account was then unreachable for ever. Sign-in resolved the address
to whichever of the two rows the database returned first, that row's hash never
matched the other account's password, and the member was told their own
credentials were wrong. Nothing failed, nothing was logged, and the firm that
had just added them had a 201 saying it worked.

Replacing the index with a unique one is what makes the rule hold under
concurrency: the route now refuses a duplicate, but two firms adding the same
address at the same moment both read "free" before either writes.

A deployment that already carries duplicates cannot have this applied, and
finding that out as a raw constraint violation mid-deploy helps nobody, so they
are looked for first and reported by address.

Revision ID: 0004_unique_email
Revises: 0003_indexes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_unique_email"
down_revision: str | None = "0003_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_practitioner_email_lower"


def _duplicate_emails(bind) -> list[tuple[str, int]]:
    """Addresses held by more than one practitioner, and how many hold them."""
    rows = bind.execute(
        sa.text(
            "SELECT lower(email) AS address, count(*) AS holders "
            "FROM practitioners GROUP BY lower(email) HAVING count(*) > 1 "
            "ORDER BY address"
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


def _refuse_if_ambiguous(bind) -> None:
    duplicates = _duplicate_emails(bind)
    if not duplicates:
        return
    listed = "\n".join(f"  {address} — {holders} accounts" for address, holders in duplicates)
    raise RuntimeError(
        "Cannot make practitioner emails unique: these addresses are already "
        f"held by more than one account.\n{listed}\n"
        "Each of these people can only ever sign in to one of their accounts, "
        "and which one is not defined. Decide which account each address should "
        "keep and change or deactivate-and-rename the others, then run this "
        "migration again."
    )


def upgrade() -> None:
    bind = op.get_bind()
    _refuse_if_ambiguous(bind)

    if bind.dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction block, and the drop and
        # the create are separate statements either way: for the moment between
        # them sign-in falls back to a sequential scan on a small table, which
        # is cheaper than holding an ACCESS EXCLUSIVE lock through the build.
        with op.get_context().autocommit_block():
            op.drop_index(INDEX_NAME, table_name="practitioners", postgresql_concurrently=True)
            op.create_index(
                INDEX_NAME,
                "practitioners",
                [sa.text("lower(email)")],
                unique=True,
                postgresql_concurrently=True,
            )
        return

    op.drop_index(INDEX_NAME, table_name="practitioners")
    op.create_index(INDEX_NAME, "practitioners", [sa.text("lower(email)")], unique=True)


def downgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index(INDEX_NAME, table_name="practitioners", postgresql_concurrently=True)
            op.create_index(
                INDEX_NAME,
                "practitioners",
                [sa.text("lower(email)")],
                postgresql_concurrently=True,
            )
        return

    op.drop_index(INDEX_NAME, table_name="practitioners")
    op.create_index(INDEX_NAME, "practitioners", [sa.text("lower(email)")])
