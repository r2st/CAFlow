"""Turning a caller's search box into a ``LIKE`` pattern safely.

Three endpoints take a free-text ``search`` and wrap it in ``%…%`` — clients,
tasks and the audit trail. The value is bound as a parameter, so this was never
an injection; ``%`` and ``_`` are metacharacters *inside* the pattern, which is
a different and quieter problem.

Unescaped, the caller's own text is read as pattern syntax:

* ``_`` matches any single character, so a client filed under "A_B Traders" is
  not found by typing its name while three other rows are;
* ``%`` matches anything at all, so searching a GSTIN fragment like ``27%``
  returns the whole firm rather than the one client, and a bare ``%`` returns
  every row while looking like a narrow search that happened to match a lot;
* ``\\`` is the escape character PostgreSQL applies to ``LIKE`` by default, so
  a term ending in one swallows the closing ``%`` and the pattern stops
  matching anything.

A search box that quietly returns the wrong rows is worse than one that returns
none: the practitioner reads "no such client" and creates a duplicate, or reads
a filtered audit trail as the whole of it.

The escape character has to be *declared* as well as applied. PostgreSQL
defaults to backslash; SQLite has no default at all, so an unescaped-by-default
``ESCAPE``-less pattern would leave the backslashes in as literal characters
there. Passing ``escape=LIKE_ESCAPE`` to ``ilike`` is what makes the two agree.
"""

from __future__ import annotations

import re

LIKE_ESCAPE = "\\"

# The two wildcards, plus the escape character itself — which has to be doubled
# first, or escaping the wildcards would introduce new escapes of its own.
_LIKE_METACHARACTERS = re.compile(r"([\\%_])")


def contains_pattern(term: str | None) -> str | None:
    """``%term%`` with the caller's metacharacters neutered, or None for blank.

    None means "no filter": a box holding only whitespace is a box nobody
    typed in, and turning it into ``%%`` costs a scan to answer a question that
    was not asked.
    """
    if term is None:
        return None
    cleaned = term.strip()
    if not cleaned:
        return None
    # A function replacement rather than a template: the template for "one
    # backslash then the group" is itself four backslashes deep, which is
    # exactly the sort of thing that reads as correct while escaping nothing.
    escaped = _LIKE_METACHARACTERS.sub(lambda match: LIKE_ESCAPE + match.group(1), cleaned)
    return f"%{escaped}%"
