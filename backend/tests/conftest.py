"""Pytest fixtures.

The suite runs against a throwaway SQLite database so it needs no services.
``DATABASE_URL`` must be set before anything imports ``app.config``, since
settings are cached at import time.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DB = Path(tempfile.mkdtemp(prefix="caflow-tests-")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_TEST_DB}"
# At least 32 bytes, or PyJWT warns on every signature.
os.environ["SECRET_KEY"] = "caflow-test-secret-key-not-for-production-use"
os.environ["OPENROUTER_API_KEY"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import Base, SessionLocal, engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Client, ComplianceItem, Firm, Practitioner  # noqa: E402,F401
from app.seeds import seed_compliance_types  # noqa: E402

FIRM_REGISTRATION = {
    "firm_name": "Sharma & Associates",
    "icai_registration_number": "012345N",
    "firm_email": "office@sharma-ca.in",
    "firm_phone": "+919812345678",
    "pan": "AAACS1234F",
    "city": "Pune",
    "state": "Maharashtra",
    "plan": "practice",
    "owner_full_name": "Anita Sharma",
    "owner_email": "anita@sharma-ca.in",
    "owner_password": "correct-horse-battery",
    "owner_membership_number": "123456",
}


@pytest.fixture
def db() -> Session:
    """A clean, seeded database for each test."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    seed_compliance_types(session)
    session.commit()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db: Session) -> TestClient:
    """A TestClient sharing the test session."""

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def registered_firm(client: TestClient) -> dict:
    """Register a firm and return the token payload."""
    response = client.post("/api/v1/auth/register", json=FIRM_REGISTRATION)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def auth_headers(registered_firm: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {registered_firm['access_token']}"}


@pytest.fixture
def firm_id(registered_firm: dict) -> str:
    return registered_firm["firm"]["id"]


def make_client_payload(**overrides) -> dict:
    """A sensible default client body; override the registration flags per test."""
    payload = {
        "name": "Nimbus Textiles Pvt Ltd",
        "entity_type": "private_limited",
        "pan": "AABCN2345P",
        "gstin": "27AABCN2345P1Z5",
        "contact_person": "Rohit Nair",
        "email": "accounts@nimbustextiles.in",
        "phone": "+919900112233",
        "state": "Maharashtra",
        "gst_registered": True,
        "gst_filing_frequency": "monthly",
        "tds_applicable": True,
        "income_tax_applicable": True,
        "tax_audit_applicable": True,
        "roc_applicable": True,
        "payroll_applicable": False,
    }
    payload.update(overrides)
    return payload
