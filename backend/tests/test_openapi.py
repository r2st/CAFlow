"""The published API reference.

These are contract checks, not documentation polish: a generated client is
built from this schema, so a missing security scheme or an undocumented status
code is a real defect.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def schema() -> dict:
    return app.openapi()


class TestSchema:
    def test_it_is_served(self, client: TestClient):
        assert client.get("/openapi.json").status_code == 200

    def test_swagger_and_redoc_both_render(self, client: TestClient):
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200

    def test_the_description_states_the_money_unit(self, schema):
        # Every amount in this API is an integer count of paise; a client that
        # misses that is off by a factor of a hundred.
        assert "paise" in schema["info"]["description"]

    def test_every_endpoint_has_a_summary(self, schema):
        missing = [
            f"{method.upper()} {path}"
            for path, operations in schema["paths"].items()
            for method, operation in operations.items()
            if not operation.get("summary")
        ]
        assert missing == []

    def test_every_endpoint_is_tagged(self, schema):
        untagged = [
            f"{method.upper()} {path}"
            for path, operations in schema["paths"].items()
            for method, operation in operations.items()
            if not operation.get("tags")
        ]
        assert untagged == []

    def test_every_tag_used_is_described(self, schema):
        described = {tag["name"] for tag in schema["tags"]}
        used = {
            tag
            for operations in schema["paths"].values()
            for operation in operations.values()
            for tag in operation.get("tags", [])
        }
        assert used <= described


class TestSecuritySchemes:
    def test_both_token_types_are_published(self, schema):
        schemes = schema["components"]["securitySchemes"]
        assert schemes["PractitionerToken"]["scheme"] == "bearer"
        assert schemes["PortalMagicLink"]["scheme"] == "bearer"

    def test_the_schemes_explain_where_each_token_comes_from(self, schema):
        schemes = schema["components"]["securitySchemes"]
        assert "/auth/login" in schemes["PractitionerToken"]["description"]
        assert "portal-link" in schemes["PortalMagicLink"]["description"]

    def test_portal_endpoints_ask_for_the_magic_link_scheme(self, schema):
        operation = schema["paths"]["/api/v1/portal/me"]["get"]
        names = {name for requirement in operation["security"] for name in requirement}
        assert names == {"PortalMagicLink"}

    def test_practitioner_endpoints_ask_for_the_practitioner_scheme(self, schema):
        operation = schema["paths"]["/api/v1/clients"]["get"]
        names = {name for requirement in operation["security"] for name in requirement}
        assert names == {"PractitionerToken"}

    def test_sign_in_needs_no_credentials(self, schema):
        assert "security" not in schema["paths"]["/api/v1/auth/login"]["post"]


class TestDocumentedResponses:
    def test_the_shared_error_codes_are_documented(self, schema):
        responses = schema["paths"]["/api/v1/clients"]["get"]["responses"]
        assert {"401", "403", "429", "500"} <= set(responses)

    def test_readiness_documents_its_failure_mode(self, schema):
        assert "503" in schema["paths"]["/health/ready"]["get"]["responses"]

    def test_health_is_not_behind_the_versioned_prefix(self, schema):
        assert "/health" in schema["paths"]
