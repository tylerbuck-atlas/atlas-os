# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Data models shared across Atlas Core.

These models define the v1 registry contract. Changing them in a breaking
way requires a new API version prefix.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

SERVICE_NAME_PATTERN = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)+$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+([-.+][0-9A-Za-z.-]+)?$")
CAPABILITY_PATTERN = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)*$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ServiceStatus(StrEnum):
    """Lifecycle states of a registered service."""

    STARTING = "starting"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNREACHABLE = "unreachable"
    DEREGISTERED = "deregistered"


class ServiceRegistration(BaseModel):
    """Payload a service submits to register with Atlas Core."""

    name: str = Field(max_length=64, examples=["atlas.echo"])
    version: str = Field(max_length=64, examples=["0.1.0"])
    address: str = Field(max_length=512, examples=["http://atlas-echo:8100"])
    health_url: str = Field(max_length=512, examples=["http://atlas-echo:8100/healthz"])
    capabilities: list[str] = Field(default_factory=list, max_length=128)
    metadata: dict[str, str] = Field(default_factory=dict)
    #: PEM CSR. Required in mtls mode; the response carries the issued
    #: certificate. Ignored in token mode.
    csr: str | None = Field(default=None, max_length=16384)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not SERVICE_NAME_PATTERN.match(v):
            raise ValueError(
                "service name must be lowercase dot-separated, e.g. 'atlas.echo' "
                "or 'vendor.thing'"
            )
        return v

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        if not VERSION_PATTERN.match(v):
            raise ValueError("version must be semantic, e.g. '0.1.0'")
        return v

    @field_validator("address", "health_url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("must be an http(s) URL")
        return v

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, v: list[str]) -> list[str]:
        for cap in v:
            if not CAPABILITY_PATTERN.match(cap):
                raise ValueError(f"invalid capability {cap!r}")
        return v


class ServiceRecord(BaseModel):
    """A registered service instance as known to Atlas Core."""

    instance_id: str
    name: str
    version: str
    address: str
    health_url: str
    capabilities: list[str]
    metadata: dict[str, str]
    status: ServiceStatus
    registered_at: datetime
    status_changed_at: datetime
    last_heartbeat_at: datetime | None = None


class RegistrationResponse(BaseModel):
    """Returned exactly once, at registration.

    token mode: `service_token` is set (never shown again).
    mtls mode: `certificate` + `ca_certificate` are set; tokens retired.
    """

    service: ServiceRecord
    heartbeat_interval_seconds: int
    service_token: str | None = None
    certificate: str | None = None
    ca_certificate: str | None = None


class Event(BaseModel):
    """An internal system event.

    Shaped to be publishable on the Atlas Event Bus (Milestone 2) without
    change: stable topic naming, versioned payloads.
    """

    id: int | None = None
    topic: str = Field(examples=["registry.service.registered"])
    occurred_at: datetime = Field(default_factory=utcnow)
    payload: dict = Field(default_factory=dict)
