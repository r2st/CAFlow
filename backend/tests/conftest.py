"""Pytest fixtures.

The suite runs against a throwaway SQLite database so it needs no services.
``DATABASE_URL`` must be set before anything imports ``app.config``, since
settings are cached at import time.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="caflow-tests-"))
_TEST_DB = _TEST_ROOT / "test.db"
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_TEST_DB}"
# At least 32 bytes, or PyJWT warns on every signature.
os.environ["SECRET_KEY"] = "caflow-test-secret-key-not-for-production-use"
os.environ["OPENROUTER_API_KEY"] = ""
# Uploads must never land in the working tree.
os.environ["STORAGE_DIR"] = str(_TEST_ROOT / "documents")
os.environ["PORTAL_BASE_URL"] = "https://portal.example.test/portal"
# The suite drives the same endpoints hundreds of times; rate limiting is
# switched on explicitly by the tests that cover it (see test_middleware.py).
os.environ["RATE_LIMIT_ENABLED"] = "false"
# No Redis in CI: rate-limit counters stay in-process and the broker health
# check reports reachable without dialling anything.
os.environ["REDIS_URL"] = "memory://"
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "memory://"

from datetime import timedelta  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core import clock  # noqa: E402
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


@pytest.fixture
def created_client(client: TestClient, auth_headers: dict[str, str]) -> dict:
    """A client with compliance items already generated."""
    response = client.post(
        "/api/v1/clients", json=make_client_payload(), headers=auth_headers
    )
    assert response.status_code == 201, response.text
    return response.json()["client"]


@pytest.fixture
def client_id(created_client: dict) -> str:
    return created_client["id"]


def _calendar_items(client: TestClient, auth_headers: dict[str, str]) -> list[dict]:
    """Every generated compliance item, earliest deadline first."""
    response = client.get(
        "/api/v1/compliance/calendar",
        params={"from_date": "2020-01-01", "to_date": "2035-12-31", "limit": 1000},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def first_item_of_type(
    client: TestClient, auth_headers: dict[str, str], code: str
) -> dict:
    """The earliest generated compliance item for a given compliance-type code.

    Tests need a real item id, and which ones exist depends on today's date, so
    they are looked up rather than hard-coded.
    """
    matches = [
        item
        for item in _calendar_items(client, auth_headers)
        if item["compliance_type_code"] == code
    ]
    assert matches, f"No compliance item generated for {code}"
    return matches[0]


@pytest.fixture
def long_standing_client_id(client: TestClient, auth_headers: dict[str, str]) -> str:
    """A client the firm has acted for a year, so its calendar reaches back.

    A client onboarded this morning has no deadline that has already passed —
    generation starts at the onboarding date — and a filing date cannot be in
    the future, so a test about a lodged return has nowhere to put one.
    """
    response = client.post(
        "/api/v1/clients",
        json=make_client_payload(
            onboarded_on=(clock.today() - timedelta(days=365)).isoformat()
        ),
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["client"]["id"]


def first_lapsed_item_of_type(
    client: TestClient, auth_headers: dict[str, str], code: str
) -> dict:
    """The earliest item of that type whose statutory deadline has passed.

    The counterpart to :func:`first_item_of_type` for tests that file a return:
    filing happens on or before today, so the deadline has to be behind us for
    both the on-time and the delayed side of the split to be reachable.
    """
    today = clock.today().isoformat()
    item = next(
        (
            candidate
            for candidate in _calendar_items(client, auth_headers)
            if candidate["compliance_type_code"] == code
            and candidate["due_date"] < today
        ),
        None,
    )
    assert item is not None, f"No lapsed compliance item generated for {code}"
    return item


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
