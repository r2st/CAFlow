"""Engine configuration, the request-scoped session, and the health helpers."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import _engine_kwargs, check_database, get_db, pool_status


class TestEngineConfiguration:
    def test_sqlite_gets_only_the_dialect_knob_it_needs(self):
        # The test-suite's database has no meaningful pool to size.
        kwargs = _engine_kwargs("sqlite+pysqlite:///./test.db")
        assert kwargs == {"connect_args": {"check_same_thread": False}}

    def test_postgres_gets_a_sized_pool(self):
        kwargs = _engine_kwargs("postgresql+psycopg://caflow:caflow@db:5432/caflow")
        assert kwargs["pool_size"] == settings.db_pool_size
        assert kwargs["max_overflow"] == settings.db_max_overflow
        assert kwargs["pool_timeout"] == settings.db_pool_timeout_seconds

    def test_connections_are_pinged_before_they_are_handed_out(self):
        # Without this, a connection killed server-side surfaces as a failed
        # request rather than a reconnect.
        assert _engine_kwargs("postgresql+psycopg://x@db/y")["pool_pre_ping"] is True

    def test_connections_are_recycled_below_the_shortest_idle_timeout(self):
        kwargs = _engine_kwargs("postgresql+psycopg://x@db/y")
        assert kwargs["pool_recycle"] == settings.db_pool_recycle_seconds

    def test_the_application_names_itself_to_the_server(self):
        # The API and the Celery worker share a database; pg_stat_activity has
        # to be able to tell them apart.
        kwargs = _engine_kwargs("postgresql+psycopg://x@db/y")
        assert kwargs["connect_args"]["application_name"] == settings.app_name


class TestRequestSession:
    def test_it_yields_a_usable_session_and_closes_it(self):
        generator = get_db()
        session = next(generator)
        assert session.is_active

        with pytest.raises(StopIteration):
            next(generator)

    def test_a_failed_request_rolls_back_before_the_session_is_reused(self):
        # A session handed back mid-transaction poisons the next borrower.
        generator = get_db()
        session = next(generator)
        rolled_back = []
        session.rollback = lambda: rolled_back.append(True)

        with pytest.raises(ValueError):
            generator.throw(ValueError("route blew up"))

        assert rolled_back == [True]


class TestHealthHelpers:
    def test_a_reachable_database_reports_ok(self):
        assert check_database() == (True, None)

    def test_an_unreachable_database_reports_the_error_type(self, monkeypatch):
        def explode():
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        monkeypatch.setattr("app.database.engine.connect", explode)
        ok, error = check_database()

        assert ok is False
        # The type, not the message: a driver error can carry the DSN.
        assert error == "OperationalError"
        assert "connection refused" not in (error or "")

    def test_pool_gauges_are_reported_when_the_dialect_has_a_pool(self):
        status = pool_status()
        assert "kind" in status
        if "size" in status:
            assert status["checked_in"] >= 0
            assert status["checked_out"] >= 0

    def test_a_pool_less_dialect_reports_only_its_kind(self, monkeypatch):
        class PoolWithoutGauges:
            pass

        monkeypatch.setattr("app.database.engine.pool", PoolWithoutGauges())
        assert pool_status() == {"kind": "PoolWithoutGauges"}
