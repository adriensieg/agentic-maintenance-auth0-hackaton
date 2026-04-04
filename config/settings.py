"""
config/settings.py
──────────────────
Single source of truth for all environment-driven configuration.
Uses pydantic-settings so every field is type-checked and documented.
"""
from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Optional

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Auth0 ────────────────────────────────────────────────
    auth0_domain: str = Field(..., description="e.g. your-tenant.auth0.com")
    auth0_audience: str = Field(..., description="API identifier in Auth0")
    auth0_client_id: str = Field(...)
    auth0_client_secret: str = Field(...)

    # Auth0 Management API (separate M2M application)
    auth0_mgmt_client_id: str = Field(...)
    auth0_mgmt_client_secret: str = Field(...)

    # Auth0 FGA (Fine-Grained Authorization / ReBAC)
    auth0_fga_store_id: str = Field(default="")
    auth0_fga_client_id: str = Field(default="")
    auth0_fga_client_secret: str = Field(default="")

    # ── Jira / Atlassian ─────────────────────────────────────
    jira_client_id: str = Field(...)
    jira_client_secret: str = Field(...)
    jira_cloud_id: str = Field(...)
    jira_project_key: str = Field(default="WASH")
    jira_auth0_user_id: str = Field(default="")

    # ── Twilio ───────────────────────────────────────────────
    twilio_account_sid: str = Field(...)
    twilio_auth_token: str = Field(...)
    twilio_phone_number: str = Field(...)
    twilio_verify_service_sid: str = Field(default="")

    # ── Stripe ───────────────────────────────────────────────
    stripe_secret_key: str = Field(...)
    stripe_webhook_secret: str = Field(default="")

    # ── Google ───────────────────────────────────────────────
    google_client_id: str = Field(default="")
    google_client_secret: str = Field(default="")

    # ── AI ───────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="")
    gemini_api_key: str = Field(default="")

    # ── App ──────────────────────────────────────────────────
    app_base_url: str = Field(default="https://washfix.devailab.work")
    mcp_server_url: str = Field(default="https://techie.devailab.work")
    secret_key: str = Field(default_factory=lambda: secrets.token_hex(32))
    encryption_key: str = Field(default="")  # 32-byte base64 AES key

    log_level: str = Field(default="INFO")
    redis_url: str = Field(default="redis://localhost:6379/0")
    database_url: str = Field(default="sqlite+aiosqlite:///washfix.db")

    # ── Computed properties ──────────────────────────────────
    @computed_field
    @property
    def auth0_issuer(self) -> str:
        return f"https://{self.auth0_domain}/"

    @computed_field
    @property
    def auth0_jwks_url(self) -> str:
        return f"https://{self.auth0_domain}/.well-known/jwks.json"

    @computed_field
    @property
    def auth0_token_url(self) -> str:
        return f"https://{self.auth0_domain}/oauth/token"

    @computed_field
    @property
    def auth0_authorize_url(self) -> str:
        return f"https://{self.auth0_domain}/authorize"

    @computed_field
    @property
    def auth0_backchannel_url(self) -> str:
        return f"https://{self.auth0_domain}/bc-authorize"

    @computed_field
    @property
    def jira_api_base(self) -> str:
        return f"https://api.atlassian.com/ex/jira/{self.jira_cloud_id}/rest/api/3"

    @computed_field
    @property
    def jira_browse_base(self) -> str:
        return "https://siegadrien.atlassian.net/browse"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
