"""Setting a practitioner's password, and ending the sessions it opened.

A password was write-once. It was chosen at sign-up by the owner, or typed into
the *Add team member* form by an admin on somebody else's behalf, and after that
nothing could change it — there was no endpoint, no schema field and no screen.
Which means that for the life of the account:

* whoever created it goes on knowing the credential. A firm admin adding a
  junior necessarily picks their first password; the junior had no way to
  replace it, so "temporary" was a description of nothing;
* a password shared over WhatsApp, read off a shoulder, or typed into the wrong
  window could not be rotated. The only remedy the API offered was deactivating
  the account, which takes the member's work off them
  (:func:`~app.services.firms.release_assignments`) to fix a leaked secret;
* a member leaving and coming back, a laptop lost, a password reused from a
  service that has since been breached — each of these has one ordinary answer
  everywhere else, and here had none.

For a product holding a firm's clients' PANs, GSTINs, filings and fees, that is
the credential half of the system having no moving parts at all.

Changing a password has to end the sessions that were opened under the old one,
or the rotation is cosmetic: an access token lives twelve hours, so someone who
took the old password and signed in keeps that session for the rest of the day
whatever the owner does about it afterwards. Revoked the way the client portal's
magic links already are — a cut-off instant on the row, and a token accepted
only if it was minted at or after it — so there is no blacklist to keep, nothing
to expire, and one mechanism to understand rather than two.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core import security
from app.models.firm import Practitioner


def set_password(practitioner: Practitioner, new_password: str, *, at: datetime | None = None):
    """Store a new password and end every session opened before now.

    The hash and the cut-off move together, deliberately. Writing one without
    the other is the half-fix that reads as done: a new hash with live old
    tokens is a password rotated in the record only, and a cut-off with the old
    hash locks everyone out including the person who just chose the password.
    """
    cutoff = at or datetime.now(UTC)
    practitioner.password_hash = security.hash_password(new_password)
    practitioner.credentials_valid_from = cutoff
    return cutoff


def sessions_are_current(practitioner: Practitioner, issued_at_ms: int | None) -> bool:
    """Was this token minted after the last password change on this account?"""
    return security.issued_after(practitioner.credentials_valid_from, issued_at_ms)
