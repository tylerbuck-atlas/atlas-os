"""Event Bus data models — the v1 bus contract."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

TOPIC_PATTERN = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)*$")
#: Subscription patterns additionally allow a trailing ".*" wildcard or bare "*".
SUBSCRIPTION_PATTERN = re.compile(r"^(\*|[a-z0-9_-]+(\.[a-z0-9_-]+)*(\.\*)?)$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def topic_matches(pattern: str, topic: str) -> bool:
    """True if a subscription pattern matches a concrete topic.

    Rules: exact match; ``registry.*`` matches any topic strictly under
    ``registry.``; bare ``*`` matches everything.
    """
    if pattern == "*" or pattern == topic:
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-1])  # keeps the trailing dot
    return False


class PublishRequest(BaseModel):
    """What a producer submits. `source` is set server-side from the
    authenticated identity — clients cannot claim to be someone else."""

    topic: str = Field(max_length=200, examples=["registry.service.registered"])
    payload: dict = Field(default_factory=dict)
    occurred_at: datetime | None = None

    @field_validator("topic")
    @classmethod
    def _validate_topic(cls, v: str) -> str:
        if not TOPIC_PATTERN.match(v):
            raise ValueError("topic must be lowercase dot-separated, e.g. 'registry.service.registered'")
        return v


class EventEnvelope(BaseModel):
    """A stored, published event."""

    event_id: str
    topic: str
    source: str
    occurred_at: datetime
    published_at: datetime
    schema_version: int | None = None
    payload: dict


class SubscriptionRequest(BaseModel):
    name: str = Field(max_length=64, examples=["main"], pattern=r"^[a-z0-9_-]{1,64}$")
    topics: list[str] = Field(min_length=1, max_length=64)

    @field_validator("topics")
    @classmethod
    def _validate_topics(cls, v: list[str]) -> list[str]:
        for pattern in v:
            if not SUBSCRIPTION_PATTERN.match(pattern):
                raise ValueError(
                    f"invalid topic pattern {pattern!r} (exact topic, 'prefix.*', or '*')"
                )
        return v


class Subscription(BaseModel):
    id: str
    service_name: str
    name: str
    topics: list[str]
    created_at: datetime


class PullRequest(BaseModel):
    max_messages: int = Field(default=10, ge=1)
    wait_seconds: int = Field(default=0, ge=0)


class Delivery(BaseModel):
    delivery_id: int
    attempt: int
    event: EventEnvelope


class PullResponse(BaseModel):
    messages: list[Delivery]


class AckRequest(BaseModel):
    delivery_ids: list[int] = Field(min_length=1, max_length=1000)


class SchemaRegistration(BaseModel):
    json_schema: dict = Field(alias="json_schema")


class TopicSchema(BaseModel):
    topic: str
    version: int
    json_schema: dict
    created_at: datetime
