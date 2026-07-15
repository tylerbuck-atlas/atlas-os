# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Sentinel's watch loop: consume the bus, evaluate rules, raise alerts."""

from __future__ import annotations

import asyncio
import logging
import ssl

import httpx

from atlas_sdk import AtlasService, EventBusClient, discover_service

from .rules import RuleEngine
from .store import SentinelStore

log = logging.getLogger("atlas.sentinel")

SUBSCRIPTION_NAME = "sentinel-watch"
TOPICS = ["registry.*", "planner.*"]


class Watcher:
    def __init__(self, store: SentinelStore, rules: RuleEngine, atlas: AtlasService) -> None:
        self._store = store
        self._rules = rules
        self._atlas = atlas
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="atlas-sentinel-watch")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def handle_event(self, event: dict) -> int:
        """Evaluate one event; persist + publish any alerts. Returns count."""
        raised = 0
        for candidate in self._rules.evaluate(event):
            alert = await self._store.raise_alert(
                kind=candidate.kind, subject=candidate.subject,
                severity=candidate.severity, detail=candidate.detail,
            )
            await self._store.append_event(
                "sentinel.alert.raised",
                {
                    "alert_id": alert.id, "kind": alert.kind, "subject": alert.subject,
                    "severity": alert.severity, "detail": alert.detail,
                },
            )
            log.warning("ALERT [%s] %s: %s", alert.severity, alert.kind, alert.detail)
            raised += 1
        return raised

    async def _find_bus(self) -> str | None:
        token, ssl_ctx = self._atlas.bus_credentials()
        try:
            buses = await discover_service(
                core_url=self._atlas.core_url, token=token, ssl_context=ssl_ctx,
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
                    log.info("watching %s (sub %s)", TOPICS, subscription_id)

                messages = await client.pull(
                    subscription_id, max_messages=50, wait_seconds=20
                )
                for message in messages:
                    await self.handle_event(message["event"])
                if messages:
                    await client.ack(
                        subscription_id, [m["delivery_id"] for m in messages]
                    )
            except asyncio.CancelledError:
                if client:
                    await client.close()
                raise
            except (httpx.HTTPError, ssl.SSLError, KeyError) as exc:
                log.warning("watcher hiccup (%s); reconnecting", exc)
                if client:
                    await client.close()
                client, subscription_id = None, None
                await asyncio.sleep(3)
            except Exception:
                log.exception("watcher pass failed")
                await asyncio.sleep(3)
