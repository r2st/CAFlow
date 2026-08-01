"""Application settings, loaded from the environment (see .env.example)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_name: str = "CAFlow"
    environment: str = "development"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"

    # --- Database ---
    database_url: str = "postgresql+psycopg://caflow:caflow@localhost:5432/caflow"

    # --- Auth ---
    secret_key: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 12
    magic_link_expire_minutes: int = 60 * 24 * 7

    # --- CORS ---
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- Client portal ---
    # Magic links are built as f"{portal_base_url}?token=...".
    portal_base_url: str = "http://localhost:5173/portal"

    # --- Document storage ---
    # Local filesystem for now; swap for S3-compatible object storage on deploy.
    storage_dir: str = "./var/documents"
    max_upload_bytes: int = 20 * 1024 * 1024
    # How much of a text document to hand the LLM for categorisation.
    document_excerpt_chars: int = 6000

    # --- Billing ---
    invoice_number_prefix: str = "INV"
    invoice_payment_terms_days: int = 15
    invoice_gst_rate_bps: int = 1800  # 18.00%
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
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True  # STARTTLS on the standard submission port
    smtp_use_ssl: bool = False  # implicit TLS, usually port 465
    smtp_timeout_seconds: float = 20.0
    email_from_address: str = "no-reply@caflow.local"
    email_from_name: str = "CAFlow"
    # A reminder is retried on the next dispatcher run until this many tries.
    reminder_max_attempts: int = 3

    # --- Queue ---
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # --- AI (OpenRouter free models) ---
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    openrouter_fallback_model: str = "google/gemma-3-27b-it:free"
    openrouter_timeout_seconds: float = 45.0

    # --- Compliance generation ---
    # How many financial years ahead of the client's onboarding to pre-generate.
    compliance_generation_months: int = 12

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def document_reminder_offsets(self) -> list[int]:
        return _int_list(self.document_reminder_offsets_days)

    @property
    def payment_reminder_offsets(self) -> list[int]:
        return _int_list(self.payment_reminder_offsets_days)


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
    return Settings()


settings = get_settings()
