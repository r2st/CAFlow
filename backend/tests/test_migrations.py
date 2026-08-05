"""What the migrations refuse to do to a database that already holds data.

A migration is exercised exactly once per environment, on the box, during a
deploy. The failure mode worth testing is not "does it apply to an empty
schema" — CI answers that against a real PostgreSQL — but what it does when
the data it is about to constrain does not fit the constraint.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load(filename: str):
    """Import a migration by filename — the module names start with a digit."""
    path = VERSIONS / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


unique_email = _load("0004_unique_practitioner_email.py")


@pytest.fixture
def practitioners():
    """A bare practitioners table, unconstrained, to put awkward rows in."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE practitioners (id INTEGER PRIMARY KEY, email TEXT)"))
        yield connection


def _add(connection, *emails: str) -> None:
    for address in emails:
        connection.execute(text("INSERT INTO practitioners (email) VALUES (:e)"), {"e": address})


class TestMakingPractitionerEmailsUnique:
    def test_a_clean_database_passes_straight_through(self, practitioners):
        _add(practitioners, "anita@sharma-ca.in", "meera@iyer-ca.in")
        unique_email._refuse_if_ambiguous(practitioners)

    def test_it_stops_before_the_index_when_an_address_is_shared(self, practitioners):
        """The point of checking first is to fail *readably*.

        Letting PostgreSQL raise the unique violation instead would abort the
        deploy with a constraint name and no indication of which people are
        affected or what to do about them.
        """
        _add(practitioners, "bob@shared.in", "bob@shared.in", "solo@sharma-ca.in")

        with pytest.raises(RuntimeError) as raised:
            unique_email._refuse_if_ambiguous(practitioners)

        message = str(raised.value)
        assert "bob@shared.in — 2 accounts" in message
        # The addresses that are fine are not listed as problems.
        assert "solo@sharma-ca.in" not in message
        # And it says what to do next, on the box, at the moment it stops.
        assert "run this migration again" in message

    def test_case_only_differences_are_one_address(self, practitioners):
        """Sign-in matches on lower(email), so the check has to as well.

        Comparing raw values would call ``Bob@`` and ``bob@`` distinct, apply
        the index, and leave the unique violation to be discovered by whichever
        of the two people signed in second.
        """
        _add(practitioners, "Bob@Shared.in", "bob@shared.in")

        with pytest.raises(RuntimeError, match="bob@shared.in"):
            unique_email._refuse_if_ambiguous(practitioners)

    def test_every_clashing_address_is_reported_not_just_the_first(self, practitioners):
        """Someone fixing these is on a production box and wants the whole list.

        Reporting one at a time turns one maintenance window into as many
        deploy attempts as there are duplicates.
        """
        _add(practitioners, "bob@shared.in", "bob@shared.in", "eve@shared.in", "eve@shared.in")

        with pytest.raises(RuntimeError) as raised:
            unique_email._refuse_if_ambiguous(practitioners)

        assert "bob@shared.in" in str(raised.value)
        assert "eve@shared.in" in str(raised.value)


line_position = _load("0007_invoice_line_position.py")


@pytest.fixture
def invoice_lines():
    """An invoice_lines table as it stood before the position column."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.execute(
            text(
                "CREATE TABLE invoice_lines ("
                "  id INTEGER PRIMARY KEY,"
                "  invoice_id TEXT NOT NULL,"
                "  description TEXT"
                ")"
            )
        )
        yield connection


def _add_lines(connection, invoice_id: str, *descriptions: str) -> None:
    for description in descriptions:
        connection.execute(
            text(
                "INSERT INTO invoice_lines (invoice_id, description)"
                " VALUES (:i, :d)"
            ),
            {"i": invoice_id, "d": description},
        )


def _backfill(connection, monkeypatch) -> None:
    """Run ``upgrade()`` against the connection, as written.

    The statements come out of the migration rather than being restated here,
    so SQL that stops numbering correctly fails this test instead of passing
    beside a copy of itself. ``op`` is replaced with something that puts them
    on this connection: Alembic's real one needs a migration context, and the
    column add and the default drop are DDL SQLite states differently.
    """
    executed: list[str] = []

    class Op:
        @staticmethod
        def add_column(table, column):
            executed.append(
                f"ALTER TABLE {table} ADD COLUMN {column.name} INTEGER NOT NULL"
                f" DEFAULT {column.server_default.arg}"
            )

        @staticmethod
        def execute(statement):
            executed.append(statement)

        @staticmethod
        def alter_column(*args, **kwargs):
            # Dropping the server default is not observable from the rows, and
            # SQLite cannot do it in place. The assertion that it happens at
            # all lives in TestTheColumnIsTheApplicationsToSet below.
            pass

    monkeypatch.setattr(line_position, "op", Op)
    line_position.upgrade()
    for statement in executed:
        connection.execute(text(statement))


def _positions(connection, invoice_id: str) -> list[tuple[str, int]]:
    rows = connection.execute(
        text(
            "SELECT description, position FROM invoice_lines"
            " WHERE invoice_id = :i ORDER BY position, id"
        ),
        {"i": invoice_id},
    ).all()
    return [(row[0], row[1]) for row in rows]


class TestNumberingExistingInvoiceLines:
    """Invoices that predate the column still have to come back in *an* order.

    Leaving every historical line at the default would tie them all at zero and
    put the whole question back on the ``id`` tiebreak — which is the same
    unspecified order the column exists to replace, only now with a column
    that looks like it means something.
    """

    def test_one_invoices_lines_are_numbered_from_zero(self, invoice_lines, monkeypatch):
        _add_lines(invoice_lines, "inv-a", "Retainer", "Audit fee", "Disbursements")

        _backfill(invoice_lines, monkeypatch)

        assert _positions(invoice_lines, "inv-a") == [
            ("Retainer", 0),
            ("Audit fee", 1),
            ("Disbursements", 2),
        ]

    def test_each_invoice_is_numbered_independently(self, invoice_lines, monkeypatch):
        """Numbering across the whole table would leave gaps and no zero.

        A position is an index into one invoice's own lines. If the second
        invoice's first line came back as 3, the editor keying rows by position
        would be reading off the end of a three-row list.
        """
        _add_lines(invoice_lines, "inv-a", "Retainer", "Audit fee")
        _add_lines(invoice_lines, "inv-b", "Advisory", "Filing fees", "Courier")

        _backfill(invoice_lines, monkeypatch)

        assert _positions(invoice_lines, "inv-a") == [("Retainer", 0), ("Audit fee", 1)]
        assert _positions(invoice_lines, "inv-b") == [
            ("Advisory", 0),
            ("Filing fees", 1),
            ("Courier", 2),
        ]

    def test_no_two_lines_of_one_invoice_share_a_position(self, invoice_lines, monkeypatch):
        """The property the whole thing rests on: the order is total."""
        _add_lines(invoice_lines, "inv-a", *[f"Line {n}" for n in range(12)])

        _backfill(invoice_lines, monkeypatch)

        positions = [position for _, position in _positions(invoice_lines, "inv-a")]
        assert positions == list(range(12))
        assert len(set(positions)) == len(positions)

    def test_an_invoice_with_one_line_gets_position_zero(self, invoice_lines, monkeypatch):
        _add_lines(invoice_lines, "inv-a", "Agreed fee for the year")

        _backfill(invoice_lines, monkeypatch)

        assert _positions(invoice_lines, "inv-a") == [("Agreed fee for the year", 0)]

    def test_an_empty_table_is_left_alone(self, invoice_lines, monkeypatch):
        """A firm with no invoices yet is the ordinary case on a fresh install."""
        _backfill(invoice_lines, monkeypatch)

        assert invoice_lines.execute(text("SELECT COUNT(*) FROM invoice_lines")).scalar() == 0


class TestTheColumnIsTheApplicationsToSet:
    """The server default exists for the length of the migration and no longer.

    It is there so the column can be NOT NULL without a window in which rows
    inserted by the still-running application have nothing to put in it. Once
    the backfill is done it has no further job, and leaving it would give the
    column a second answer — one the database supplies and no part of the
    application ever agreed to.
    """

    @staticmethod
    def _recorded():
        """What ``upgrade()`` does to the column, in order."""
        steps = []

        class Op:
            @staticmethod
            def add_column(table, column):
                steps.append(("add", table, column.name, column.server_default))

            @staticmethod
            def execute(statement):
                steps.append(("execute", statement))

            @staticmethod
            def alter_column(table, name, **kwargs):
                steps.append(("alter", table, name, kwargs.get("server_default")))

        original = line_position.op
        line_position.op = Op
        try:
            line_position.upgrade()
        finally:
            line_position.op = original
        return steps

    def test_the_column_arrives_with_a_default_and_leaves_without_one(self):
        steps = self._recorded()

        added = next(step for step in steps if step[0] == "add")
        assert added[2] == "position"
        assert added[3] is not None, "NOT NULL with no default fails on a table with rows"

        altered = next(step for step in steps if step[0] == "alter")
        assert altered[1:] == ("invoice_lines", "position", None)

    def test_the_default_is_dropped_after_the_backfill_not_before(self):
        """Order matters: dropping it first reopens the window it was closing."""
        steps = [step[0] for step in self._recorded()]

        assert steps.index("add") < steps.index("execute") < steps.index("alter")
