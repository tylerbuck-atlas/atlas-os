# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Bus consumer: registry.* events → durable system.services facts.

This is where "event replay lands in Memory" (roadmap M4): the transient
event stream becomes queryable, versioned state with provenance.
"""

from __future__ import annotations

import asyncio
import logging
import ssl

import httpx

from atlas_sdk import AtlasService, EventBusClient, discover_service

from .service import MemoryService

log = logging.getLogger("atlas.memory.consumer")

SUBSCRIPTION_NAME = "memory-registry"
TOPICS = ["registry.*"]


class RegistryEventConsumer:
    def __init__(self, memory: MemoryService, atlas: AtlasService) -> None:
        self._memory = memory
        self._atlas = atlas
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="atlas-memory-consumer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _find_bus(self) -> str | None:
        token, ssl_ctx = self._atlas.bus_credentials()
        try:
            buses = await discover_service(
                core_url=self._atlas.core_url,
                token=token,
                ssl_context=ssl_ctx,
                name="atlas.eventbus",
            )
        except (httpx.HTTPError, ssl.SSLError):
            return None
        live = [b for b in buses if b.get("status") in ("starting", "healthy")]
        return live[0]["address"] if live else None

    async def _loop(self) -> None:
        subscription_id: str | None = None
        client: EventBusClient | None = None
        while True:
            try:
                if client is None:
                    bus_url = await self._find_bus()
                    if bus_url is None:
                        await asyncio.sleep(3)
                        continue
                    token, ssl_ctx = self._atlas.bus_credentials()
                    client = EventBusClient(bus_url, token, ssl_context=ssl_ctx)
                    subscription_id = await client.ensure_subscription(
                        SUBSCRIPTION_NAME, TOPICS
                    )
                    log.info("consuming %s from %s (sub %s)", TOPICS, bus_url, subscription_id)

                messages = await client.pull(
                    subscription_id, max_messages=50, wait_seconds=20
                )
                for message in messages:
                    await self._memory.materialize_registry_event(message["event"])
                if messages:
                    await client.ack(
                        subscription_id, [m["delivery_id"] for m in messages]
                    )
            except asyncio.CancelledError:
                if client:
                    await client.close()
                raise
            except (httpx.HTTPError, ssl.SSLError, KeyError) as exc:
                log.warning("consumer hiccup (%s); reconnecting", exc)
                if client:
                    await client.close()
                client, subscription_id = None, None
                await asyncio.sleep(3)
            except Exception:
                log.exception("consumer pass failed")
                await asyncio.sleep(3)
