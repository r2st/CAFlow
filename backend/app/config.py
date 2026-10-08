"""Application settings, loaded from the environment (see .env.example).

Settings are validated at import time. Anything that would be a security or
availability problem in production — a placeholder signing key, a wildcard CORS
origin, DEBUG left on — raises here rather than at the first request that
happens to need it.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Environments that get the strict checks. Anything else (development, test,
# ci) is allowed to run on defaults so a fresh clone starts without ceremony.
PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod", "staging"})

# Refused as a production signing key.
INSECURE_SECRET_KEYS = frozenset(
    {"dev-secret-change-me", "change-me-in-production", "changeme", "secret", ""}
)

MIN_SECRET_KEY_LENGTH = 32


class ConfigError(RuntimeError):
    """Raised when the environment cannot produce a usable configuration."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_name: str = "DoAide Reach"
    environment: str = "development"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"
    # Swagger, ReDoc and the schema they are generated from. Unset means "on
    # in development, off in production": the reference is a complete inventory
    # of every route, every field name and every error code, and anonymous
    # visitors to a production deployment have no use for it that an attacker
    # does not have first. Set DOCS_ENABLED explicitly to override in either
    # direction — true on a staging box someone is integrating against, false
    # on a laptop that is briefly on a conference network.
    docs_enabled: bool | None = None
    # Emit one JSON object per log line. Off in development, where the
    # human-readable format is easier to scan.
    log_json: bool = False
    log_level: str = ""  # blank => DEBUG when debug else INFO

    # --- Database ---
    database_url: str = "postgresql+psycopg://caflow:caflow@localhost:5432/caflow"
    # Connection pooling. Sized for a single uvicorn worker; multiply the
    # totals by the worker count when planning the server's max_connections.
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=20, ge=0, le=200)
    # Recycle below the shortest idle timeout on the path — PgBouncer, RDS
    # proxies and load balancers all drop idle connections silently.
    db_pool_recycle_seconds: int = Field(default=1800, ge=60)
    db_pool_timeout_seconds: int = Field(default=30, ge=1)
    db_echo: bool = False

    # --- Auth ---
    secret_key: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(default=60 * 12, ge=1)
    magic_link_expire_minutes: int = Field(default=60 * 24 * 7, ge=1)

    # --- CORS ---
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- Rate limiting ---
    # Counters live in Redis when it is reachable so limits hold across
    # workers; otherwise they fall back to a per-process in-memory window.
    rate_limit_enabled: bool = True
    # Default budget for authenticated API traffic, per caller per window.
    rate_limit_default_per_minute: int = Field(default=300, ge=1)
    # Anything unauthenticated: login, register, portal.
    rate_limit_anonymous_per_minute: int = Field(default=60, ge=1)
    # Credential endpoints get their own, much tighter bucket.
    rate_limit_auth_per_minute: int = Field(default=10, ge=1)
    # Uploads are expensive (disk plus AI categorisation).
    rate_limit_upload_per_minute: int = Field(default=30, ge=1)
    # Trust X-Forwarded-For for the client IP. Only enable behind a proxy that
    # overwrites the header — otherwise callers can forge their rate-limit key.
    trust_proxy_headers: bool = False
    # ...and only when the connection itself came from one of these. Turning
    # the switch above on is not enough on its own: the stack publishes the API
    # port alongside nginx, so a caller who reaches it directly could otherwise
    # send any X-Forwarded-For it liked and pick a fresh rate-limit bucket for
    # every request. A proxy sits on a private network in every deployment
    # this ships with, so the default closes that without configuration.
    # Set to "*" to trust any peer — only sane if nothing else can reach the port.
    trusted_proxy_ips: str = (
        "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
    )

    # --- Request handling ---
    # Rejected before the body is read, so a huge upload cannot exhaust memory.
    max_request_body_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)
    # Requests slower than this are logged at WARNING.
    slow_request_ms: int = Field(default=1000, ge=1)

    # --- Client portal ---
    # Magic links are built as f"{portal_base_url}?token=...".
    portal_base_url: str = "http://localhost:5173/portal"

    # --- Document storage ---
    # Local filesystem for now; swap for S3-compatible object storage on deploy.
    storage_dir: str = "./var/documents"
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    # How much of a text document to hand the LLM for categorisation.
    document_excerpt_chars: int = Field(default=6000, ge=100)

    # --- Billing ---
    invoice_number_prefix: str = "INV"
    invoice_payment_terms_days: int = Field(default=15, ge=0)
    invoice_gst_rate_bps: int = Field(default=1800, ge=0, le=10000)  # 18.00%
    # Days after the due date at which payment reminders fire.
    payment_reminder_offsets_days: str = "0,7,15,30"

    # --- Document collection reminders ---
    # Days before a filing's due date at which we chase the client for documents.
    document_reminder_offsets_days: str = "15,10,5,2"

    # --- Email delivery (SMTP) ---
    # With no host configured, mail is logged instead of sent. That keeps
    # development and CI from needing a mail server while still exercising the
    # whole dispatch path.
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True  # STARTTLS on the standard submission port
    smtp_use_ssl: bool = False  # implicit TLS, usually port 465
    smtp_timeout_seconds: float = Field(default=20.0, gt=0)
    email_from_address: str = "no-reply@caflow.local"
    email_from_name: str = "DoAide Reach"
    # A reminder is retried on the next dispatcher run until this many tries.
    reminder_max_attempts: int = Field(default=3, ge=1)

    # --- Queue ---
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # --- AI (OpenRouter free models) ---
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    openrouter_fallback_model: str = "google/gemma-3-27b-it:free"
    openrouter_timeout_seconds: float = Field(default=45.0, gt=0)
    # How long one batch job may spend asking the model to word its messages.
    # The nightly queueing runs draft one message per reminder, and a firm's
    # filings cluster on the same offset day, so the count is the firm's client
    # list rather than a handful. Past this the deterministic template is used
    # for the rest of the run: a plainly-worded reminder that goes out beats a
    # well-worded one that was never queued, and Celery's ten-minute limit is
    # what the run is really spending. See app.services.ai.DraftingBudget.
    ai_draft_budget_seconds: float = Field(default=120.0, ge=0)

    # --- Compliance generation ---
    # How many financial years ahead of the client's onboarding to pre-generate.
    compliance_generation_months: int = Field(default=12, ge=1)

    # ------------------------------------------------------------- validators --

    @field_validator("environment")
    @classmethod
    def _normalise_environment(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("docs_enabled", mode="before")
    @classmethod
    def _blank_docs_enabled_means_default(cls, value: object) -> object:
        """``DOCS_ENABLED=`` reads as unset, the way ``LOG_LEVEL=`` does.

        Without this, the blank line .env.example ships refuses to boot: an
        empty string is not a boolean pydantic will accept, so the file that
        documents the setting would be the file that stops the process.
        """
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        value = value.strip().upper()
        allowed = {"", "CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if value not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed - {''})}")
        return value

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        value = value.strip()
        if "://" not in value:
            raise ValueError("DATABASE_URL must be a SQLAlchemy URL, e.g. postgresql+psycopg://…")
        return value

    @field_validator("jwt_algorithm")
    @classmethod
    def _validate_jwt_algorithm(cls, value: str) -> str:
        # HMAC only: the app signs and verifies with the same key. An "alg" of
        # "none" or an asymmetric family here would be a verification hole.
        allowed = {"HS256", "HS384", "HS512"}
        if value not in allowed:
            raise ValueError(f"JWT_ALGORITHM must be one of {sorted(allowed)}")
        return value

    @field_validator("portal_base_url", "openrouter_base_url")
    @classmethod
    def _validate_http_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("must be an absolute http(s) URL")
        return value

    @field_validator("redis_url", "celery_broker_url", "celery_result_backend")
    @classmethod
    def _validate_redis_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("redis://", "rediss://", "unix://", "memory://")):
            raise ValueError("must be a redis://, rediss://, unix:// or memory:// URL")
        return value

    @model_validator(mode="after")
    def _validate_combinations(self) -> Settings:
        if self.smtp_use_tls and self.smtp_use_ssl:
            raise ValueError("Set at most one of SMTP_USE_TLS (STARTTLS) and SMTP_USE_SSL")
        if self.smtp_host and not self.email_from_address:
            raise ValueError("EMAIL_FROM_ADDRESS is required when SMTP_HOST is configured")
        if self.max_request_body_bytes < self.max_upload_bytes:
            raise ValueError(
                "MAX_REQUEST_BODY_BYTES must be at least MAX_UPLOAD_BYTES, or uploads at "
                "the limit are rejected before they are read"
            )

        if self.is_production:
            problems = []
            if self.secret_key in INSECURE_SECRET_KEYS:
                problems.append(
                    "SECRET_KEY is still a placeholder — generate one with "
                    '`python -c "import secrets; print(secrets.token_urlsafe(48))"`'
                )
            elif len(self.secret_key) < MIN_SECRET_KEY_LENGTH:
                problems.append(f"SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters")
            if self.debug:
                problems.append("DEBUG must be false outside development")
            if not self.cors_origin_list:
                problems.append("CORS_ORIGINS must list at least one origin")
            if "*" in self.cors_origin_list:
                problems.append(
                    "CORS_ORIGINS cannot be '*' while credentials are allowed — "
                    "list the exact frontend origins"
                )
            if self.database_url.startswith("sqlite"):
                problems.append("DATABASE_URL points at SQLite; use PostgreSQL in production")
            if self.portal_base_url.startswith("http://"):
                problems.append("PORTAL_BASE_URL must use https — magic links carry a token")
            if problems:
                raise ValueError(
                    f"Invalid configuration for ENVIRONMENT={self.environment}:\n  - "
                    + "\n  - ".join(problems)
                )
        return self

    # -------------------------------------------------------------- accessors --

    @property
    def is_production(self) -> bool:
        return self.environment in PRODUCTION_ENVIRONMENTS

    @property
    def serves_api_docs(self) -> bool:
        """Whether /docs, /redoc and /openapi.json are routed at all.

        The unset default is the interesting case: it follows the environment,
        so a deployment that is production enough to refuse a placeholder
        signing key is also production enough to stop publishing its own map.
        """
        if self.docs_enabled is None:
            return not self.is_production
        return self.docs_enabled

    @property
    def effective_log_level(self) -> str:
        return self.log_level or ("DEBUG" if self.debug else "INFO")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def trusted_proxy_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        """Networks whose forwarded headers are believed.

        An empty list means nothing is trusted, which is the safe way to read a
        malformed setting: the peer address is then used directly, and a caller
        gets its own real address as a rate-limit key rather than one it chose.
        """
        return _network_list(self.trusted_proxy_ips)

    @property
    def trusts_every_proxy(self) -> bool:
        return self.trusted_proxy_ips.strip() == "*"

    @property
    def document_reminder_offsets(self) -> list[int]:
        return _int_list(self.document_reminder_offsets_days)

    @property
    def payment_reminder_offsets(self) -> list[int]:
        return _int_list(self.payment_reminder_offsets_days)


def _network_list(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse a comma-separated list of IPs and CIDRs, ignoring junk entries.

    A bare address is accepted and read as a single-host network, so the
    setting does not force anyone to write ``/32``.
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        part = part.strip()
        if not part or part == "*":
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return networks


def _int_list(raw: str) -> list[int]:
    """Parse a comma-separated day-offset setting, ignoring junk entries."""
    values = []
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            values.append(int(part))
    return sorted(set(values), reverse=True)


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValueError as exc:
        # Pydantic's own report is accurate but noisy; lead with the fix.
        raise ConfigError(
            "DoAide Reach could not start: the environment is invalid.\n"
            "Check your .env against .env.example.\n\n"
            f"{exc}"
        ) from exc


settings = get_settings()
