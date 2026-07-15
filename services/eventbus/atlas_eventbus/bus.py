# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Bus logic: publish → validate → fan out; pull/ack; long-polling."""

from __future__ import annotations

import asyncio
import logging
import uuid

import jsonschema

from .config import BusConfig
from .models import (
    Delivery,
    EventEnvelope,
    PublishRequest,
    Subscription,
    SubscriptionRequest,
    topic_matches,
    utcnow,
)
from .store import BusStore

log = logging.getLogger("atlas.eventbus")


class SchemaValidationError(Exception):
    def __init__(self, topic: str, version: int, detail: str) -> None:
        self.topic, self.version, self.detail = topic, version, detail
        super().__init__(detail)


class EventBus:
    """Durable at-least-once pub/sub over BusStore."""

    def __init__(self, store: BusStore, config: BusConfig) -> None:
        self._store = store
        self._config = config
        #: Wakes long-polling pullers when something is published.
        self._activity = asyncio.Event()
        self._housekeeping_task: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._housekeeping_task = asyncio.create_task(
            self._housekeeping_loop(), name="atlas-eventbus-housekeeping"
        )

    async def stop(self) -> None:
        if self._housekeeping_task:
            self._housekeeping_task.cancel()
            try:
                await self._housekeeping_task
            except asyncio.CancelledError:
                pass
            self._housekeeping_task = None

    # -- publish ---------------------------------------------------------------

    async def publish(self, request: PublishRequest, *, source: str) -> EventEnvelope:
        """Validate against the topic's registered schema (if any), store,
        and fan out to every matching subscription."""
        schema = await self._store.latest_schema(request.topic)
        schema_version: int | None = None
        if schema is not None:
            try:
                jsonschema.validate(request.payload, schema.json_schema)
            except jsonschema.ValidationError as exc:
                raise SchemaValidationError(
                    request.topic, schema.version, exc.message
                ) from exc
            schema_version = schema.version

        envelope = EventEnvelope(
            event_id=uuid.uuid4().hex,
            topic=request.topic,
            source=source,
            occurred_at=request.occurred_at or utcnow(),
            published_at=utcnow(),
            schema_version=schema_version,
            payload=request.payload,
        )
        event_row = await self._store.insert_event(envelope)

        fanout = 0
        for subscription in await self._store.list_subscriptions():
            if any(topic_matches(p, envelope.topic) for p in subscription.topics):
                await self._store.enqueue(subscription.id, event_row)
                fanout += 1
        log.info(
            "event %s topic=%s source=%s fanout=%d",
            envelope.event_id, envelope.topic, source, fanout,
        )
        self._activity.set()
        self._activity = asyncio.Event()  # fresh event for the next publish
        return envelope

    # -- subscriptions -----------------------------------------------------------

    async def subscribe(
        self, request: SubscriptionRequest, *, service_name: str
    ) -> Subscription:
        subscription = Subscription(
            id=uuid.uuid4().hex,
            service_name=service_name,
            name=request.name,
            topics=request.topics,
            created_at=utcnow(),
        )
        result = await self._store.upsert_subscription(subscription)
        log.info(
            "subscription %s/%s -> %s (id %s)",
            service_name, request.name, request.topics, result.id,
        )
        return result

    # -- pull / ack -----------------------------------------------------------------

    async def pull(
        self, subscription_id: str, *, max_messages: int, wait_seconds: int
    ) -> list[Delivery]:
        """Claim ready deliveries; long-poll up to wait_seconds if empty."""
        max_messages = min(max_messages, self._config.max_pull_batch)
        wait_seconds = min(wait_seconds, self._config.max_wait_seconds)
        deadline = asyncio.get_event_loop().time() + wait_seconds
        while True:
            deliveries = await self._store.pull(
                subscription_id,
                max_messages=max_messages,
                visibility_timeout=self._config.visibility_timeout_seconds,
            )
            if deliveries:
                return deliveries
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return []
            # Wake on the next publish or every second (visibility expiries).
            activity = self._activity
            try:
                await asyncio.wait_for(activity.wait(), timeout=min(remaining, 1.0))
            except TimeoutError:
                pass

    async def ack(self, subscription_id: str, delivery_ids: list[int]) -> int:
        return await self._store.ack(subscription_id, delivery_ids)

    # -- housekeeping --------------------------------------------------------------

    async def _housekeeping_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                purged = await self._store.purge_acked()
                if purged:
                    log.info("housekeeping: purged %d acked deliveries", purged)
            except Exception:
                log.exception("housekeeping pass failed")
