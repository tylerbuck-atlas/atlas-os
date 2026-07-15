# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Planner data models: policies, plans, steps.

The founding rules made real:

- **Nothing acts on the home except through a plan.** A plan is a list
  of capability invocations, validated against policy before anything
  runs, executed step by step, every step recorded forever.
- **Default deny.** No matching policy means no. An empty policy table
  means Atlas executes nothing at all until the operator says otherwise.
- **LLM output is data.** When the AI service arrives (M7), its
  proposals enter here as plan requests like anyone else's — the model
  gets no other door.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

CAPABILITY_PATTERN = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)*$")
POLICY_PATTERN = re.compile(r"^(\*|[a-z0-9_-]+(\.[a-z0-9_-]+)*(\.\*)?)$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -- policies -----------------------------------------------------------------

class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyWrite(BaseModel):
    """A policy rule. First match (by priority, then id) wins."""

    priority: int = Field(default=100, ge=0, le=10000)
    requester: str = Field(examples=["atlas.ai", "*"])
    capability: str = Field(examples=["echo.reply", "devices.*", "*"])
    effect: PolicyEffect
    note: str = Field(default="", max_length=500)

    @field_validator("requester", "capability")
    @classmethod
    def _pattern(cls, v: str) -> str:
        if not POLICY_PATTERN.match(v):
            raise ValueError("must be a name, 'prefix.*', or '*'")
        return v


class PolicyRecord(PolicyWrite):
    id: int
    created_by: str
    created_at: datetime


def pattern_matches(pattern: str, value: str) -> bool:
    if pattern == "*" or pattern == value:
        return True
    if pattern.endswith(".*"):
        return value.startswith(pattern[:-1])
    return False


# -- plans ----------------------------------------------------------------------

class PlanStatus(StrEnum):
    REJECTED = "rejected"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ActionRequest(BaseModel):
    capability: str = Field(examples=["echo.reply"])
    params: dict = Field(default_factory=dict)
    #: Pin a specific service; otherwise resolved by capability discovery.
    target_service: str | None = None

    @field_validator("capability")
    @classmethod
    def _capability(cls, v: str) -> str:
        if not CAPABILITY_PATTERN.match(v):
            raise ValueError("invalid capability")
        return v


class PlanRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=1000, examples=["Say hello via echo"])
    actions: list[ActionRequest] = Field(min_length=1, max_length=50)


class StepRecord(BaseModel):
    index: int
    capability: str
    params: dict
    target_service: str | None
    status: StepStatus
    resolved_service: str | None = None
    result: dict | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class PlanRecord(BaseModel):
    id: str
    goal: str
    requester: str
    status: PlanStatus
    reason: str | None = None
    approved_by: str | None = None
    created_at: datetime
    updated_at: datetime
    steps: list[StepRecord] = []
