"""Startup configuration validation.

Each case builds a ``Settings`` directly so it exercises the validators
without disturbing the process-wide singleton the rest of the suite uses.
"""

from __future__ import annotations

import pytest

from app.config import ConfigError, Settings, get_settings

PRODUCTION = {
    "environment": "production",
    "debug": False,
    "secret_key": "x" * 48,
    "database_url": "postgresql+psycopg://caflow:pw@db:5432/caflow",
    "portal_base_url": "https://portal.example.com/portal",
    "cors_origins": "https://app.example.com",
    # Loading the developer's own .env would defeat the point of the test.
    "_env_file": None,
}


def production(**overrides) -> Settings:
    return Settings(**{**PRODUCTION, **overrides})


class TestProductionGuards:
    def test_a_well_formed_production_config_validates(self):
        settings = production()
        assert settings.is_production
        assert settings.cors_origin_list == ["https://app.example.com"]

    def test_the_placeholder_secret_key_is_refused(self):
        with pytest.raises(ValueError, match="SECRET_KEY"):
            production(secret_key="change-me-in-production")

    def test_a_short_secret_key_is_refused(self):
        with pytest.raises(ValueError, match="at least 32"):
            production(secret_key="too-short")

    def test_debug_cannot_be_left_on(self):
        with pytest.raises(ValueError, match="DEBUG"):
            production(debug=True)

    def test_a_wildcard_cors_origin_is_refused(self):
        # Credentials are allowed, so "*" would let any site read the API.
        with pytest.raises(ValueError, match="CORS_ORIGINS"):
            production(cors_origins="*")

    def test_empty_cors_origins_are_refused(self):
        with pytest.raises(ValueError, match="CORS_ORIGINS"):
            production(cors_origins="  ,  ")

    def test_sqlite_is_refused(self):
        with pytest.raises(ValueError, match="SQLite"):
            production(database_url="sqlite+pysqlite:///./caflow.db")

    def test_a_plaintext_portal_url_is_refused(self):
        # The magic link carries a bearer token in the query string.
        with pytest.raises(ValueError, match="https"):
            production(portal_base_url="http://portal.example.com/portal")

    def test_staging_is_held_to_the_same_bar(self):
        with pytest.raises(ValueError, match="SECRET_KEY"):
            production(environment="staging", secret_key="dev-secret-change-me")


class TestDevelopmentDefaults:
    def test_defaults_start_without_ceremony(self):
        settings = Settings(_env_file=None)
        assert not settings.is_production
        assert settings.debug is True

    def test_the_environment_name_is_case_insensitive(self):
        assert production(environment="PRODUCTION  ").is_production
        assert Settings(_env_file=None, environment="  Development ").environment == "development"


class TestFieldValidation:
    def test_an_asymmetric_jwt_algorithm_is_refused(self):
        # The app signs and verifies with one shared key.
        with pytest.raises(ValueError, match="JWT_ALGORITHM"):
            Settings(_env_file=None, jwt_algorithm="RS256")

    def test_the_none_algorithm_is_refused(self):
        with pytest.raises(ValueError, match="JWT_ALGORITHM"):
            Settings(_env_file=None, jwt_algorithm="none")

    def test_a_relative_portal_url_is_refused(self):
        with pytest.raises(ValueError, match="absolute"):
            Settings(_env_file=None, portal_base_url="/portal")

    def test_a_non_redis_queue_url_is_refused(self):
        with pytest.raises(ValueError, match="redis"):
            Settings(_env_file=None, redis_url="http://localhost:6379")

    def test_an_unknown_log_level_is_refused(self):
        with pytest.raises(ValueError, match="LOG_LEVEL"):
            Settings(_env_file=None, log_level="chatty")

    def test_both_smtp_tls_modes_cannot_be_set(self):
        with pytest.raises(ValueError, match="SMTP_USE_TLS"):
            Settings(_env_file=None, smtp_use_tls=True, smtp_use_ssl=True)

    def test_the_body_limit_must_admit_a_maximum_upload(self):
        with pytest.raises(ValueError, match="MAX_REQUEST_BODY_BYTES"):
            Settings(_env_file=None, max_upload_bytes=1024 * 1024, max_request_body_bytes=2048)

    def test_a_zero_pool_is_refused(self):
        with pytest.raises(ValueError):
            Settings(_env_file=None, db_pool_size=0)


class TestDerivedValues:
    def test_reminder_offsets_are_parsed_and_ordered(self):
        settings = Settings(_env_file=None, document_reminder_offsets_days="5, 15,junk, 5,2")
        assert settings.document_reminder_offsets == [15, 5, 2]

    def test_the_log_level_follows_debug_when_unset(self):
        assert Settings(_env_file=None, debug=True).effective_log_level == "DEBUG"
        assert Settings(_env_file=None, debug=False).effective_log_level == "INFO"

    def test_an_explicit_log_level_wins(self):
        assert Settings(_env_file=None, debug=True, log_level="warning").effective_log_level == (
            "WARNING"
        )


class TestStartupFailure:
    def test_the_failure_message_points_at_env_example(self, monkeypatch):
        monkeypatch.setenv("JWT_ALGORITHM", "RS256")
        get_settings.cache_clear()
        try:
            with pytest.raises(ConfigError, match=".env.example"):
                get_settings()
        finally:
            monkeypatch.delenv("JWT_ALGORITHM", raising=False)
            get_settings.cache_clear()
            get_settings()
