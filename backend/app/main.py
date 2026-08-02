"""FastAPI application factory and router wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import (
    audit,
    auth,
    clients,
    compliance,
    documents,
    health,
    invoices,
    portal,
    reminders,
    tasks,
)
from app.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.database import check_database

configure_logging()
logger = logging.getLogger(__name__)

DESCRIPTION = """
CAFlow — AI practice management for Indian Chartered Accountants.

Compliance calendar, client management, document intake and billing for CA firms.

### Conventions

* **Money** is always an integer count of **paise** (₹1 = 100 paise). No floats.
* **Dates** are ISO-8601 (`YYYY-MM-DD`); **timestamps** are ISO-8601 with a UTC offset.
* **Lists** are paginated with `limit`/`offset` and return `{items, total, limit, offset}`.
* **Errors** share one shape:
  `{"detail": "...", "error": {"code": "...", "request_id": "...", "fields": [...]}}`.
  `detail` is safe to show a user, `error.code` is stable, and `error.request_id`
  matches the `X-Request-ID` response header and the server log line.

### Authentication

Two token types share the `Authorization: Bearer <token>` header.

* **Practitioner tokens** come from `POST /auth/login` or `POST /auth/register` and
  reach every firm-scoped endpoint. The role (`owner`, `partner`, `manager`,
  `junior`) decides what is writable.
* **Magic-link tokens** come from `POST /clients/{id}/portal-link` and reach only
  `/portal/*`, and only for the one client they were issued for. They are never
  accepted anywhere else.

### Rate limits

Responses carry `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`.
Sign-in and registration have a much tighter budget than the rest of the API; a
`429` includes `Retry-After`.
"""

TAGS_METADATA = [
    {"name": "auth", "description": "Firm registration, sign-in and team management."},
    {
        "name": "clients",
        "description": (
            "The businesses and individuals a firm files for. Creating a client "
            "generates its compliance calendar from its registration flags."
        ),
    },
    {
        "name": "compliance",
        "description": (
            "The statutory calendar: compliance types, per-client filing items, "
            "status transitions and the firm dashboard."
        ),
    },
    {
        "name": "documents",
        "description": (
            "Document intake — upload, AI categorisation, per-filing checklists and "
            "the outstanding-documents chase list."
        ),
    },
    {"name": "tasks", "description": "Internal work items and team workload."},
    {"name": "billing", "description": "Invoices: draft, issue, record payments, chase."},
    {"name": "reminders", "description": "Scheduled client reminders and their delivery state."},
    {
        "name": "portal",
        "description": (
            "The client-facing portal, plus the practitioner endpoints that grant and "
            "revoke access to it."
        ),
    },
    {
        "name": "audit",
        "description": (
            "The append-only record of every mutating action. Read-only, and "
            "restricted to owners and partners."
        ),
    },
    {"name": "meta", "description": "Health and readiness probes."},
]

# Documented once and applied to every route, so the reference shows the error
# envelope without each endpoint repeating it.
COMMON_RESPONSES = {
    401: {"description": "Missing, malformed or expired credentials"},
    403: {"description": "Authenticated, but not allowed to do this"},
    422: {"description": "The request body or query failed validation"},
    429: {"description": "Rate limit exceeded — see `Retry-After`"},
    500: {"description": "Unexpected server error; quote `error.request_id`"},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting %s %s (environment=%s, debug=%s)",
        settings.app_name,
        __version__,
        settings.environment,
        settings.debug,
    )
    ok, error = check_database()
    if ok:
        logger.info("Database reachable")
    else:
        # Not fatal: the readiness probe reports it and the orchestrator holds
        # traffic back, which beats crash-looping while the database boots.
        logger.error("Database unreachable at startup (%s) — /health/ready will fail", error)
    if not settings.rate_limit_enabled:
        logger.warning("Rate limiting is DISABLED")
    yield
    logger.info("Shutting down %s", settings.app_name)


def create_app() -> FastAPI:
    # Passing None for a docs URL is how FastAPI is told not to route it at
    # all: the path 404s like any other unknown one, rather than answering
    # with something that admits a reference exists here. /openapi.json goes
    # with them — leaving the schema served while hiding the two renderers
    # would publish exactly the same inventory in a form that is easier to
    # script against. `app.openapi()` still builds it in-process, so the
    # generated-client and contract checks are unaffected.
    docs = settings.serves_api_docs
    if not docs:
        logger.info("API reference is not routed (ENVIRONMENT=%s)", settings.environment)

    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=__version__,
        summary="Compliance, documents and billing for Indian CA firms.",
        openapi_tags=TAGS_METADATA,
        contact={"name": "CAFlow"},
        license_info={"name": "Proprietary"},
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
        responses=COMMON_RESPONSES,
        lifespan=lifespan,
    )

    # add_middleware wraps, so the LAST one added is the outermost. Read this
    # block bottom-up for the request order:
    #   SecurityHeaders -> CORS -> RequestContext -> BodySizeLimit -> RateLimit
    # Security headers and CORS go outermost so they still apply to a response
    # produced by an inner layer (a 429, a 413, a handled 500). Request context
    # sits above the rest so everything below it logs with a request id.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        # Explicit rather than "*": the browser only needs these, and a
        # wildcard would silently authorise whatever gets added later.
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=[
            "X-Request-ID",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "Content-Disposition",
        ],
        max_age=600,
    )
    app.add_middleware(SecurityHeadersMiddleware)

    register_exception_handlers(app)

    api_prefix = settings.api_v1_prefix
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(clients.router, prefix=api_prefix)
    app.include_router(compliance.router, prefix=api_prefix)
    app.include_router(documents.router, prefix=api_prefix)
    app.include_router(tasks.router, prefix=api_prefix)
    app.include_router(invoices.router, prefix=api_prefix)
    app.include_router(reminders.router, prefix=api_prefix)
    app.include_router(audit.router, prefix=api_prefix)
    # Carries both /clients/{id}/portal-* (practitioner) and /portal/* (client).
    app.include_router(portal.router, prefix=api_prefix)
    # Unprefixed: probes should not move when the API version does.
    app.include_router(health.router)

    return app


app = create_app()
