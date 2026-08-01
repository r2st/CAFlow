"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import __version__
from app.api.routes import health as health_route


class TestHealth:
    def test_the_shallow_check_reports_the_build(self, client: TestClient):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["version"] == __version__
        assert body["service"] == "CAFlow"

    def test_liveness_never_touches_a_dependency(self, client: TestClient, monkeypatch):
        # A database outage must not restart the container.
        monkeypatch.setattr(
            "app.database.check_database", lambda: (_ for _ in ()).throw(AssertionError())
        )
        assert client.get("/health/live").status_code == 200

    def test_probes_sit_outside_the_versioned_prefix(self, client: TestClient):
        # Orchestrator config should not have to change when /api/v1 does.
        assert client.get("/api/v1/health").status_code == 404


class TestReadiness:
    def test_it_reports_each_dependency(self, client: TestClient):
        response = client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["checks"]["database"]["ok"] is True
        assert body["checks"]["database"]["required"] is True
        assert "broker" in body["checks"]
        assert body["duration_ms"] >= 0

    def test_it_returns_503_when_the_database_is_down(self, client: TestClient, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.health.check_database", lambda: (False, "OperationalError")
        )
        response = client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["checks"]["database"]["error"] == "OperationalError"

    def test_a_broker_outage_alone_does_not_fail_readiness(
        self, client: TestClient, monkeypatch
    ):
        # The API serves fine without the queue; reminders just queue up.
        monkeypatch.setattr(health_route, "_check_broker", lambda: (False, "ConnectionError"))
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["checks"]["broker"]["ok"] is False

    def test_it_exposes_pool_gauges(self, client: TestClient):
        pool = client.get("/health/ready").json()["checks"]["database"]["pool"]
        assert "kind" in pool
