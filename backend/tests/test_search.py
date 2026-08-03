"""Free-text search: the caller's text is text, not ``LIKE`` pattern syntax.

Three endpoints take a ``search`` box and wrap it in ``%…%`` — clients, tasks
and the audit trail. The value has always been bound as a parameter, so this
was never an injection. ``%`` and ``_`` are metacharacters *inside* the
pattern, which is the quieter problem: the search silently answers a different
question from the one that was typed, and there is nothing on the screen to say
so.

A practitioner who searches a client by name and is told there is no such
client creates a duplicate. One who searches the audit trail and is shown a
wider or narrower slice than they asked for has a trail they cannot rely on,
which is the only thing a trail is for.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.search import contains_pattern
from tests.conftest import make_client_payload

API = "/api/v1"


class TestContainsPattern:
    """The helper on its own, before any endpoint uses it."""

    def test_ordinary_text_is_wrapped_untouched(self):
        assert contains_pattern("Aurora") == "%Aurora%"

    def test_surrounding_whitespace_is_dropped(self):
        assert contains_pattern("  Aurora  ") == "%Aurora%"

    @pytest.mark.parametrize("term", [None, "", "   ", "\t\n"])
    def test_a_box_nobody_typed_in_is_no_filter_at_all(self, term):
        """``%%`` costs a scan to answer a question that was not asked."""
        assert contains_pattern(term) is None

    @pytest.mark.parametrize(
        ("term", "expected"),
        [
            ("A_B", r"%A\_B%"),
            ("50%", r"%50\%%"),
            ("%", r"%\%%"),
            ("_", r"%\_%"),
            ("back\\slash", r"%back\\slash%"),
            # The escape character is doubled first, or escaping the wildcard
            # after it would produce an escape of our own escape.
            ("\\%", r"%\\\%%"),
        ],
    )
    def test_metacharacters_are_escaped(self, term, expected):
        assert contains_pattern(term) == expected


class TestClientSearchTakesTheTermLiterally:
    @pytest.fixture(autouse=True)
    def two_similar_clients(self, client: TestClient, auth_headers: dict):
        client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                name="A_B Traders", pan="AABCA1111P", gstin=None, email="ab@example.in"
            ),
        )
        client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                name="AXB Traders", pan="AABCX2222P", gstin=None, email="axb@example.in"
            ),
        )

    def _search(self, client, auth_headers, term):
        response = client.get(
            f"{API}/clients", headers=auth_headers, params={"search": term}
        )
        assert response.status_code == 200, response.text
        return [row["name"] for row in response.json()["items"]]

    def test_an_underscore_matches_an_underscore_and_not_any_character(
        self, client: TestClient, auth_headers: dict
    ):
        """Unescaped, ``A_B`` also matched ``AXB`` — and the practitioner
        cannot tell which of the two rows in front of them is the one they
        looked for."""
        assert self._search(client, auth_headers, "A_B") == ["A_B Traders"]

    def test_a_bare_wildcard_matches_nothing_rather_than_the_whole_firm(
        self, client: TestClient, auth_headers: dict
    ):
        assert self._search(client, auth_headers, "%") == []

    def test_a_wildcard_inside_a_term_does_not_widen_it(
        self, client: TestClient, auth_headers: dict
    ):
        assert self._search(client, auth_headers, "A%Traders") == []

    def test_a_trailing_backslash_does_not_swallow_the_closing_wildcard(
        self, client: TestClient, auth_headers: dict
    ):
        """PostgreSQL treats ``\\`` as ``LIKE``'s escape by default, so a term
        ending in one used to consume the ``%`` we appended and match nothing
        anywhere."""
        assert self._search(client, auth_headers, "Traders\\") == []

    def test_an_ordinary_search_still_finds_its_client(
        self, client: TestClient, auth_headers: dict
    ):
        assert self._search(client, auth_headers, "Traders") == [
            "AXB Traders",
            "A_B Traders",
        ]

    def test_a_blank_search_lists_everyone_rather_than_filtering(
        self, client: TestClient, auth_headers: dict
    ):
        assert sorted(self._search(client, auth_headers, "   ")) == [
            "AXB Traders",
            "A_B Traders",
        ]


class TestTaskSearchTakesTheTermLiterally:
    @pytest.fixture(autouse=True)
    def two_similar_tasks(self, client: TestClient, auth_headers: dict):
        for title in ("Reconcile bank_statement", "Reconcile bankXstatement"):
            response = client.post(
                f"{API}/tasks", headers=auth_headers, json={"title": title}
            )
            assert response.status_code == 201, response.text

    def _search(self, client, auth_headers, term):
        response = client.get(f"{API}/tasks", headers=auth_headers, params={"search": term})
        assert response.status_code == 200, response.text
        return [row["title"] for row in response.json()["items"]]

    def test_an_underscore_is_not_a_single_character_wildcard(
        self, client: TestClient, auth_headers: dict
    ):
        assert self._search(client, auth_headers, "bank_statement") == [
            "Reconcile bank_statement"
        ]

    def test_a_bare_wildcard_matches_no_task(self, client: TestClient, auth_headers: dict):
        assert self._search(client, auth_headers, "%") == []

    def test_an_ordinary_search_still_finds_both(
        self, client: TestClient, auth_headers: dict
    ):
        assert len(self._search(client, auth_headers, "Reconcile")) == 2


class TestAuditSearchTakesTheTermLiterally:
    @pytest.fixture(autouse=True)
    def two_similar_entries(self, client: TestClient, auth_headers: dict):
        client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                name="A_B Traders", pan="AABCA1111P", gstin=None, email="ab@example.in"
            ),
        )
        client.post(
            f"{API}/clients",
            headers=auth_headers,
            json=make_client_payload(
                name="AXB Traders", pan="AABCX2222P", gstin=None, email="axb@example.in"
            ),
        )

    def _search(self, client, auth_headers, term):
        response = client.get(f"{API}/audit", headers=auth_headers, params={"search": term})
        assert response.status_code == 200, response.text
        return [row["summary"] for row in response.json()["items"]]

    def test_an_underscore_matches_only_itself(self, client: TestClient, auth_headers: dict):
        summaries = self._search(client, auth_headers, "A_B Traders")
        assert len(summaries) == 1
        assert "A_B Traders" in summaries[0]

    def test_a_bare_wildcard_does_not_return_the_whole_trail(
        self, client: TestClient, auth_headers: dict
    ):
        """The worst place for a search to quietly widen: a trail read as
        complete when the filter never applied."""
        assert self._search(client, auth_headers, "%") == []

    def test_an_ordinary_search_still_finds_both_entries(
        self, client: TestClient, auth_headers: dict
    ):
        assert len(self._search(client, auth_headers, "Traders")) == 2
