"""Application configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Accounting"
    app_env: str = "development"
    # Self-contained SQLite database in ./data.
    database_url: str = "sqlite:///./data/accounting.sqlite3"
    # The single port this app runs on (API + web UI).
    port: int = 8100
    # Seed the default Chart of Accounts on first startup.
    seed_on_startup: bool = True
    # Default VAT rate for the UAE (5%).
    vat_standard_rate: float = 0.05

    # Authentication. Enabled by default; tests disable it. Change the secret + bootstrap
    # admin password before any real/shared deployment.
    auth_enabled: bool = True
    secret_key: str = "change-me-please-set-a-strong-secret"
    admin_username: str = "admin"
    admin_password: str = "admin123"   # bootstrap only — change on first login
    token_ttl_hours: int = 12
    # Password-reset (forgot-password) tokens.
    reset_token_ttl_minutes: int = 30          # configurable expiry window
    reset_request_window_minutes: int = 15      # rate-limit window per account
    reset_request_max: int = 3                  # max reset requests per account per window
    # When no SMTP is configured (default), the reset link/token is returned to the caller in
    # dev so the flow is testable end-to-end. Set true only for a trusted local console.
    reset_expose_token: bool = True

    # Transaction document attachments.
    attachments_dir: str = "./data/attachments"
    max_upload_mb: int = 25

    # Organization profile assets (logo).
    org_dir: str = "./data/org"


@lru_cache
def get_settings() -> Settings:
    return Settings()
