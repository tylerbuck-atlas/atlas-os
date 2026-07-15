# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Device Manager data models.

A **device** is the abstraction; an **adapter** owns the protocol. The
adapter that syncs a device is its *steward*: only it may update the
device, and for Class 3 devices (cameras, presence, microphones) only it
and the operator may read the state.

Commands are actions on the home, so they enter only through the
Planner (or the operator) — the Device Manager refuses everyone else.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

KIND_PATTERN = re.compile(r"^[a-z0-9_-]{1,50}$")
NATIVE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")

CLASS_INTIMATE = 3

KNOWN_KINDS = {
    "light", "switch", "sensor", "thermostat", "lock", "camera",
    "speaker", "cover", "fan", "appliance", "other",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeviceSync(BaseModel):
    """What an adapter reports about one of its devices."""

    name: str = Field(max_length=200, examples=["Kitchen ceiling light"])
    kind: str = Field(examples=["light"])
    room: str | None = Field(default=None, max_length=100)
    data_class: int = Field(default=1, ge=0, le=3, alias="class")
    #: Commands this device accepts (e.g. ["turn_on", "turn_off"]).
    commands: list[str] = Field(default_factory=list, max_length=50)
    state: dict = Field(default_factory=dict)
    online: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if not KIND_PATTERN.match(v):
            raise ValueError("invalid kind")
        if v not in KNOWN_KINDS:
            raise ValueError(f"kind must be one of {sorted(KNOWN_KINDS)}")
        return v


class DeviceRecord(BaseModel):
    id: str
    adapter: str
    native_id: str
    name: str
    kind: str
    room: str | None
    data_class: int = Field(serialization_alias="class")
    commands: list[str]
    state: dict
    online: bool
    metadata: dict[str, str]
    first_seen: datetime
    updated_at: datetime

    model_config = {"populate_by_name": True}


class CommandRequest(BaseModel):
    """Body of /v1/invoke/devices.command — the Planner's door."""

    device_id: str
    command: str = Field(max_length=100)
    params: dict = Field(default_factory=dict)


class CommandResult(BaseModel):
    device_id: str
    command: str
    adapter: str
    result: dict
    state: dict
