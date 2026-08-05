"""Request context, security headers, body limits, rate limiting and the
shared error envelope."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.core import ratelimit
from app.core.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    client_ip,
)
from app.main import app

# A whole number of minutes, so the frozen instant below sits exactly on a
# window boundary and ``Retry-After`` is a full window rather than whatever
# fraction of one happened to be left.
FROZEN_NOW = 1_800_000_000.0


@pytest.fixture
def rate_limited(monkeypatch):
    """Turn rate limiting on for one test, on a window that cannot roll under it.

    The counters are fixed-window: ``window_start = now - (now % 60)``. A test
    that spends a second or two filling a bucket is therefore a test that will
    occasionally cross a minute boundary, have its count zeroed half way
    through the loop, and never reach the limit it exists to assert.

    That stayed hidden while signing in was fast. Hashing a password even when
    the email matches nobody — which is what keeps the sign-in clock from
    saying who is a customer — put roughly two seconds between the first
    attempt and the throttled one, and with it a few-percent chance per run of
    a failure that reproduced nowhere.

    Freezing the clock the limiter reads takes the boundary out of the test
    rather than widening a tolerance around it.
    """
    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(time=lambda: FROZEN_NOW))
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


class TestABodyThatNeverDeclaredItsLength:
    """``Content-Length`` is the half of the limit a caller can simply omit.

    ``Transfer-Encoding: chunked`` carries no length, and nothing then stood
    between the socket and Starlette's multipart parser — which puts no size
    limit on a *file* part at all, spools it to a temporary file past one
    megabyte, and hands the route an ``UploadFile`` the route reads whole into
    memory. The per-upload check in ``services.storage`` runs after all of
    that: the 413 it produces is accurate about the file and useless about the
    cost, because the bytes are already on disk and in RAM.

    ``POST /portal/documents`` is the sharp end of it — reachable by anyone
    holding a client's magic link, which is a party outside the firm entirely.
    """

    LIMIT = settings.max_request_body_bytes

    @pytest.fixture
    def portal_headers(self, client, auth_headers, client_id) -> dict[str, str]:
        token = client.post(
            f"/api/v1/clients/{client_id}/portal-link", json={}, headers=auth_headers
        ).json()["token"]
        return {"Authorization": f"Bearer {token}"}

    def _chunked_upload(self, client, headers, fields, path, body_bytes):
        boundary = "----caflow-test"
        head = "".join(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            for name, value in fields.items()
        ) + (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="big.pdf"\r\n'
            f"Content-Type: application/pdf\r\n\r\n"
        )

        def stream():
            # A generator body is what makes httpx omit Content-Length, which
            # is the whole point: this is the shape the declared-length check
            # cannot see.
            yield head.encode()
            yield b"%PDF-1.4\n"
            sent = 0
            while sent < body_bytes:
                chunk = b"A" * (256 * 1024)
                sent += len(chunk)
                yield chunk
            yield f"\r\n--{boundary}--\r\n".encode()

        return client.post(
            path,
            content=stream(),
            headers={
                **headers,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    def test_a_practitioner_upload_is_capped_by_what_arrives(
        self, client: TestClient, auth_headers, client_id
    ):
        response = self._chunked_upload(
            client,
            auth_headers,
            {"client_id": client_id},
            "/api/v1/documents/upload",
            self.LIMIT + 5 * 1024 * 1024,
        )
        assert response.status_code == 413, response.text
        assert response.json()["error"]["code"] == "payload_too_large"
        # The request-body limit, not the per-upload one: this was stopped
        # before it ever reached a file the storage layer could measure.
        assert "Request body exceeds" in response.json()["detail"]

    def test_the_portal_upload_is_capped_too(self, client: TestClient, portal_headers):
        """The one an outside party can reach."""
        response = self._chunked_upload(
            client,
            portal_headers,
            {},
            "/api/v1/portal/documents",
            self.LIMIT + 5 * 1024 * 1024,
        )
        assert response.status_code == 413, response.text
        assert response.json()["error"]["code"] == "payload_too_large"
        # Asserted on the message, not just the code: the storage layer answers
        # 413 too, and it answers it only once the bytes are already spent.
        assert "Request body exceeds" in response.json()["detail"]

    def test_an_undeclared_body_inside_the_limit_still_works(
        self, client: TestClient, auth_headers, client_id
    ):
        """The cap must not turn every chunked upload into a refusal."""
        response = self._chunked_upload(
            client, auth_headers, {"client_id": client_id}, "/api/v1/documents/upload", 1024
        )
        assert response.status_code == 201, response.text

    def test_the_read_stops_rather_than_reporting_afterwards(self):
        """Asserted at the ASGI layer, because that is where the cost is.

        Everything above only proves the status code. What matters is that the
        body is *not drained first*: the refusal has to come out of ``receive``
        while there are still chunks on the wire, or the disk and the memory
        have already been spent by the time anyone says no.
        """
        chunks = [
            {"type": "http.request", "body": b"x" * 400, "more_body": True}
            for _ in range(10)
        ]
        consumed = []

        async def inner(scope, receive, send):  # pragma: no cover - raises first
            while True:
                message = await receive()
                consumed.append(len(message.get("body", b"")))
                if not message.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive():
            return chunks.pop(0)

        async def send(message):  # pragma: no cover - never reached
            raise AssertionError("the app should not have answered")

        middleware = BodySizeLimitMiddleware(inner, max_bytes=1000)
        scope = {"type": "http", "method": "POST", "path": "/x", "headers": []}

        with pytest.raises(StarletteHTTPException) as raised:
            asyncio.run(middleware(scope, receive, send))

        assert raised.value.status_code == 413
        # Two chunks accepted (800 bytes), the third refused at 1200 — and the
        # remaining seven were never pulled off the wire.
        assert consumed == [400, 400]
        assert len(chunks) == 7

    def test_a_non_http_scope_is_passed_straight_through(self):
        seen = []

        async def inner(scope, receive, send):
            seen.append(scope["type"])

        asyncio.run(
            BodySizeLimitMiddleware(inner, max_bytes=10)({"type": "lifespan"}, None, None)
        )
        assert seen == ["lifespan"]


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

    def test_the_client_behind_the_proxy_chain_wins_when_trusted(self, monkeypatch):
        """The production shape: Caddy names the client, nginx appends itself."""
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


class TestForwardedChainsAreReadFromTheRight:
    """A forged prefix must not survive an appending proxy.

    Every proxy in this stack appends rather than replaces — nginx forwards
    ``$proxy_add_x_forwarded_for`` — so whatever a caller puts in the header
    arrives here with the real hops added *after* it. Reading the left-most
    entry would hand that caller the answer. The rule is the right-most entry
    that is not itself a trusted proxy.
    """

    def test_a_forged_prefix_is_ignored_in_favour_of_the_real_hop(self, monkeypatch):
        """What a caller invents sits to the left of what a proxy observed.

        This is Caddy with its ``header_up`` line removed: the forged entry is
        kept and the address Caddy actually accepted is appended to it.
        """
        monkeypatch.setattr(settings, "trust_proxy_headers", True)

        request = fake_request(
            {"x-forwarded-for": "1.2.3.4, 203.0.113.7, 172.18.0.1"}, peer="172.18.0.5"
        )

        assert client_ip(request) == "203.0.113.7"

    def test_a_caller_cannot_escape_by_naming_a_public_address_first(self, monkeypatch):
        """Two forged entries are still two entries to the left of the real one."""
        monkeypatch.setattr(settings, "trust_proxy_headers", True)

        request = fake_request(
            {"x-forwarded-for": "198.51.100.1, 198.51.100.2, 203.0.113.7, 10.0.0.1"}
        )

        assert client_ip(request) == "203.0.113.7"

    def test_a_chain_of_only_trusted_hops_falls_back_to_where_it_started(self, monkeypatch):
        """A caller genuinely inside the network is not turned into a proxy address."""
        monkeypatch.setattr(settings, "trust_proxy_headers", True)

        request = fake_request({"x-forwarded-for": "10.1.2.3, 172.18.0.1"})

        assert client_ip(request) == "10.1.2.3"

    def test_junk_in_the_chain_falls_back_to_the_peer(self, monkeypatch):
        """An unreadable hop means the rest of the chain cannot be placed."""
        monkeypatch.setattr(settings, "trust_proxy_headers", True)

        request = fake_request({"x-forwarded-for": "203.0.113.7, not-an-ip, 10.0.0.1"})

        assert client_ip(request) == "10.0.0.9"

    def test_a_padded_header_is_not_walked_end_to_end(self, monkeypatch):
        """Parsing is capped, so a long header is not free work at a caller's request."""
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        padding = ", ".join(["10.0.0.1"] * 500)

        request = fake_request({"x-forwarded-for": f"203.0.113.7, {padding}"})

        # Every entry within the cap is a trusted hop, so the walk never
        # reaches the real client — and answers with a trusted hop rather
        # than with whatever sat beyond the cap.
        assert client_ip(request) == "10.0.0.1"

    def test_empty_entries_do_not_shift_the_chain(self, monkeypatch):
        """A trailing comma is a formatting quirk, not a hop."""
        monkeypatch.setattr(settings, "trust_proxy_headers", True)

        request = fake_request({"x-forwarded-for": "203.0.113.7, 10.0.0.1,"})

        assert client_ip(request) == "203.0.113.7"


class TestTheDatabaseFailuresCallersAreToldAbout:
    """A database fault is not one shape, and the three it comes in read differently.

    Every one of them is raised deep inside a request and none carries a
    message safe to hand back: a driver error names columns, constraints,
    hosts and sometimes the connection string. So each is mapped to the answer
    a caller can actually act on, and the detail stays in the log beside the
    request id.
    """

    def raising(self, exc):
        """Mount a temporary route that raises ``exc``, and call it."""
        path = "/api/v1/_database_fault_for_tests"

        @app.get(path)
        def fault():
            raise exc

        try:
            with TestClient(app, raise_server_exceptions=False) as bare:
                return bare.get(path)
        finally:
            app.router.routes = [
                route
                for route in app.router.routes
                if getattr(route, "path", "") != path
            ]

    def test_a_constraint_violation_is_a_conflict_rather_than_a_fault(self):
        """The row could not be written because of what is already there.

        Nothing is broken and the caller has something to change, so a 409 with
        a sentence they can read beats a 500 with a request id.
        """
        response = self.raising(
            IntegrityError(
                "INSERT INTO clients",
                {},
                Exception('duplicate key value violates unique constraint "uq_client_firm_pan"'),
            )
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
        # The constraint name is for the log, not for the client.
        assert "uq_client_firm_pan" not in response.text

    def test_a_database_that_is_not_answering_is_a_503_with_a_retry(self):
        """The request was fine; come back shortly."""
        response = self.raising(
            OperationalError("SELECT 1", {}, Exception("connection refused"))
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "service_unavailable"
        assert response.headers["Retry-After"] == "5"
        assert "connection refused" not in response.text

    def test_any_other_database_error_is_a_generic_500(self):
        response = self.raising(SQLAlchemyError("mapper configuration is wrong"))
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "server_error"
        assert "mapper configuration" not in response.text
        assert response.json()["error"]["request_id"]

    def test_a_response_that_fails_its_own_model_is_our_bug_not_the_callers(self):
        """A 422 here would blame the caller for something they cannot fix."""
        response = self.raising(
            ValidationError.from_exception_data("InvoiceOut", [])
        )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "server_error"


class TestStrictTransportSecurity:
    def test_it_is_only_sent_where_there_is_tls_to_pin(
        self, client: TestClient, monkeypatch
    ):
        """In development the app is served over plain HTTP, and an HSTS header
        would pin a browser to an https origin that does not answer."""
        assert "Strict-Transport-Security" not in client.get("/health").headers

        monkeypatch.setattr(settings, "environment", "production")
        header = client.get("/health").headers["Strict-Transport-Security"]
        assert "max-age=31536000" in header
        assert "includeSubDomains" in header


class TestABodyWhoseDeclaredLengthIsNotANumber:
    def test_the_header_is_ignored_rather_than_the_request_refused(
        self, client: TestClient
    ):
        """A malformed ``Content-Length`` says nothing about the body's size.

        The counting half of the limit still applies, so nothing is let through
        unmeasured; what must not happen is a 500 out of ``int()`` on a header
        the caller chose.
        """
        response = client.post(
            "/api/v1/auth/login",
            content=b'{"email":"nobody@example.test","password":"whatever-it-is"}',
            headers={"Content-Type": "application/json", "Content-Length": "not-a-number"},
        )
        assert response.status_code in (401, 422), response.text
