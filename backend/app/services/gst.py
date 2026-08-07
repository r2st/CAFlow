"""Place of supply, and the CGST/SGST/IGST split that follows from it.

GST is not one tax. A supply is either *intra-state* — the supplier and the
place of supply are in the same state, and the tax is levied half by the Centre
(CGST) and half by the State (SGST) — or *inter-state*, where the whole of it is
a single integrated levy (IGST). The total the client pays is identical either
way at the same rate, so nothing about the arithmetic of a total is affected.
What is affected is everything the number is *for*:

* a tax invoice must show the split, and the place of supply, under Rule 46 of
  the CGST Rules. A single "GST @ 18%" line is not a compliant tax invoice;
* the firm's own GSTR-1 reports the components separately, and reconciles per
  invoice. A ledger that only knows a lump sum cannot produce that return —
  which, for a product sold to the people who file GSTR-1 for a living, is the
  gap that costs the sale;
* the client claims input tax credit against the components. CGST credit
  offsets CGST; it cannot offset IGST. A client handed an invoice that names
  the wrong one claims the wrong credit, and the mismatch surfaces months later
  as a notice against *them*.

Getting it wrong in either direction is a real error and neither is self-
correcting: charging IGST on a local supply and CGST/SGST on an interstate one
both leave the client unable to take the credit they paid for.

The determination is a fact about the two parties, not a preference, so nothing
here is configurable. Under s.12(2) of the IGST Act the place of supply of a
service to a *registered* person is that person's location; to an unregistered
one it is the address on the supplier's record. A CA firm's supply of
professional services is the ordinary case of both.
"""

from __future__ import annotations

import re

from app.models.base import SupplyType

# The 36-character alphabet a GSTIN is written in, and the value of each
# character in the check-digit sum: '0'-'9' are 0-9 and 'A'-'Z' are 10-35.
CHECKSUM_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# GST state codes. The first two digits of every GSTIN, and what the place of
# supply is actually recorded as — a name is a label for one of these, not the
# other way round.
#
# Two entries are historical and are kept because a GSTIN issued under them is
# still on record and still valid to *read*: 25 (Daman and Diu) was merged into
# 26 in 2020, and 28 was Andhra Pradesh before Telangana was carved out of it
# and the remainder renumbered to 37. Refusing them would refuse a client's own
# registration number back to the firm that typed it correctly.
STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "25": "Daman and Diu",  # historical; merged into 26
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra",
    "28": "Andhra Pradesh",  # historical; pre-bifurcation, now 37
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
    "97": "Other Territory",
    "99": "Centre Jurisdiction",
}

# What a practitioner types into a free-text state field and means one of the
# codes above. Only the names that are genuinely ambiguous or renamed are
# listed; an exact match on ``STATE_CODES`` is handled without any of this.
#
# The renames matter because both names remain in daily use: a client record
# entered five years ago says "Orissa" and one entered today says "Odisha", and
# they are the same state for the purpose of deciding whether a supply is local.
_STATE_ALIASES: dict[str, str] = {
    "orissa": "21",
    "pondicherry": "34",
    "puduchery": "34",
    "uttaranchal": "05",
    "nct of delhi": "07",
    "new delhi": "07",
    "delhi ncr": "07",
    "jammu kashmir": "01",
    "j and k": "01",
    "andaman nicobar islands": "35",
    "andaman and nicobar": "35",
    "dadra and nagar haveli": "26",
    "daman and diu": "26",
    "chattisgarh": "22",
    "chhatisgarh": "22",
    "tamilnadu": "33",
    "pondichery": "34",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalise_state(name: str) -> str:
    """Fold a typed state name to something two spellings of it share."""
    return _NON_ALNUM.sub(" ", name.strip().lower()).strip()


# Built in ascending code order so that where two codes share a name — 28 and 37
# are both "Andhra Pradesh" — the later assignment wins and the lookup lands on
# the *current* code, since a name typed today means today's state. The
# superseded code stays readable in :data:`STATE_CODES` for a GSTIN issued under
# it; it is only unreachable by name, which is right.
_STATE_BY_NAME: dict[str, str] = {
    _normalise_state(name): code for code, name in STATE_CODES.items()
}
_STATE_BY_NAME.update(_STATE_ALIASES)


def state_code_of_gstin(gstin: str | None) -> str | None:
    """The state a GSTIN is registered in, or None if it is not one.

    A GSTIN is state-wise by construction: an entity operating in three states
    holds three of them, and the leading two digits are which. That makes it the
    only unambiguous statement of location either party has — an address is free
    text and a state name has three spellings, but this is a code.
    """
    if not gstin:
        return None
    code = gstin.strip()[:2]
    return code if code in STATE_CODES else None


def state_code_of_name(name: str | None) -> str | None:
    """The code for a typed state name, or None if it does not name one."""
    if not name:
        return None
    return _STATE_BY_NAME.get(_normalise_state(name))


def state_name(code: str | None) -> str | None:
    """``"27"`` -> ``"Maharashtra"``."""
    return STATE_CODES.get(code) if code else None


def place_of_supply_label(code: str | None) -> str | None:
    """``"27"`` -> ``"27-Maharashtra"``, which is how it is printed on an invoice."""
    name = state_name(code)
    return f"{code}-{name}" if name else None


def gstin_check_digit(first_fourteen: str) -> str:
    """The fifteenth character of a GSTIN, computed from the first fourteen.

    A weighted sum mod 36: each character's value is multiplied by 1 or 2 by
    alternating position, the quotient and remainder of that product against 36
    are both added in, and the check digit is what brings the total to a
    multiple of 36. It catches every single-character substitution and every
    transposition of two adjacent characters, which between them are very nearly
    the whole population of ways a fifteen-character code gets mistyped.
    """
    total = 0
    for index, char in enumerate(first_fourteen):
        product = CHECKSUM_CHARSET.index(char) * (2 if index % 2 else 1)
        total += product // 36 + product % 36
    return CHECKSUM_CHARSET[(36 - total % 36) % 36]


def gstin_checksum_valid(gstin: str) -> bool:
    """Whether a well-formed GSTIN's own check digit agrees with the rest of it."""
    if len(gstin) != 15:
        return False
    try:
        return gstin_check_digit(gstin[:14]) == gstin[14]
    except ValueError:  # a character outside the alphabet
        return False


# Where the holder's PAN sits inside a GSTIN: characters 3 to 12 of the fifteen.
PAN_IN_GSTIN = slice(2, 12)


def pan_of_gstin(gstin: str | None) -> str | None:
    """The PAN a GSTIN is built around, or None if it is not a GSTIN.

    A GSTIN is not an identifier in its own right. It is the holder's PAN with
    a state code in front of it and a registration serial, a fixed ``Z`` and a
    check digit behind::

        27 AAACR5055K 1 Z 5
        ^^ ^^^^^^^^^^ ^ ^ ^
        |  PAN        |  | check digit
        |             |  literal Z
        |             registration serial within the state
        state code

    That construction is what makes the two cross-checkable, and cross-checking
    them is the only way either can be caught being wrong. Each is separately
    well-formed — the PAN matches its pattern, the GSTIN matches its own and
    passes its own check digit — so nothing about either one alone says that
    the pair cannot both belong to one person. See
    :func:`~app.schemas.common.check_pan_gstin_agreement` for why that pair
    being wrong is expensive.
    """
    if not gstin or len(gstin) != 15:
        return None
    return gstin[PAN_IN_GSTIN]


def client_place_of_supply(client) -> str | None:
    """Where a client's supply is placed, as a state code.

    The GSTIN first, because a registered client's location *is* the state they
    are registered in — s.12(2)(a) — and the code is stated rather than
    interpreted. The typed state name is the fallback for a client with no
    registration, which is s.12(2)(b): the address on the supplier's record.

    None when neither says anything, which is left to the caller rather than
    guessed at; see :func:`resolve_supply`.
    """
    return state_code_of_gstin(client.gstin) or state_code_of_name(client.state)


def firm_state_code(firm) -> str | None:
    """The state the firm is registered in, by the same order of preference."""
    return state_code_of_gstin(firm.gstin) or state_code_of_name(firm.state)


def resolve_supply(firm, client) -> tuple[str | None, SupplyType]:
    """``(place_of_supply, supply_type)`` for a supply from ``firm`` to ``client``.

    Both states have to be known before the two can be compared, and a firm that
    has not filled in its own GSTIN is the ordinary way for one of them not to
    be. Rather than guess at an answer that would be printed on a tax invoice,
    an undetermined supply falls back to intra-state and records no place of
    supply at all — so the invoice carries CGST/SGST, which is what a practice
    billing the clients in its own city raises all day, and the empty place of
    supply is the visible marker that nothing established it.

    That is a deliberate asymmetry. Defaulting the other way would put IGST on
    the overwhelmingly common local invoice, and IGST wrongly charged is credit
    the client cannot take. Defaulting this way is wrong only for the firm that
    bills across state lines *and* has not entered its own GSTIN — which is the
    firm the blank place-of-supply field is telling to enter it.

    Resolved from the parties at the moment an invoice is raised and then stored
    on it, never recomputed: a client who later re-registers in another state has
    not changed the tax character of a bill already issued to them.

    The blank has to be *written* for it to be a marker, and only the missing
    half of the comparison was ever blanking it. An undetermined supply kept the
    recipient's own state — which is almost always known, because the client's
    GSTIN is what a firm records first — so the case the marker exists for is
    precisely the one that never showed it. A Maharashtra practice that has not
    entered its own GSTIN, billing a Karnataka client, issued a tax invoice
    reading "Place of supply: 29-Karnataka" beside CGST and SGST: an inter-state
    supply taxed as a local one, on a document that states both halves and
    contradicts itself, with nothing anywhere saying the determination had not
    been made. The client claims CGST/SGST credit that their 2B will not
    support, and it surfaces months later as a notice against *them*.

    Nothing downstream could tell either. ``InvoiceOut.place_of_supply_label``
    renders a resolved-looking "29-Karnataka", and the invoice editor's own
    "not determined — add the firm's and the client's GSTIN" branch was
    unreachable for the one reason that actually causes it. The firm's settings
    screen already says so — ``FirmOut.place_of_supply_label`` is None with no
    GSTIN — but a firm reads that screen once and its invoices every day.

    So both halves being known is what records a place of supply, and either
    one missing records none. The tax head is unchanged: intra-state either
    way, for the asymmetry above.
    """
    supplier = firm_state_code(firm)
    recipient = client_place_of_supply(client)
    if supplier is None or recipient is None:
        return None, SupplyType.INTRA_STATE
    return recipient, (
        SupplyType.INTRA_STATE if supplier == recipient else SupplyType.INTER_STATE
    )


def split_tax(tax_paise: int, supply_type: SupplyType) -> tuple[int, int, int]:
    """``(cgst, sgst, igst)`` for a tax total. The three always sum to ``tax_paise``.

    Split from the total rather than each component being computed from its own
    half-rate, so that what the invoice prints adds up to what the client is
    asked to pay. Rounding each half independently does not: at 18% on a
    subtotal of 3 paise the total rounds to 1 paise while two halves of 9% each
    round to nothing, and the invoice then shows a tax of zero against a total
    that includes one. A document of record that does not add up is worse than
    either rounding.

    The halves are equal wherever it is possible for them to be. CGST and SGST
    are levied at the same rate on the same value, so an odd total is the only
    thing that can separate them, and an odd total needs a subtotal that is not
    a whole number of rupees — fees are billed in rupees, so in practice this
    never arises. When it does the odd paise goes to SGST, consistently, rather
    than to whichever side a rounding rule happened to favour.
    """
    if supply_type is SupplyType.INTER_STATE:
        return 0, 0, tax_paise
    cgst = tax_paise // 2
    return cgst, tax_paise - cgst, 0
