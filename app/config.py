"""
Central configuration via pydantic-settings.

All settings are read from environment variables (or a .env file).
Import `settings` for app-wide access; never hardcode secrets.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Google Cloud / Vertex AI ──────────────────────────────────────────
    google_cloud_project: str = Field(..., description="GCP project ID")
    google_cloud_location: str = Field("us-central1", description="Vertex AI region")
    google_application_credentials: str | None = Field(
        None, description="Path to service account JSON; omit on Cloud Run"
    )

    # ── Model ─────────────────────────────────────────────────────────────
    gemini_model: str = Field(
        "gemini-2.0-flash",
        description="Gemini model ID used by all agents (override to swap model)",
    )

    # ── Orchestration ─────────────────────────────────────────────────────
    max_critique_retries: Annotated[int, Field(ge=0, le=5)] = Field(
        2,
        description="Max critique-triggered re-run cycles before accepting / abstaining",
    )
    max_specialist_tool_calls: Annotated[int, Field(ge=1, le=50)] = Field(
        7,
        description=(
            "Max tool calls per specialist per critic cycle. "
            "Resets automatically when a specialist is re-run after critique feedback."
        ),
    )

    # ── Observability ─────────────────────────────────────────────────────
    log_level: str = Field("INFO")
    enable_cloud_trace: bool = Field(
        False, description="Export OTel spans to Google Cloud Trace"
    )

    # ── Server ────────────────────────────────────────────────────────────
    host: str = Field("0.0.0.0")
    port: int = Field(8080)
    cors_origins: list[str] = Field(
        default=["http://localhost:8080", "http://127.0.0.1:8080"]
    )

    # ── Web crawling ──────────────────────────────────────────────────────
    crawl_rate_limit_seconds: float = Field(
        1.0, description="Seconds between outbound requests to news sites"
    )
    crawl_max_articles: int = Field(
        5, description="Max articles fetched per crawl_news() call"
    )

    # ── SEC EDGAR ─────────────────────────────────────────────────────────
    sec_user_agent: str = Field(
        "AgenticFinancialAnalysis admin@example.com",
        description="Required User-Agent for SEC EDGAR API (see SEC policy)",
    )

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return upper

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v  # type: ignore[return-value]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton Settings instance."""
    return Settings()  # type: ignore[call-arg]


# Module-level convenience alias
settings: Settings = get_settings()
