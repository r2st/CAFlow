"""Global exception handling.

Every error leaves the API in the same shape, whatever raised it:

    {
      "detail": "Client not found",           # human-readable, safe to show
      "error": {
        "code": "not_found",                  # stable, machine-readable
        "request_id": "0f9c…",                # matches the log line
        "fields": [{"field": "pan", "message": "..."}]   # 422 only
      }
    }

``detail`` is kept at the top level because that is what FastAPI produces by
default and what the frontend already reads; ``error`` is additive.

Unhandled exceptions never leak their message or traceback to the caller —
they are logged in full with the request id, and the client gets that id so a
support conversation can point at the exact log line.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_request_id
from app.services import storage

logger = logging.getLogger(__name__)

# Status code -> stable error code. Anything unmapped falls back to
# "http_error"/"server_error", so a new status never breaks a client.
STATUS_CODES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthenticated",
    status.HTTP_402_PAYMENT_REQUIRED: "plan_limit_reached",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_410_GONE: "gone",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: "payload_too_large",
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "unsupported_media_type",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "validation_error",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "server_error",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
}

GENERIC_SERVER_ERROR = (
    "Something went wrong on our side. The problem has been logged — quote the "
    "request id below if you contact support."
)


def request_id_for(request: Request | None) -> str:
    """The current request id.

    Prefers ``request.state`` over the ContextVar: Starlette's
    ``ServerErrorMiddleware`` sits outside every application middleware, so by
    the time a handler runs for an unhandled exception the ContextVar has
    already been reset. ``state`` is backed by the ASGI scope and survives.
    """
    if request is not None:
        stored = getattr(request.state, "request_id", "")
        if stored:
            return stored
    return get_request_id()


def error_response(
    *,
    status_code: int,
    detail: str,
    code: str | None = None,
    fields: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
    request: Request | None = None,
) -> JSONResponse:
    """Build the canonical error body."""
    request_id = request_id_for(request)
    error: dict[str, Any] = {
        "code": code or STATUS_CODES.get(status_code, "http_error"),
        "request_id": request_id,
    }
    if fields:
        error["fields"] = fields
    merged = dict(headers or {})
    if request_id:
        merged.setdefault("X-Request-ID", request_id)
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "error": error},
        headers=merged,
    )


def _field_errors(exc: RequestValidationError | ValidationError) -> list[dict[str, str]]:
    """Flatten pydantic's error list into ``field``/``message`` pairs.

    The first element of ``loc`` is the source ("body", "query", "path") and
    is dropped: callers care which field is wrong, not where FastAPI found it.
    """
    fields = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ())]
        if location and location[0] in ("body", "query", "path", "header", "cookie"):
            location = location[1:]
        fields.append(
            {"field": ".".join(location) or "body", "message": error.get("msg", "Invalid value")}
        )
    return fields


def _summarise(fields: list[dict[str, str]]) -> str:
    if not fields:
        return "The request could not be validated"
    head = "; ".join(f"{f['field']}: {f['message']}" for f in fields[:3])
    return head if len(fields) <= 3 else f"{head}; and {len(fields) - 3} more"


def register_exception_handlers(app: FastAPI) -> None:
    """Install every handler on ``app``. Ordering follows specificity."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Route-raised HTTPExceptions already carry a caller-safe message.
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        if exc.status_code >= 500:
            logger.error(
                "Server-side HTTPException on %s %s: %s",
                request.method,
                request.url.path,
                detail,
                extra={"status_code": exc.status_code, "path": request.url.path},
            )
        return error_response(
            status_code=exc.status_code,
            detail=detail,
            headers=dict(exc.headers) if exc.headers else None,
            request=request,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = _field_errors(exc)
        logger.info(
            "Rejected %s %s: %d validation error(s)",
            request.method,
            request.url.path,
            len(fields),
            extra={"path": request.url.path, "fields": fields},
        )
        return error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_summarise(fields),
            fields=fields,
            request=request,
        )

    @app.exception_handler(ValidationError)
    async def model_validation_exception(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        # A response model that fails to validate is our bug, not the caller's.
        logger.error(
            "Response validation failed on %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
            extra={"path": request.url.path},
        )
        return error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_SERVER_ERROR,
            request=request,
        )

    @app.exception_handler(storage.UploadTooLarge)
    async def upload_too_large(request: Request, exc: storage.UploadTooLarge) -> JSONResponse:
        return error_response(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
            request=request,
        )

    @app.exception_handler(storage.UnsupportedFileType)
    async def unsupported_file(
        request: Request, exc: storage.UnsupportedFileType
    ) -> JSONResponse:
        return error_response(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
            request=request,
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        # A unique/foreign-key violation is a conflict, not a server fault.
        # The driver message names columns and constraints, so it is logged
        # rather than returned.
        logger.warning(
            "Integrity error on %s %s: %s",
            request.method,
            request.url.path,
            exc.orig,
            extra={"path": request.url.path},
        )
        return error_response(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "That change conflicts with an existing record. It may already "
                "exist, or something it refers to has been removed."
            ),
            request=request,
        )

    @app.exception_handler(OperationalError)
    async def operational_error(request: Request, exc: OperationalError) -> JSONResponse:
        logger.error(
            "Database unavailable on %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
            extra={"path": request.url.path},
        )
        return error_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The database is temporarily unavailable. Please retry shortly.",
            headers={"Retry-After": "5"},
            request=request,
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error(
            "Database error on %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
            extra={"path": request.url.path},
        )
        return error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_SERVER_ERROR,
            request=request,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled %s on %s %s",
            type(exc).__name__,
            request.method,
            request.url.path,
            extra={"path": request.url.path},
        )
        return error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_SERVER_ERROR,
            request=request,
        )
