"""Helper for writing audit-trail entries."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.firm import Practitioner
from app.schemas.common import sanitize_text

# What ``audit_log.user_agent`` holds. The header is whatever the caller chose
# to send and nothing bounded it, on the two endpoints that record it —
# ``/auth/register`` and ``/auth/login``, both reachable without credentials.
# SQLite truncates silently and PostgreSQL, which is what the deployment runs,
# refuses the row outright: a ``DataError`` out of the commit, so a caller
# sending a kilobyte of User-Agent could not register *and could not sign in*.
#
# Truncated rather than refused, for the reason ``documents`` truncates a
# filename: the sign-in is genuine either way, and losing it over the length of
# a header the practitioner never typed would be the worse answer. The head is
# what is kept here — a user agent identifies itself at the front.
MAX_USER_AGENT = 512

# What ``audit_log.actor_label`` holds, and the reason it has to be built
# rather than formatted.
#
# The column is a ``VARCHAR(255)``. The label written for a practitioner is
# ``"<full name> <email>"``, and both halves come from a caller: ``full_name``
# is bounded at 255 characters by the schema and an address at the 254 an
# ``EmailStr`` allows. So the ordinary format string is a 512-character value
# going into a 255-character column — SQLite truncates it silently and
# PostgreSQL, which is what the deployment runs, refuses the row.
#
# That refusal is not a failed audit entry. ``record`` is called inside the
# transaction of the action it describes, so the ``DataError`` comes out of the
# commit and takes the *action* with it: a practitioner whose name and address
# are long enough could not file a return, raise an invoice, upload a document
# or add a client — every mutating endpoint in the API writes here. The portal
# label has the same shape on a smaller margin: ``"<client name> (client
# portal)"`` is 271 characters for a client named at the 255 the schema allows,
# and the endpoint that writes it is the client's own upload.
#
# Cut rather than refused, for the reason the user agent above is: the action
# is genuine either way, and losing it over the length of a name would be the
# worse answer. The *head* is what gives, not the tail — the trailing part is
# the email address or the marker saying this was the portal, which is the half
# that identifies the actor and the half a reader searches on.
MAX_ACTOR_LABEL = 255


def bounded_label(head: str | None, suffix: str = "") -> str:
    """A label for ``audit_log.actor_label``, cut to what the column holds.

    ``suffix`` is the part that survives: the email address in
    ``"Anita Sharma <anita@sharma-ca.in>"``, or the ``" (client portal)"``
    marker on a label naming a client rather than a practitioner. Only ``head``
    is trimmed, and only when the two together do not fit.
    """
    room = MAX_ACTOR_LABEL - len(suffix)
    if room < 1:
        # The suffix alone is over the limit, which leaves nothing to trim
        # around it. Keep its tail, which is where an address ends.
        return suffix.strip()[-MAX_ACTOR_LABEL:]
    return f"{(head or '').strip()[:room]}{suffix}"


def practitioner_label(practitioner: Practitioner) -> str:
    """``Anita Sharma <anita@sharma-ca.in>``, bounded. See :func:`bounded_label`."""
    return bounded_label(practitioner.full_name, f" <{practitioner.email}>")


def request_origin(request: Any) -> tuple[str | None, str | None]:
    """Who made this request, as the trail records it: address and user agent.

    ``request.client.host`` is the machine that opened the TCP connection, and
    in every deployment of this system that machine is Caddy. So the one field
    on the one table that answers "where was this signed in from" held the
    reverse proxy's address — the same value on every row, for every firm, for
    every registration and every sign-in. The trail is what a firm reads after
    a credential is suspected of having leaked, and it was answering with a
    constant.

    :func:`app.core.middleware.client_ip` is the resolution the access log and
    the rate limiter already use, and it is the careful one: the forwarded
    chain is believed only when the connection itself came from a trusted
    proxy, and it is walked right-to-left so a prefix the caller forged is
    skipped. Reused here rather than re-derived, so the three places that name
    a caller cannot disagree about who it was.

    The user agent is sanitised and cut to what the column holds; see
    :data:`MAX_USER_AGENT`. Sanitised because it is rendered back into the
    audit screen and the log, and control characters in it are the caller's
    choice — inbound sanitising covers every other string that reaches the
    database, and a raw header is not an exception worth making.
    """
    from app.core.middleware import client_ip

    agent = request.headers.get("user-agent")
    return (
        client_ip(request),
        (sanitize_text(agent)[:MAX_USER_AGENT] or None) if agent else None,
    )


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    actor: Practitioner | None = None,
    actor_label: str | None = None,
    firm_id: uuid.UUID | None = None,
    summary: str | None = None,
    changes: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    """Append one entry. ``actor_label`` names a non-practitioner actor —
    a client acting through the portal, or a background job.

    Whichever of the two names the actor is bounded to what the column holds;
    see :data:`MAX_ACTOR_LABEL` for what an unbounded one costs. A caller that
    has already composed its own label passes it through the same cut, so no
    call site can reintroduce the problem by formatting its own string.
    """
    entry = AuditLog(
        firm_id=firm_id or (actor.firm_id if actor else None),
        actor_practitioner_id=actor.id if actor else None,
        actor_label=(
            practitioner_label(actor)
            if actor
            else (bounded_label(actor_label) if actor_label else actor_label)
        ),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
        changes=changes or {},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(entry)
    return entry


def snapshot(entity: Any, keys: Iterable[str]) -> dict[str, Any]:
    """The entity's current values for ``keys``, for one side of a :func:`diff`.

    Both sides come from the entity, never from the request body. Diffing the
    payload against the pre-state records what was *asked for*, and a handler
    that derives a field after applying the patch then has that derivation
    misreported or lost outright:

    * A filing patched ``filed_on`` to a date past its own deadline is stored
      as ``delayed_filed``. Diffed against the payload it reads ``filed`` — a
      status the row never held — and a bare date correction that reclassified
      it recorded no status change at all. That log is the firm's account of
      when a return was lodged, which is exactly the thing an assessing officer
      asks about, so "recorded on time" against a row that says otherwise is
      the worst way for it to be wrong.
    * A task moved to ``done`` stamps ``completed_at``; a document given a
      category by hand clears the classifier's confidence. Neither is in the
      payload, so neither appeared.

    Pass a key set covering both what the caller sent and whatever the handler
    may derive from it. Keys that did not move cost nothing — ``diff`` drops
    them.
    """
    return {key: getattr(entity, key) for key in keys}


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Reduce two snapshots to only the fields that actually changed."""
    changed_before, changed_after = {}, {}
    for key in set(before) | set(after):
        old, new = before.get(key), after.get(key)
        if old != new:
            changed_before[key] = _jsonable(old)
            changed_after[key] = _jsonable(new)
    if not changed_after:
        return {}
    return {"before": changed_before, "after": changed_after}


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool, type(None), dict, list)):
        return value
    return str(value)
