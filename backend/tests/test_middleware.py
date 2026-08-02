"""Request context, security headers, body limits, rate limiting and the
shared error envelope."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import ratelimit
from app.core.middleware import RequestContextMiddleware, client_ip
from app.main import app


@pytest.fixture
def rate_limited():
    """Turn rate limiting on for one test and leave the counters clean."""
    asyncio.run(ratelimit.reset_counters())
    settings.rate_limit_enabled = True
    try:
        yield
    finally:
        settings.rate_limit_enabled = False
        asyncio.run(ratelimit.reset_counters())


class TestRequestContext:
    def test_every_response_carries_a_request_id(self, client: TestClient):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.headers["X-Request-ID"]

    def test_two_requests_get_different_ids(self, client: TestClient):
        first = client.get("/health").headers["X-Request-ID"]
        second = client.get("/health").headers["X-Request-ID"]
        assert first != second

    def test_an_inbound_request_id_is_honoured(self, client: TestClient):
        response = client.get("/health", headers={"X-Request-ID": "trace-abc123"})
        assert response.headers["X-Request-ID"] == "trace-abc123"

    def test_a_hostile_inbound_request_id_is_replaced(self, client: TestClient):
        # Newlines would let a caller forge extra log lines.
        response = client.get("/health", headers={"X-Request-ID": "abc def"})
        assert response.headers["X-Request-ID"] != "abc def"

    def test_an_over_long_inbound_request_id_is_replaced(self, client: TestClient):
        oversized = "a" * (RequestContextMiddleware.MAX_INBOUND_ID_LENGTH + 1)
        response = client.get("/health", headers={"X-Request-ID": oversized})
        assert response.headers["X-Request-ID"] != oversized

    def test_responses_report_their_own_duration(self, client: TestClient):
        assert client.get("/health").headers["Server-Timing"].startswith("app;dur=")


class TestSecurityHeaders:
    def test_hardening_headers_are_present(self, client: TestClient):
        headers = client.get("/health").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

    def test_hsts_is_only_sent_in_production(self, client: TestClient):
        assert "Strict-Transport-Security" not in client.get("/health").headers

    def test_the_docs_page_gets_a_policy_that_lets_it_render(self, client: TestClient):
        policy = client.get("/docs").headers["Content-Security-Policy"]
        assert "cdn.jsdelivr.net" in policy


class TestErrorEnvelope:
    def test_a_404_carries_a_code_and_the_request_id(self, client: TestClient):
        response = client.get("/api/v1/clients/00000000-0000-0000-0000-000000000000")
        assert response.status_code in (401, 404)
        body = response.json()
        assert isinstance(body["detail"], str)
        assert body["error"]["request_id"] == response.headers["X-Request-ID"]

    def test_an_unauthenticated_call_is_401_with_a_stable_code(self, client: TestClient):
        response = client.get("/api/v1/clients")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"

    def test_validation_errors_list_the_offending_fields(self, client: TestClient):
        response = client.post("/api/v1/auth/login", json={"email": "not-an-email"})
        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "validation_error"
        fields = {field["field"] for field in body["error"]["fields"]}
        assert {"email", "password"} & fields
        # The summary is a plain sentence, not a pydantic dump.
        assert isinstance(body["detail"], str)

    def test_the_field_name_drops_the_body_prefix(self, client: TestClient):
        response = client.post("/api/v1/auth/login", json={"email": "x", "password": "y"})
        fields = [field["field"] for field in response.json()["error"]["fields"]]
        assert all(not name.startswith("body") for name in fields)

    def test_an_unhandled_exception_does_not_leak_its_message(self, db):
        secret = "connection string with a password in it"

        @app.get("/api/v1/_boom_for_tests")
        def boom():
            raise RuntimeError(secret)

        try:
            # raise_server_exceptions=False makes TestClient behave like a real
            # server: the handler's response is returned instead of the
            # exception being re-raised into the test.
            with TestClient(app, raise_server_exceptions=False) as bare_client:
                response = bare_client.get("/api/v1/_boom_for_tests")
            assert response.status_code == 500
            body = response.json()
            assert secret not in response.text
            assert body["error"]["code"] == "server_error"
            # The caller still gets something to quote to support.
            assert body["error"]["request_id"]
        finally:
            app.router.routes = [
                route
                for route in app.router.routes
                if getattr(route, "path", "") != "/api/v1/_boom_for_tests"
            ]


class TestBodySizeLimit:
    def test_an_oversized_declared_body_is_rejected_before_it_is_read(
        self, client: TestClient
    ):
        response = client.post(
            "/api/v1/auth/login",
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(settings.max_request_body_bytes + 1),
            },
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"


class TestRateLimiting:
    def test_sign_in_attempts_are_capped(self, client: TestClient, rate_limited):
        limit = settings.rate_limit_auth_per_minute
        payload = {"email": "nobody@example.com", "password": "wrong-password"}

        statuses = [
            client.post("/api/v1/auth/login", json=payload).status_code
            for _ in range(limit + 2)
        ]
        assert statuses[0] == 401  # the limit is not so tight it blocks a first try
        assert 429 in statuses

    def test_a_throttled_response_says_when_to_retry(self, client: TestClient, rate_limited):
        payload = {"email": "nobody@example.com", "password": "wrong-password"}
        response = None
        for _ in range(settings.rate_limit_auth_per_minute + 2):
            response = client.post("/api/v1/auth/login", json=payload)
            if response.status_code == 429:
                break
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) >= 1
        assert response.headers["X-RateLimit-Limit"] == str(settings.rate_limit_auth_per_minute)
        assert response.json()["error"]["code"] == "rate_limited"

    def test_allowed_responses_report_the_remaining_budget(
        self, client: TestClient, rate_limited
    ):
        response = client.get("/api/v1/clients")
        assert int(response.headers["X-RateLimit-Remaining"]) >= 0

    def test_health_probes_are_never_throttled(self, client: TestClient, rate_limited):
        # Well past the anonymous budget.
        for _ in range(settings.rate_limit_anonymous_per_minute + 5):
            assert client.get("/health/live").status_code == 200

    def test_two_practitioners_do_not_share_a_budget(
        self, client: TestClient, registered_firm, rate_limited
    ):
        # The bucket key is derived from the token subject, so one busy user
        # must not lock out the rest of the firm.
        token = registered_firm["access_token"]
        authed = client.get("/api/v1/clients", headers={"Authorization": f"Bearer {token}"})
        anonymous = client.get("/api/v1/clients")
        assert authed.status_code == 200
        assert anonymous.status_code == 401
        assert authed.headers["X-RateLimit-Limit"] == str(settings.rate_limit_default_per_minute)
        assert anonymous.headers["X-RateLimit-Limit"] == str(
            settings.rate_limit_anonymous_per_minute
        )


class TestBucketSelection:
    def test_sign_in_uses_the_tight_bucket(self):
        bucket = ratelimit.bucket_for("/api/v1/auth/login", "POST", authenticated=False)
        assert bucket.name == "auth"

    def test_uploads_have_their_own_bucket(self):
        bucket = ratelimit.bucket_for("/api/v1/documents/upload", "POST", authenticated=True)
        assert bucket.name == "upload"

    def test_portal_uploads_share_the_upload_bucket(self):
        bucket = ratelimit.bucket_for("/api/v1/portal/documents", "POST", authenticated=True)
        assert bucket.name == "upload"

    def test_reading_the_login_route_is_not_treated_as_a_sign_in(self):
        bucket = ratelimit.bucket_for("/api/v1/auth/login", "GET", authenticated=True)
        assert bucket.name == "default"


class TestInMemoryCounter:
    def test_it_allows_up_to_the_limit_then_refuses(self):
        counter = ratelimit.InMemoryCounter()

        async def run():
            return [await counter.hit("k", 3, 60) for _ in range(4)]

        decisions = asyncio.run(run())
        assert [d.allowed for d in decisions] == [True, True, True, False]
        assert decisions[2].remaining == 0

    def test_separate_keys_have_separate_budgets(self):
        counter = ratelimit.InMemoryCounter()

        async def run():
            await counter.hit("a", 1, 60)
            return await counter.hit("b", 1, 60)

        assert asyncio.run(run()).allowed


def fake_request(headers=None, peer="10.0.0.9"):
    """A request as ``client_ip`` sees it: some headers and a peer address."""

    class FakeClient:
        host = peer

    class FakeRequest:
        pass

    request = FakeRequest()
    request.headers = headers or {}
    request.client = FakeClient() if peer is not None else None
    return request


class TestClientIp:
    def test_forwarded_headers_are_ignored_by_default(self, client: TestClient):
        # Trusting X-Forwarded-For unconditionally would let a caller pick
        # their own rate-limit bucket.
        assert settings.trust_proxy_headers is False

    def test_the_left_most_forwarded_address_wins_when_trusted(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)

        request = fake_request({"x-forwarded-for": "203.0.113.7, 10.0.0.1"})

        assert client_ip(request) == "203.0.113.7"

    def test_x_real_ip_is_used_when_there_is_no_forwarded_for(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)

        request = fake_request({"x-real-ip": "203.0.113.9"})

        assert client_ip(request) == "203.0.113.9"


class TestForwardedHeadersFromUntrustedPeers:
    """The switch is not enough on its own.

    ``docker-compose`` publishes the API port alongside nginx, so a caller can
    reach the app directly. If the header were believed on that connection, it
    could name a different address on every request and never meet a rate
    limit at all.
    """

    def test_a_direct_caller_cannot_forge_its_own_address(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)

        request = fake_request(
            {"x-forwarded-for": "203.0.113.7"}, peer="198.51.100.4"
        )

        assert client_ip(request) == "198.51.100.4"

    def test_a_request_with_no_peer_is_not_trusted_either(self, monkeypatch):
        """No peer means nothing to check the header against."""
        monkeypatch.setattr(settings, "trust_proxy_headers", True)

        request = fake_request({"x-forwarded-for": "203.0.113.7"}, peer=None)

        assert client_ip(request) == "unknown"

    def test_the_proxy_nginx_runs_behind_is_trusted_out_of_the_box(self, monkeypatch):
        """The compose network is 172.16/12, and it must work unconfigured."""
        monkeypatch.setattr(settings, "trust_proxy_headers", True)

        request = fake_request({"x-forwarded-for": "203.0.113.7"}, peer="172.18.0.5")

        assert client_ip(request) == "203.0.113.7"

    def test_a_star_trusts_whatever_opened_the_connection(self, monkeypatch):
        """For a deployment where nothing but the proxy can reach the port."""
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        monkeypatch.setattr(settings, "trusted_proxy_ips", "*")

        request = fake_request({"x-forwarded-for": "203.0.113.7"}, peer="198.51.100.4")

        assert client_ip(request) == "203.0.113.7"

    def test_a_forwarded_value_that_is_not_an_address_is_discarded(self, monkeypatch):
        """Junk must not key a counter or reach a log line."""
        monkeypatch.setattr(settings, "trust_proxy_headers", True)

        request = fake_request({"x-forwarded-for": "not-an-ip"})

        assert client_ip(request) == "10.0.0.9"

    def test_an_unparseable_trust_list_trusts_nothing(self, monkeypatch):
        """Reading a malformed setting as 'trust nobody' is the safe direction."""
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        monkeypatch.setattr(settings, "trusted_proxy_ips", "garbage,,also-garbage")

        request = fake_request({"x-forwarded-for": "203.0.113.7"})

        assert client_ip(request) == "10.0.0.9"
