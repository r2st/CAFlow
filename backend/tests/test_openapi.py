"""The published API reference.

These are contract checks, not documentation polish: a generated client is
built from this schema, so a missing security scheme or an undocumented status
code is a real defect.

``TestTheReferenceIsNotPublishedInProduction`` is the other half: the same
schema that a client generator needs is a complete inventory of every route,
every field name and every error code, and a production deployment hands it to
anyone who asks for it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, create_app


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


PRODUCTION = {
    "environment": "production",
    "debug": False,
    "secret_key": "x" * 48,
    "database_url": "postgresql+psycopg://caflow:pw@db:5432/caflow",
    "portal_base_url": "https://portal.example.com/portal",
    "cors_origins": "https://app.example.com",
    "_env_file": None,
}

REFERENCE_PATHS = ["/docs", "/redoc", "/openapi.json"]


@pytest.fixture
def app_under(monkeypatch):
    """Builds a fresh app against a settings object of the test's choosing.

    ``create_app`` reads the module-level ``settings`` that every other test in
    the suite shares, so the substitution has to happen in ``app.main``'s own
    namespace and be undone afterwards — which is what ``monkeypatch`` is for.
    """

    def build(**overrides):
        monkeypatch.setattr("app.main.settings", Settings(**{**PRODUCTION, **overrides}))
        return create_app()

    return build


class TestTheReferenceIsNotPublishedInProduction:
    """A full map of the API, served to anonymous visitors, by default.

    Every route, every field name, every error code and every enum value the
    app knows about — which is the first thing anyone enumerating it would
    otherwise have to guess at. Development still gets all three, because the
    reference is genuinely how the frontend is written against the backend.
    """

    @pytest.mark.parametrize("path", REFERENCE_PATHS)
    def test_production_does_not_route_it(self, app_under, path):
        with TestClient(app_under()) as client:
            assert client.get(path).status_code == 404

    @pytest.mark.parametrize("path", REFERENCE_PATHS)
    def test_development_still_routes_it(self, app_under, path):
        development = app_under(environment="development", debug=True)

        with TestClient(development) as client:
            assert client.get(path).status_code == 200

    @pytest.mark.parametrize("path", REFERENCE_PATHS)
    def test_production_can_turn_it_back_on_deliberately(self, app_under, path):
        """A staging box someone is integrating against is a real case.

        The point is that it takes saying so. ``DOCS_ENABLED=true`` in the
        environment file is a decision with a name on it; the default is not.
        """
        with TestClient(app_under(docs_enabled=True)) as client:
            assert client.get(path).status_code == 200

    @pytest.mark.parametrize("path", REFERENCE_PATHS)
    def test_development_can_turn_it_off_deliberately(self, app_under, path):
        with TestClient(app_under(environment="development", debug=True, docs_enabled=False)) as (
            client
        ):
            assert client.get(path).status_code == 404

    def test_the_schema_is_still_built_in_process(self, app_under):
        """Hiding the route must not cost the contract checks their subject.

        ``app.openapi()`` is what the tests above this line read, and what a
        client generator is pointed at from a checkout rather than over the
        wire. Gating the *route* is the whole intent; gating the schema itself
        would mean production ran a build nothing had validated.
        """
        schema = app_under().openapi()

        assert "/api/v1/clients" in schema["paths"]
        assert "paise" in schema["info"]["description"]

    def test_the_hidden_schema_is_not_reachable_by_asking_the_docs_for_it(self, app_under):
        """Swagger UI fetches /openapi.json; both go, or neither is hidden.

        Leaving the schema routed while dropping the two renderers would
        publish the identical inventory in the form that is easier to script
        against, which is the opposite of the trade being made here.
        """
        with TestClient(app_under()) as client:
            assert client.get("/docs/oauth2-redirect").status_code == 404
            assert client.get("/openapi.json").status_code == 404
