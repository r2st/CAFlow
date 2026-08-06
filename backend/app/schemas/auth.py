"""Auth, firm and practitioner schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field, computed_field, field_validator

from app.core.security import MAX_PASSWORD_BYTES
from app.models.base import FirmPlan, PractitionerRole
from app.schemas.common import (
    ORMModel,
    Password,
    SanitizedModel,
    not_clearable,
    pan_matches_gstin,
    validate_gstin,
    validate_pan,
    validate_pincode,
)


class FirmRegisterRequest(SanitizedModel):
    firm_name: str = Field(min_length=2, max_length=255)
    icai_registration_number: str | None = Field(default=None, max_length=64)
    firm_email: EmailStr
    firm_phone: str | None = Field(default=None, max_length=20)
    pan: str | None = None
    gstin: str | None = None
    # The supplier's address, which is a mandatory particular of a tax invoice
    # under Rule 46(b). The columns were always there and sign-up never asked,
    # so every firm reached its first invoice without one.
    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    pincode: str | None = None
    plan: FirmPlan = FirmPlan.SOLO

    # The first practitioner — becomes the firm owner.
    owner_full_name: str = Field(min_length=2, max_length=255)
    owner_email: EmailStr
    owner_password: Password
    owner_membership_number: str | None = Field(default=None, max_length=32)

    _validate_pan = field_validator("pan")(validate_pan)
    _validate_gstin = field_validator("gstin")(validate_gstin)
    _validate_pincode = field_validator("pincode")(validate_pincode)
    _pan_matches_gstin = pan_matches_gstin()


class FirmUpdate(SanitizedModel):
    """The firm's own profile, as its owner or a partner may correct it.

    Everything here was write-once until now: the firm was built from the
    sign-up form and no endpoint could touch it again. That is a stranger gap
    than it sounds, because two of these fields are not description — they
    decide the tax on every invoice the practice raises.

    :func:`~app.services.gst.resolve_supply` compares the firm's state with the
    client's to decide whether a supply is taxed as CGST plus SGST or as IGST,
    and it takes the firm's state from its GSTIN first. Sign-up never asked for
    a GSTIN, so no firm had one, and none could be added: every supply was
    undetermined, every invoice fell back to intra-state, and a practice billing
    a client in the next state issued CGST/SGST on what is an IGST supply. The
    client cannot claim that credit. They find out from their own 2B, months
    later, and it is the firm's invoice that has to be revised.

    The address is the same shape of problem in a quieter place: Rule 46
    requires the supplier's address and PIN code on a tax invoice, and there
    was nowhere to put either.

    What is deliberately *not* here is the plan and the standing. ``plan`` is
    what the client and user limits are read from, so accepting it on this
    endpoint would let any firm admin lift their own limits by sending a
    field — the plan changes when it is paid for, not when it is patched.
    ``is_active`` switches the whole firm off, and every sign-in checks it:
    a firm that patched its own flag to false would lock out its owner along
    with everyone else, with no one left holding the standing to undo it.
    """

    name: str | None = Field(default=None, min_length=2, max_length=255)
    icai_registration_number: str | None = Field(default=None, max_length=64)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=20)
    pan: str | None = None
    gstin: str | None = None
    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    pincode: str | None = None

    _validate_pan = field_validator("pan")(validate_pan)
    _validate_gstin = field_validator("gstin")(validate_gstin)
    _validate_pincode = field_validator("pincode")(validate_pincode)
    # Only catches a patch carrying both halves; one that moves a single half is
    # checked against what is stored, in ``update_firm``.
    _pan_matches_gstin = pan_matches_gstin()
    # A firm always has a name and a contact address; the rest may be emptied.
    _no_nulls = not_clearable("name", "email")


class LoginRequest(SanitizedModel):
    email: EmailStr
    # Not ``Password``: this one is being checked, not set. A password too long
    # for bcrypt cannot be the one on any account, and ``verify_password``
    # already turns it into the same 401 as any other wrong password — while
    # still spending the same time on it. A 422 here would answer faster than
    # a real attempt and say so.
    password: str = Field(min_length=1, max_length=72)


class PractitionerCreate(SanitizedModel):
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    password: Password
    role: PractitionerRole = PractitionerRole.JUNIOR
    phone: str | None = Field(default=None, max_length=20)
    membership_number: str | None = Field(default=None, max_length=32)


class PractitionerUpdate(SanitizedModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=255)
    role: PractitionerRole | None = None
    phone: str | None = Field(default=None, max_length=20)
    membership_number: str | None = Field(default=None, max_length=32)
    is_active: bool | None = None

    # A team member always has a name, a role and a standing.
    _no_nulls = not_clearable("full_name", "role", "is_active")


class PasswordChangeRequest(SanitizedModel):
    """A member replacing their own password.

    The current one is required, and it is what separates this from a reset. A
    bearer token is a credential a browser hands over on every request, and a
    token that has been taken is precisely the case a password change is the
    remedy for — so a change that needed only the token would let whoever took
    it lock the real member out of their own account. Knowing the password in
    force is the proof the token alone does not carry.

    Not ``Password`` for the current one, for the reason
    :class:`LoginRequest` gives: it is being checked rather than set, and a
    length its own account could never hold is simply a wrong password.
    """

    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_BYTES)
    new_password: Password


class PasswordResetRequest(SanitizedModel):
    """A firm admin setting a password for a member who cannot sign in.

    No current password, because the point of this is that nobody has it —
    which is exactly why it is restricted to owners and partners, refuses the
    firm owner, and is refused on the caller's own account. See
    :func:`~app.api.routes.auth.reset_practitioner_password`.
    """

    new_password: Password


class PractitionerOut(ORMModel):
    id: uuid.UUID
    firm_id: uuid.UUID
    full_name: str
    email: EmailStr
    phone: str | None
    membership_number: str | None
    role: PractitionerRole
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class FirmOut(ORMModel):
    id: uuid.UUID
    name: str
    icai_registration_number: str | None
    email: EmailStr
    phone: str | None
    pan: str | None
    gstin: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    pincode: str | None
    plan: FirmPlan
    is_active: bool
    created_at: datetime
    # Derived from the plan. Sent so the team screen can show what is left
    # before it is spent, rather than letting the firm discover the ceiling
    # by being refused at the point of adding someone. ``None`` is unlimited.
    user_limit: int | None
    client_limit: int | None

    @computed_field
    @property
    def place_of_supply_label(self) -> str | None:
        """``"27-Maharashtra"`` — the state this firm's supplies are made from.

        The same resolution every invoice is taxed by, answered here so a firm
        can see it before it is printed on one rather than afterwards. It is
        the GSTIN's own state code when there is a GSTIN, and the typed state
        name otherwise; see :func:`~app.services.gst.firm_state_code`.

        ``None`` means neither field named a state, which is not cosmetic: with
        no supplier state there is nothing to compare a client's against, so
        every invoice the firm raises falls back to intra-state CGST/SGST
        whatever the client's own state says. Sent so the settings screen can
        say that plainly, since the alternative is a firm discovering it from a
        client who could not claim the credit.

        Resolved by ``firm_state_code`` itself rather than by repeating what it
        does, so this cannot answer one thing while an invoice is taxed by
        another. It reads ``.gstin`` and ``.state``, which this schema carries
        under the same names as the model does.
        """
        from app.services.gst import firm_state_code, place_of_supply_label

        return place_of_supply_label(firm_state_code(self))


class TokenResponse(SanitizedModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    practitioner: PractitionerOut
    firm: FirmOut


class RegisterResponse(TokenResponse):
    pass
