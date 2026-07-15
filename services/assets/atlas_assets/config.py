# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Atlas Asset Manager configuration (12-factor, validated at boot)."""

from __future__ import annotations

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AssetsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_ASSETS_", env_file=".env", extra="ignore")

    # Network
    host: str = "0.0.0.0"
    port: int = Field(default=8400, ge=1, le=65535)
    self_url: str = "https://atlas-assets:8400"

    # Atlas Core
    core_url: str = "https://atlas-core:8000"
    bootstrap_token: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATLAS_ASSETS_BOOTSTRAP_TOKEN", "ATLAS_BOOTSTRAP_TOKEN", "bootstrap_token"
        ),
    )

    # Security
    security_mode: str = Field(
        default="mtls",
        validation_alias=AliasChoices(
            "ATLAS_ASSETS_SECURITY_MODE", "ATLAS_SECURITY_MODE", "security_mode"
        ),
    )
    tls_dir: str = "data/tls"
    ca_cert_file: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ATLAS_ASSETS_CA_CERT", "ATLAS_CA_CERT", "ca_cert_file"),
    )
    introspect_cache_ttl_seconds: float = Field(default=30.0, ge=0)

    # Storage
    database_path: str = "data/atlas-assets.db"
    blob_dir: str = "data/blobs"
    max_upload_bytes: int = Field(default=512 * 1024 * 1024, ge=1)

    # Logging
    log_level: str = "INFO"

    @field_validator("bootstrap_token")
    @classmethod
    def _require_token(cls, v: str) -> str:
        if len(v) < 16:
            raise ValueError("ATLAS_BOOTSTRAP_TOKEN must be set (>=16 chars)")
        return v

    @field_validator("security_mode")
    @classmethod
    def _validate_security_mode(cls, v: str) -> str:
        lower = v.lower()
        if lower not in ("mtls", "token"):
            raise ValueError("security mode must be 'mtls' or 'token'")
        return lower

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        upper = v.upper()
        if upper not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("invalid log level")
        return upper
