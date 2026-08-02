"""Decisions taken per firm, and the lock that puts them in an order.

Two things live here because they are the same shape: read the firm's current
state, decide, then write. Between the read and the write sits every other
request, which is what a plan limit and an invoice number both got wrong.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.firm import Firm, Practitioner


class PlanLimitReached(Exception):
    """The firm's plan has no room left for another of something."""


def lock_firm(db: Session, firm_id: uuid.UUID) -> None:
    """Serialise this firm's per-firm decisions for the rest of the transaction.

    Anything that counts what a firm already has and then adds to it needs the
    two halves to be one step. Holding the firm's own row is the cheapest thing
    that does it, and being per firm, nobody else waits behind it.

    A plain ``SELECT`` is not blocked by this in PostgreSQL, so resolving the
    firm on other requests carries on untouched. SQLite has no row locks and
    drops the clause, which costs the test-suite nothing: one connection writes
    at a time there anyway.
    """
    db.execute(select(Firm.id).where(Firm.id == firm_id).with_for_update())


def servable_firm_ids(db: Session, firm_id: uuid.UUID | None = None) -> set[uuid.UUID]:
    """The firms this deployment still acts for, optionally narrowed to one.

    A background job runs for every tenant at once, so "is this firm still
    active?" has no request to have answered it. Anything that speaks to a
    client on a firm's behalf resolves the set once and works inside it: a firm
    switched off is one whose clients hear nothing further from us.

    Narrowing to a firm that is not active returns the empty set rather than
    that firm, so a caller that already holds a firm id gets the same answer as
    one that swept for them.
    """
    stmt = select(Firm.id).where(Firm.is_active.is_(True))
    if firm_id is not None:
        stmt = stmt.where(Firm.id == firm_id)
    return set(db.scalars(stmt).all())


def active_client_count(db: Session, firm_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count(Client.id)).where(
                Client.firm_id == firm_id, Client.is_active.is_(True)
            )
        )
        or 0
    )


def active_user_count(db: Session, firm_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count(Practitioner.id)).where(
                Practitioner.firm_id == firm_id, Practitioner.is_active.is_(True)
            )
        )
        or 0
    )


def claim_client_slot(db: Session, firm: Firm) -> None:
    """Take one of the firm's client slots, or refuse.

    Call this before the row is created or switched back on, never after: the
    count has to be of what the firm has *without* the one being asked for.
    """
    limit = firm.client_limit
    if limit is None:
        return
    lock_firm(db, firm.id)
    if active_client_count(db, firm.id) >= limit:
        raise PlanLimitReached(
            f"The {firm.plan.value} plan allows {limit} clients. Upgrade to add more."
        )


def claim_user_slot(db: Session, firm: Firm) -> None:
    """Take one of the firm's user slots, or refuse."""
    limit = firm.user_limit
    if limit is None:
        return
    lock_firm(db, firm.id)
    if active_user_count(db, firm.id) >= limit:
        raise PlanLimitReached(
            f"The {firm.plan.value} plan allows {limit} user(s). Upgrade to add more."
        )
