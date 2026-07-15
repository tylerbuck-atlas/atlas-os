# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Durable outbox → Event Bus publisher, shared by Atlas services.

The pattern (established by Core in Milestone 2): a service writes events
to its own durable store first, and this loop forwards them to the Event
Bus in order, tracking a persisted cursor — at-least-once delivery across
restarts, and no lost events when the bus is down or not yet registered.

The bus is discovered through Core's registry each pass; if none is
registered, events simply wait.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Awaitable, Callable

import httpx

from .client import discover_service

log = logging.getLogger("atlas.sdk.outbox")

EVENTBUS_SERVICE_NAME = "atlas.eventbus"


class BusOutbox:
    """Forwards a service's local event log to the Event Bus, in order.

    Storage-agnostic: the owning service supplies async callables for its
    own store. Events are 4-tuples: (id, topic, payload_dict, occurred_at_iso).
    """

    def __init__(
        self,
        *,
        core_url: str,
        credentials: Callable[[], tuple[str | None, ssl.SSLContext | None]],
        list_events_after: Callable[[int, int], Awaitable[list[tuple]]],
        get_cursor: Callable[[], Awaitable[int]],
        set_cursor: Callable[[int], Awaitable[None]],
        interval_seconds: float = 1.0,
        batch_size: int = 100,
    ) -> None:
        self._core_url = core_url.rstrip("/")
        self._credentials = credentials
        self._list_events_after = list_events_after
        self._get_cursor = get_cursor
        self._set_cursor = set_cursor
        self._interval = interval_seconds
        self._batch = batch_size
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="atlas-outbox")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def publish_once(self) -> int:
        """One pass; returns events forwarded. Public for tests."""
        token, ssl_ctx = self._credentials()
        try:
            buses = await discover_service(
                core_url=self._core_url,
                token=token,
                ssl_context=ssl_ctx,
                name=EVENTBUS_SERVICE_NAME,
            )
        except (httpx.HTTPError, ssl.SSLError) as exc:
            log.debug("bus discovery failed (%s); retrying later", exc)
            return 0
        live = [b for b in buses if b.get("status") in ("starting", "healthy")]
        if not live:
            return 0
        bus_url = live[0]["address"].rstrip("/")

        cursor = await self._get_cursor()
        events = await self._list_events_after(cursor, self._batch)
        if not events:
            return 0

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        published = 0
        async with httpx.AsyncClient(
            timeout=5.0, verify=ssl_ctx if ssl_ctx is not None else True, headers=headers
        ) as client:
            for event_id, topic, payload, occurred_at in events:
                try:
                    response = await client.post(
                        f"{bus_url}/v1/events",
                        json={"topic": topic, "payload": payload, "occurred_at": occurred_at},
                    )
                except (httpx.HTTPError, ssl.SSLError) as exc:
                    log.warning("bus unreachable mid-batch (%s); will retry", exc)
                    break
                if response.status_code != 201:
                    log.warning(
                        "bus rejected event %s (%s): %s — will retry",
                        event_id, response.status_code, response.text[:200],
                    )
                    break
                cursor = event_id
                await self._set_cursor(cursor)
                published += 1
        if published:
            log.info("outbox: published %d event(s) (cursor=%d)", published, cursor)
        return published

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                while await self.publish_once() >= self._batch:
                    pass
            except Exception:
                log.exception("outbox pass failed")
