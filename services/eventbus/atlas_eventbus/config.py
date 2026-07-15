"""Atlas Event Bus configuration (12-factor, validated at boot)."""

from __future__ import annotations

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BusConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_EVENTBUS_", env_file=".env", extra="ignore")

    # Network
    host: str = "0.0.0.0"
    port: int = Field(default=8200, ge=1, le=65535)
    #: Address other services use to reach this bus (published to Core).
    self_url: str = "http://atlas-eventbus:8200"

    # Atlas Core
    core_url: str = "http://atlas-core:8000"
    #: Required to register with Core. Read from ATLAS_EVENTBUS_BOOTSTRAP_TOKEN
    #: or, if unset, from the shared ATLAS_BOOTSTRAP_TOKEN.
    bootstrap_token: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATLAS_EVENTBUS_BOOTSTRAP_TOKEN", "ATLAS_BOOTSTRAP_TOKEN", "bootstrap_token"
        ),
    )

    # Delivery semantics
    visibility_timeout_seconds: int = Field(default=30, ge=1)
    max_pull_batch: int = Field(default=100, ge=1)
    max_wait_seconds: int = Field(default=30, ge=0)

    # Auth
    introspect_cache_ttl_seconds: float = Field(default=30.0, ge=0)

    # Storage
    database_path: str = "data/atlas-eventbus.db"

    # Logging
    log_level: str = "INFO"

    @field_validator("bootstrap_token")
    @classmethod
    def _require_token(cls, v: str) -> str:
        if len(v) < 16:
            raise ValueError(
                "ATLAS_BOOTSTRAP_TOKEN must be set (>=16 chars) so the bus can "
                "register with Atlas Core. The bus does not boot anonymously."
            )
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log level must be one of {sorted(allowed)}")
        return upper
