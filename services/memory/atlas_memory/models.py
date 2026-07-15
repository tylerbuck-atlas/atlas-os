# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Atlas Memory data models — facts, versions, and the data-class policy.

Facts are **versioned, never silently overwritten**: every write appends
a new version, so the household's memory is auditable end to end. Every
fact carries:

- a **data class** (docs/privacy.md): 0 public, 1 household, 2 personal,
  3 intimate;
- **provenance** — how this fact is known (event, sensor, user, ingest…);
- **source** — the verified identity that wrote it (never client-claimed);
- an optional **owner** — the person a Class 2/3 fact belongs to
  (schema-ready now; person-level policy enforcement arrives with the
  Planner's grant system).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

from atlas_sdk.service_auth import Identity

NAMESPACE_PATTERN = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)*$")
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:@-]{1,200}$")

CLASS_PUBLIC = 0
CLASS_HOUSEHOLD = 1
CLASS_PERSONAL = 2
CLASS_INTIMATE = 3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FactWrite(BaseModel):
    """Payload to write a fact version. `source` is set server-side."""

    payload: dict = Field(default_factory=dict)
    data_class: int = Field(default=CLASS_HOUSEHOLD, ge=0, le=3, alias="class")
    provenance: str = Field(max_length=200, examples=["user:tyler", "event:registry.service.status_changed"])
    owner: str | None = Field(default=None, max_length=100)

    model_config = {"populate_by_name": True}

    @field_validator("provenance")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("provenance is required — Atlas does not store facts of unknown origin")
        return v


class FactRecord(BaseModel):
    namespace: str
    key: str
    version: int
    payload: dict
    data_class: int = Field(serialization_alias="class")
    provenance: str
    source: str
    owner: str | None
    created_at: datetime
    deleted: bool = False

    model_config = {"populate_by_name": True}


def can_read(identity: Identity, record: "FactRecord") -> bool:
    """The Milestone-4 access rule (docs/privacy.md, docs/memory-assets.md).

    - Class 0–2: any authenticated service in the mesh (the household
      boundary is the mTLS mesh itself).
    - Class 3 (intimate): only the *steward* — the service that wrote the
      fact — and the operator. No exceptions, no grants yet: grants
      arrive with the Planner, and until then intimate data stays with
      its steward.
    """
    if record.data_class < CLASS_INTIMATE:
        return True
    return identity.is_operator or identity.name == record.source
