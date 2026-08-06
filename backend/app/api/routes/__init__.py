"""Route modules, and the checks more than one of them makes."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.schemas.common import check_pan_gstin_agreement

__all__ = ["reject_mismatched_pan_gstin"]


def reject_mismatched_pan_gstin(pan: str | None, gstin: str | None) -> None:
    """422 if a PAN and a GSTIN cannot both belong to one person.

    The schemas compare the two whenever a single request carries both. This is
    for the other half: a PATCH that moves one of them against the other
    already on the record, where the pair only becomes whole once the stored
    row is in hand. Both the client and the firm are edited that way, and the
    consequence is the same on either — see
    :func:`~app.schemas.common.check_pan_gstin_agreement`.

    A 422 rather than a 409, because this is the shape of the submitted data
    being wrong and not a conflict with anything else the firm holds. It is
    what a caller sending both in one request is already answered.
    """
    try:
        check_pan_gstin_agreement(pan, gstin)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
