"""Outbox publisher: streams Core's event log onto the Atlas Event Bus.

Core writes every event to its durable local log first (the outbox), then
this publisher forwards them, in order, to the Event Bus once one is
registered. The cursor (last published event id) is persisted, so
delivery to the Bus is at-least-once across Core restarts — consumers
must treat redelivered events as possible duplicates (docs/eventbus.md).

Discovery is Core-native: the publisher looks up ``atlas.eventbus`` in
Core's own registry. No configured bus address, no hidden dependency —
if no bus is registered, events simply wait in the outbox.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .config import CoreConfig
from .registry import ServiceRegistry
from .store import RegistryStore

log = logging.getLogger("atlas.core.publisher")

CURSOR_KEY = "eventbus.publisher.cursor"
EVENTBUS_SERVICE_NAME = "atlas.eventbus"


class EventPublisher:
    """Forwards the local event log to the Event Bus, in order."""

    def __init__(
        self,
        store: RegistryStore,
        registry: ServiceRegistry,
        config: CoreConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._config = config
        self._client = client  # injectable for tests
        self._owns_client = client is None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0)
        self._task = asyncio.create_task(self._loop(), name="atlas-event-publisher")
        log.info(
            "event publisher started (interval %ss, batch %s)",
            self._config.publish_interval_seconds,
            self._config.publish_batch_size,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    async def publish_once(self) -> int:
        """One publishing pass. Returns how many events were forwarded.

        Public for tests.
        """
        bus_address = await self._find_bus()
        if bus_address is None:
            return 0

        cursor = int(await self._store.get_meta(CURSOR_KEY) or 0)
        events = await self._store.list_events_after(
            cursor, limit=self._config.publish_batch_size
        )
        published = 0
        for event in events:
            assert self._client is not None and event.id is not None
            try:
                response = await self._client.post(
                    f"{bus_address}/v1/events",
                    headers={
                        "Authorization": f"Bearer {self._config.bootstrap_token}"
                    },
                    json={
                        "topic": event.topic,
                        "payload": event.payload,
                        "occurred_at": event.occurred_at.isoformat(),
                    },
                )
            except httpx.HTTPError as exc:
                log.warning("bus unreachable mid-batch (%s); will retry", exc)
                break
            if response.status_code != 201:
                log.warning(
                    "bus rejected event %s (%s): %s — will retry",
                    event.id, response.status_code, response.text[:200],
                )
                break
            cursor = event.id
            await self._store.set_meta(CURSOR_KEY, str(cursor))
            published += 1
        if published:
            log.info("published %d event(s) to the bus (cursor=%d)", published, cursor)
        return published

    async def _find_bus(self) -> str | None:
        records = await self._registry.find(name=EVENTBUS_SERVICE_NAME)
        for record in records:
            if record.status.value in ("starting", "healthy"):
                return record.address.rstrip("/")
        return None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.publish_interval_seconds)
            try:
                # Drain fully if there is a backlog.
                while await self.publish_once() >= self._config.publish_batch_size:
                    pass
            except Exception:
                log.exception("event publishing pass failed")
