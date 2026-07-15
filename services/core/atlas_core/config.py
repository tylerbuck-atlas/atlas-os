# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Atlas Core configuration.

Twelve-factor: all configuration comes from the environment (or an env
file for local development). Configuration is validated at boot; invalid
configuration refuses to boot.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_FORBIDDEN_TOKENS = {
    "change-me-to-a-long-random-secret",
    "changeme",
    "secret",
    "password",
}


class CoreConfig(BaseSettings):
    """Validated configuration for Atlas Core."""

    model_config = SettingsConfigDict(env_prefix="ATLAS_", env_file=".env", extra="ignore")

    # Network
    core_host: str = "0.0.0.0"
    core_port: int = Field(default=8000, ge=1, le=65535)

    # Security
    bootstrap_token: str = ""

    # Health monitoring
    heartbeat_interval_seconds: int = Field(default=10, ge=1)
    heartbeat_misses_allowed: int = Field(default=3, ge=1)
    probe_interval_seconds: int = Field(default=15, ge=1)
    probe_timeout_seconds: float = Field(default=5.0, gt=0)

    # Event publishing (outbox → Event Bus)
    publish_interval_seconds: float = Field(default=1.0, gt=0)
    publish_batch_size: int = Field(default=100, ge=1)

    # Storage
    database_path: str = "data/atlas-core.db"

    # Logging
    log_level: str = "INFO"

    @field_validator("bootstrap_token")
    @classmethod
    def _validate_bootstrap_token(cls, v: str) -> str:
        if len(v) < 16:
            raise ValueError(
                "ATLAS_BOOTSTRAP_TOKEN must be set to a secret of at least 16 characters. "
                "Atlas does not boot with weak or missing credentials."
            )
        if v.lower() in _FORBIDDEN_TOKENS:
            raise ValueError(
                "ATLAS_BOOTSTRAP_TOKEN is set to a known placeholder value. "
                "Set a real secret."
            )
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"ATLAS_LOG_LEVEL must be one of {sorted(allowed)}")
        return upper
