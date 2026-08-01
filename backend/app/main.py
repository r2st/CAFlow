"""FastAPI application factory and router wiring."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import (
    auth,
    clients,
    compliance,
    documents,
    invoices,
    portal,
    reminders,
    tasks,
)
from app.config import settings

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

DESCRIPTION = """
CAFlow — AI practice management for Indian Chartered Accountants.

Compliance calendar, client management, document intake and billing for CA firms.
All monetary amounts are integers in **paise**.

Two authentication schemes share the `Authorization: Bearer` header:

* **practitioner tokens** (`/auth/login`) reach every firm-scoped endpoint;
* **magic-link tokens** (`/clients/{id}/portal-link`) reach only `/portal/*`,
  and only for the single client they were issued for.
"""


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=__version__,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api_prefix = settings.api_v1_prefix
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(clients.router, prefix=api_prefix)
    app.include_router(compliance.router, prefix=api_prefix)
    app.include_router(documents.router, prefix=api_prefix)
    app.include_router(tasks.router, prefix=api_prefix)
    app.include_router(invoices.router, prefix=api_prefix)
    app.include_router(reminders.router, prefix=api_prefix)
    # Carries both /clients/{id}/portal-* (practitioner) and /portal/* (client).
    app.include_router(portal.router, prefix=api_prefix)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": settings.app_name, "version": __version__}

    return app


app = create_app()
