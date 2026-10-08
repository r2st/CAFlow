"""Liveness and readiness probes."""

from __future__ import annotations

import sys
import types

from fastapi.testclient import TestClient

from app import __version__
from app.api.routes import health as health_route
from app.config import settings


class TestHealth:
    def test_the_shallow_check_reports_the_build(self, client: TestClient):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["version"] == __version__
        assert body["service"] == "DoAide Reach"

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


class FakeRedisClient:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.closed = False

    def ping(self):
        if self.fail:
            raise ConnectionError("Connection refused")
        return True

    def close(self):
        self.closed = True


def install_fake_redis(monkeypatch, client: FakeRedisClient) -> dict:
    """Stand in for the redis package, and record how it was called."""
    calls: dict = {}

    def from_url(url, **kwargs):
        calls["url"] = url
        calls["kwargs"] = kwargs
        return client

    module = types.ModuleType("redis")
    module.from_url = from_url
    monkeypatch.setitem(sys.modules, "redis", module)
    return calls


class TestBrokerCheck:
    def test_a_non_redis_broker_is_reported_reachable_without_dialling(self, monkeypatch):
        # The suite runs on memory://, and an in-process broker is always there.
        monkeypatch.setattr(settings, "celery_broker_url", "memory://")
        monkeypatch.setitem(sys.modules, "redis", None)  # would raise if imported
        assert health_route._check_broker() == (True, None)

    def test_a_reachable_broker_answers_its_ping(self, monkeypatch):
        monkeypatch.setattr(settings, "celery_broker_url", "redis://localhost:6379/1")
        fake = FakeRedisClient()
        install_fake_redis(monkeypatch, fake)

        assert health_route._check_broker() == (True, None)
        # The connection must not be left behind on every probe.
        assert fake.closed is True

    def test_an_unreachable_broker_reports_the_error_type(self, monkeypatch):
        monkeypatch.setattr(settings, "celery_broker_url", "redis://localhost:6379/1")
        install_fake_redis(monkeypatch, FakeRedisClient(fail=True))

        ok, error = health_route._check_broker()
        assert ok is False
        assert error == "ConnectionError"

    def test_the_probe_is_given_a_short_deadline(self, monkeypatch):
        # A probe that hangs is worse than one that reports "not reachable".
        monkeypatch.setattr(settings, "celery_broker_url", "rediss://broker:6379/1")
        calls = install_fake_redis(monkeypatch, FakeRedisClient())

        health_route._check_broker()
        assert calls["kwargs"]["socket_connect_timeout"] == health_route.BROKER_TIMEOUT_SECONDS
        assert calls["kwargs"]["socket_timeout"] == health_route.BROKER_TIMEOUT_SECONDS

    def test_a_missing_redis_package_is_reported_not_reachable(self, monkeypatch):
        # redis is an optional runtime dependency; its absence must not 500.
        monkeypatch.setattr(settings, "celery_broker_url", "redis://localhost:6379/1")
        monkeypatch.setitem(sys.modules, "redis", None)

        ok, error = health_route._check_broker()
        assert ok is False
        # Which subclass a blocked import raises is the interpreter's business:
        # 3.12 says ImportError, 3.14 says ModuleNotFoundError. What readiness
        # promises is that it reports the failure instead of raising.
        assert error in {"ImportError", "ModuleNotFoundError"}
