"""Firm registration, login and practitioner management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentFirm, CurrentPractitioner, DbSession, FirmAdmin
from app.api.routes import reject_mismatched_pan_gstin
from app.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.models.base import PractitionerRole
from app.models.firm import Firm, Practitioner
from app.schemas.auth import (
    FirmOut,
    FirmRegisterRequest,
    FirmUpdate,
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetRequest,
    PractitionerCreate,
    PractitionerOut,
    PractitionerUpdate,
    RegisterResponse,
    TokenResponse,
)
from app.services import audit, credentials, firms
from app.services import tasks as task_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _claim_user_slot(db: Session, firm: Firm) -> None:
    """Take a user slot, or answer 402.

    Called from adding a member *and* from switching a deactivated one back on.
    The plan caps how many practitioners a firm has active at once, and a
    reactivation adds to that count exactly as an addition does.
    """
    try:
        firms.claim_user_slot(db, firm)
    except firms.PlanLimitReached as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
        ) from exc


def practitioner_by_email(db: Session, email: str) -> Practitioner | None:
    """The one account signing in with ``email``, or None.

    Case-insensitive, and deliberately not scoped to a firm: ``/auth/login``
    receives an address and a password and nothing else, so the address is the
    identity. ``ix_practitioner_email_lower`` is what makes "the one account"
    true — see :func:`_refuse_duplicate_email`.
    """
    return db.scalar(select(Practitioner).where(func.lower(Practitioner.email) == email.lower()))


def _refuse_duplicate_email(db: Session, email: str) -> None:
    """Stop an address that already signs in somewhere being reused.

    Checked across the deployment rather than within the firm. An address is
    what sign-in resolves an account by, so a second account under one is an
    account that can never be reached: the lookup returns one row, that row's
    hash does not match the other password, and the member is told their
    credentials are wrong while the firm that added them saw a 201.

    The message does not say *where* the address is already in use. A firm
    admin may add any address they like, and confirming that it belongs to
    someone at another firm would answer a question they are not entitled to
    ask by typing addresses into this form.
    """
    if practitioner_by_email(db, email) is None:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "This email address is already in use. An address signs in to one "
            "DoAide Reach account, so each team member needs their own."
        ),
    )


def _record_sign_in_refusal(
    db: Session,
    request: Request,
    practitioner: Practitioner | None,
    *,
    reason: str,
) -> None:
    """Record a sign-in that was refused, and commit it before the refusal.

    The audit trail is what a firm reads once a credential is suspected of
    having leaked, and it held only the sign-ins that *worked*. So the one
    question it exists to answer — was somebody trying to get into this
    account? — could not be asked of it. A hundred failed attempts against a
    partner's address over a weekend, followed by one success, read in the log
    as a single ordinary Monday sign-in; the two hundred rows that would have
    named the attempt were never written. The same blindness covers the quieter
    version: a member who has left and whose password still works somewhere is
    only visible in the failures they cause before they get in.

    ``actor_practitioner_id`` is deliberately left unset, and this is the
    reason the entry is built by hand rather than passed ``actor=``. That
    column means "this practitioner did this", and a failed sign-in is somebody
    *claiming* to be them — recording the account as the actor of an attempt it
    may know nothing about would put the member's own name against an intrusion
    into their account. The address is named in the label and the summary
    instead, which is what a reader searches on either way.

    Nothing is recorded for an address that signs in nowhere. There is no firm
    to attach such a row to, so no firm could read it — ``GET /audit`` is
    scoped by ``firm_id`` — and an unattached row for any address a caller
    cares to type is an unauthenticated write into the audit table with no
    reader. The addresses that matter are the ones that name an account, and
    those are exactly the ones kept.

    Committed here, because the caller raises immediately afterwards and an
    ``HTTPException`` out of a handler discards the session's work. Nothing
    else is pending on this transaction: the failure paths write no other row.
    """
    if practitioner is None:
        return
    caller_ip, caller_agent = audit.request_origin(request)
    audit.record(
        db,
        action="auth.login_failed",
        entity_type="practitioner",
        entity_id=practitioner.id,
        firm_id=practitioner.firm_id,
        actor_label=audit.practitioner_label(practitioner),
        summary=f"Refused sign-in for {practitioner.email} — {reason}",
        changes={"reason": reason},
        ip_address=caller_ip,
        user_agent=caller_agent,
    )
    db.commit()


def _token_response(practitioner: Practitioner, firm: Firm) -> TokenResponse:
    token = create_access_token(
        practitioner_id=practitioner.id, firm_id=firm.id, role=practitioner.role
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
        practitioner=PractitionerOut.model_validate(practitioner),
        firm=FirmOut.model_validate(firm),
    )


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a firm and its owner",
)
def register_firm(payload: FirmRegisterRequest, request: Request, db: DbSession):
    """Create a firm together with its owner practitioner."""
    _refuse_duplicate_email(db, payload.owner_email)
    caller_ip, caller_agent = audit.request_origin(request)

    firm = Firm(
        name=payload.firm_name,
        icai_registration_number=payload.icai_registration_number,
        email=payload.firm_email.lower(),
        phone=payload.firm_phone,
        pan=payload.pan,
        gstin=payload.gstin,
        address_line1=payload.address_line1,
        address_line2=payload.address_line2,
        city=payload.city,
        state=payload.state,
        pincode=payload.pincode,
        plan=payload.plan,
    )
    db.add(firm)
    db.flush()

    owner = Practitioner(
        firm_id=firm.id,
        full_name=payload.owner_full_name,
        email=payload.owner_email.lower(),
        password_hash=hash_password(payload.owner_password),
        role=PractitionerRole.OWNER,
        membership_number=payload.owner_membership_number,
    )
    db.add(owner)
    db.flush()

    audit.record(
        db,
        action="firm.register",
        entity_type="firm",
        entity_id=firm.id,
        actor=owner,
        summary=f"Firm {firm.name} registered on the {firm.plan.value} plan",
        ip_address=caller_ip,
        user_agent=caller_agent,
    )
    db.commit()
    db.refresh(firm)
    db.refresh(owner)
    return _token_response(owner, firm)


@router.post("/login", response_model=TokenResponse, summary="Sign in")
def login(payload: LoginRequest, request: Request, db: DbSession):
    practitioner = practitioner_by_email(db, payload.email)
    # Handing the absent case to verify_password rather than short-circuiting
    # on it is what makes the claim true: both paths spend a full bcrypt round,
    # so the reply takes the same time whether or not the email is known here.
    stored_hash = practitioner.password_hash if practitioner is not None else None
    # Evaluated into a name before it is tested, so that no rearrangement of
    # this condition can grow a short circuit that skips the bcrypt round on
    # the unknown-email path. `practitioner is None` implies the check below
    # failed anyway; naming both makes that an assertion rather than a trace.
    password_matches = verify_password(payload.password, stored_hash)
    if practitioner is None or not password_matches:
        # Recorded against the account the address names, and against nothing
        # at all when it names none — see :func:`_record_sign_in_refusal`. What
        # comes back to the caller is unchanged either way, so the trail learns
        # something the reply still does not say.
        _record_sign_in_refusal(db, request, practitioner, reason="wrong password")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    if not practitioner.is_active:
        # The password was right. That is the more urgent of the two entries:
        # a credential that still works on an account the firm has switched
        # off is one nobody has rotated.
        _record_sign_in_refusal(
            db, request, practitioner, reason="the account has been deactivated"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account has been deactivated"
        )

    firm = db.get(Firm, practitioner.firm_id)
    if firm is None or not firm.is_active:
        # Only when the firm is still a row: ``audit_log.firm_id`` is a foreign
        # key, so an entry naming a firm that has gone cannot be written — and
        # there would be nobody left to read it.
        _record_sign_in_refusal(
            db,
            request,
            practitioner if firm is not None else None,
            reason="the firm is not active",
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Firm is not active")

    practitioner.last_login_at = datetime.now(UTC)
    caller_ip, caller_agent = audit.request_origin(request)
    audit.record(
        db,
        action="auth.login",
        entity_type="practitioner",
        entity_id=practitioner.id,
        actor=practitioner,
        summary=f"{practitioner.email} signed in",
        ip_address=caller_ip,
        user_agent=caller_agent,
    )
    db.commit()
    db.refresh(practitioner)
    return _token_response(practitioner, firm)


@router.get("/me", response_model=PractitionerOut, summary="The signed-in practitioner")
def read_me(practitioner: CurrentPractitioner):
    return practitioner


@router.get("/firm", response_model=FirmOut, summary="The signed-in practitioner's firm")
def read_firm(firm: CurrentFirm):
    return firm


@router.patch("/firm", response_model=FirmOut, summary="Update the firm's own profile")
def update_firm(payload: FirmUpdate, admin: FirmAdmin, firm: CurrentFirm, db: DbSession):
    """Correct the firm's own particulars. Restricted to owners and partners.

    Owners and partners rather than managers, which is where the rest of firm
    administration already sits. A manager commits the firm to individual
    things — an invoice, a client, a payment — while this decides how *every*
    invoice is taxed and what address is printed on all of them, which is the
    same standing as team management and the audit trail.

    See :class:`~app.schemas.auth.FirmUpdate` for what is editable and, more
    to the point, what is not: the plan and the firm's standing are not fields
    a firm may set on itself.
    """
    updates = payload.model_dump(exclude_unset=True)
    # Against the pair the patch leaves behind rather than what it carried. A
    # firm entering its GSTIN for the first time — which is the ordinary use of
    # this endpoint — sends that field alone, and the PAN it has to agree with
    # is the one already on the record.
    reject_mismatched_pan_gstin(updates.get("pan", firm.pan), updates.get("gstin", firm.gstin))

    before = audit.snapshot(firm, updates)
    for key, value in updates.items():
        setattr(firm, key, value.lower() if key == "email" else value)

    audit.record(
        db,
        action="firm.update",
        entity_type="firm",
        entity_id=firm.id,
        actor=admin,
        summary=f"Updated the firm profile for {firm.name}",
        # Read off the record after the patch, not off the request body, for
        # the reason every other handler here does: the email is lowercased on
        # the way in, so the payload is not what was stored.
        changes=audit.diff(before, audit.snapshot(firm, updates)),
    )
    db.commit()
    db.refresh(firm)
    return firm


@router.post(
    "/change-password",
    response_model=TokenResponse,
    summary="Change your own password",
)
def change_password(
    payload: PasswordChangeRequest,
    practitioner: CurrentPractitioner,
    firm: CurrentFirm,
    db: DbSession,
    request: Request,
):
    """Replace your own password. Every other session ends; this one continues.

    Nothing could change a password before this. An account's first one is
    chosen by whoever created the account — the owner at sign-up, or an admin
    filling in the *Add team member* form — so "temporary password" described a
    credential that was permanent, known to a second person for the life of the
    account, and unrotatable after a laptop was lost or a message went to the
    wrong chat. See :mod:`app.services.credentials`.

    Three things happen together, and each is load-bearing:

    * **the current password is checked**, because a bearer token is not proof
      of knowing it — and a token in the wrong hands is the case this endpoint
      exists to answer. Without the check, whoever holds a stolen token locks
      the real member out of their own account with one request;
    * **every session opened before now ends**, because otherwise the rotation
      reaches only the next sign-in. A token lives twelve hours; the point of
      changing a password is to close that window, not to wait it out;
    * **a fresh token comes back**, because the cut-off has just invalidated
      the one the caller is holding. Without it, changing your password signs
      you out of the tab you did it in — which reads as the change having
      failed, and is what makes people not do it.

    A new password identical to the old one is refused rather than accepted as
    a no-op: the member came here to rotate a credential, and answering 200
    would tell them a rotation happened when the same secret is still in force.
    """
    if not verify_password(payload.current_password, practitioner.password_hash):
        # The same wording sign-in gives, and the same status. It is the same
        # question being asked of the same stored hash.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )
    # Checked against the stored hash rather than against the two strings, so a
    # password re-entered in a different normalisation is still caught as the
    # same one. Constant-time either way, being the same bcrypt comparison.
    if verify_password(payload.new_password, practitioner.password_hash):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The new password is the same as the current one — choose a different one",
        )

    credentials.set_password(practitioner, payload.new_password)
    caller_ip, caller_agent = audit.request_origin(request)
    audit.record(
        db,
        action="practitioner.password_change",
        entity_type="practitioner",
        entity_id=practitioner.id,
        actor=practitioner,
        # Never the password, obviously — but not the cut-off either as a
        # `changes` diff, since the trail is readable by the firm and the useful
        # fact is that it happened and who did it.
        summary=f"{practitioner.email} changed their own password",
        ip_address=caller_ip,
        user_agent=caller_agent,
    )
    db.commit()
    db.refresh(practitioner)
    # Minted after the cut-off was stamped and committed, so it outlives the
    # revocation it was issued alongside — which is what the millisecond
    # resolution on ``iat_ms`` is for.
    return _token_response(practitioner, firm)


@router.get("/practitioners", response_model=list[PractitionerOut], summary="List the firm's team")
def list_practitioners(practitioner: CurrentPractitioner, db: DbSession):
    stmt = (
        select(Practitioner)
        .where(Practitioner.firm_id == practitioner.firm_id)
        .order_by(Practitioner.created_at)
    )
    return list(db.scalars(stmt).all())


@router.post(
    "/practitioners",
    response_model=PractitionerOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a team member",
)
def add_practitioner(
    payload: PractitionerCreate, admin: FirmAdmin, firm: CurrentFirm, db: DbSession
):
    """Add a team member. Restricted to owners and partners."""
    if payload.role == PractitionerRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="A firm can only have one owner"
        )

    # Before the slot is claimed, so a firm that is both full and re-using an
    # address is told which of the two actually stopped it. Claiming first
    # reported a plan limit for what was really a duplicate, and sent an admin
    # to the pricing page over a typo in an email.
    _refuse_duplicate_email(db, payload.email)
    _claim_user_slot(db, firm)

    practitioner = Practitioner(
        firm_id=firm.id,
        full_name=payload.full_name,
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        role=payload.role,
        phone=payload.phone,
        membership_number=payload.membership_number,
    )
    db.add(practitioner)
    db.flush()

    audit.record(
        db,
        action="practitioner.create",
        entity_type="practitioner",
        entity_id=practitioner.id,
        actor=admin,
        summary=f"Added {practitioner.email} as {practitioner.role.value}",
    )
    db.commit()
    db.refresh(practitioner)
    return practitioner


@router.patch(
    "/practitioners/{practitioner_id}",
    response_model=PractitionerOut,
    summary="Update a team member",
)
def update_practitioner(
    practitioner_id: uuid.UUID,
    payload: PractitionerUpdate,
    admin: FirmAdmin,
    firm: CurrentFirm,
    db: DbSession,
):
    target = db.get(Practitioner, practitioner_id)
    if target is None or target.firm_id != admin.firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Practitioner not found")
    if target.role == PractitionerRole.OWNER and target.id != admin.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="The firm owner cannot be modified"
        )

    updates = payload.model_dump(exclude_unset=True)
    if updates.get("role") == PractitionerRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="A firm can only have one owner"
        )

    # The owner is the one practitioner nobody else may edit, so a change they
    # make to their own standing is a change no one has the standing to undo.
    # Stepping down or switching themselves off would leave the firm with no
    # one who can administer it and no way back in. Their own details stay
    # editable — it is only the standing that is frozen.
    if target.role == PractitionerRole.OWNER:
        if updates.get("is_active") is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "The firm owner cannot be deactivated — no one else could "
                    "reactivate the account."
                ),
            )
        if "role" in updates and updates["role"] != PractitionerRole.OWNER:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "The firm owner cannot change their own role — the firm "
                    "would be left without an administrator."
                ),
            )

    # Before the flag is applied, so the count is of what the firm holds
    # without this one, and only on the transition — re-saving an already
    # active member must not be charged a slot it already occupies.
    if updates.get("is_active") is True and not target.is_active:
        _claim_user_slot(db, firm)

    # Only on the transition, and before the flag lands so the tasks are still
    # findable under their name. Switching an account off leaves whatever it
    # was holding: they can no longer sign in to do it, and no active member's
    # queue shows it, so a statutory deadline sits on a name nobody is
    # watching. The open work goes back to unassigned; the finished work keeps
    # its assignee, being the record of who did it.
    released = 0
    handed_back = firms.ReleasedWork(clients=0, items=0)
    if updates.get("is_active") is False and target.is_active:
        released = task_service.release_open_tasks(db, target)
        # The other two things that name a practitioner. The filings went on
        # reading as theirs under a filter nobody can match, and the clients
        # are worse: generation stamps a new compliance item with the client's
        # owner, so every filing raised from then on was put back onto the
        # deactivated account. See ``firms.release_assignments``.
        handed_back = firms.release_assignments(db, target)

    before = audit.snapshot(target, updates)
    for key, value in updates.items():
        setattr(target, key, value)

    audit.record(
        db,
        action="practitioner.update",
        entity_type="practitioner",
        entity_id=target.id,
        actor=admin,
        summary=f"Updated {target.email}"
        + (f"; {released} open task(s) returned to unassigned" if released else "")
        + (
            f"; {handed_back.clients} client(s) and {handed_back.items} open "
            "filing(s) returned to unassigned"
            if handed_back
            else ""
        ),
        changes=audit.diff(before, audit.snapshot(target, updates)),
    )
    db.commit()
    db.refresh(target)
    return target


@router.post(
    "/practitioners/{practitioner_id}/reset-password",
    response_model=PractitionerOut,
    summary="Set a new password for a team member",
)
def reset_practitioner_password(
    practitioner_id: uuid.UUID,
    payload: PasswordResetRequest,
    admin: FirmAdmin,
    db: DbSession,
    request: Request,
):
    """Give a locked-out member a new password. Their existing sessions end.

    The other half of :func:`change_password`, and the half a firm reaches for
    on a Monday morning. A member who has forgotten their password had exactly
    one route back before this, and it was not a route: deactivate the account
    and create a new one, which loses their sign-in identity, takes their
    clients and open filings off them
    (:func:`~app.services.firms.release_assignments`), and spends a plan seat on
    the duplicate — all to fix a forgotten string.

    Three refusals, and each is a boundary rather than a nicety:

    * **not the owner.** A partner is a firm admin, so without this a partner
      could set the owner's password, sign in as them and take the firm. It is
      the same rule :func:`update_practitioner` already applies to the owner's
      standing, applied to the credential, where the stakes are higher;
    * **not yourself.** Resetting your own password here would be a change that
      never asks for the current one — which is the whole strength of
      :func:`change_password`, undone by a caller with a stolen token pointing
      the endpoint at themselves. Admins change their own password the way
      everyone else does;
    * **not another firm's.** The scope check every practitioner endpoint makes.

    Together these leave the owner's own password changeable only by knowing it.
    That is deliberate: any weaker rule is a path for someone inside the firm to
    take the firm's most privileged account. An owner who has genuinely lost
    theirs needs an out-of-band recovery, which is a separate mechanism and not
    something a colleague should be able to invoke.

    No password is returned, and none is generated: the admin chooses one and
    hands it over, which is the same shape as adding a member and keeps the
    secret out of the response body, the logs and the audit trail.
    """
    target = db.get(Practitioner, practitioner_id)
    if target is None or target.firm_id != admin.firm_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Practitioner not found")
    if target.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Use Change password to set your own — a reset does not ask for "
                "the password in force, and yours is the one you know."
            ),
        )
    if target.role == PractitionerRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "The firm owner's password cannot be reset by anyone else — "
                "they change it themselves from their account page."
            ),
        )

    credentials.set_password(target, payload.new_password)
    caller_ip, caller_agent = audit.request_origin(request)
    audit.record(
        db,
        action="practitioner.password_reset",
        entity_type="practitioner",
        entity_id=target.id,
        actor=admin,
        # Named on both sides: this is one person setting another person's
        # credential, and who did it to whom is the fact the trail is read for.
        summary=f"{admin.email} reset the password for {target.email}",
        ip_address=caller_ip,
        user_agent=caller_agent,
    )
    db.commit()
    db.refresh(target)
    return target
