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
