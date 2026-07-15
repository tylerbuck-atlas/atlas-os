# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Health monitoring for registered services.

Two independent mechanisms, run by one monitor:

- **Heartbeat watchdog (push).** Services heartbeat on the negotiated
  interval. Missing `heartbeat_misses_allowed` consecutive intervals
  marks the service UNREACHABLE.
- **Active probes (pull).** Core polls each service's declared health
  URL. A failing probe marks the service UNHEALTHY; a succeeding probe
  on a non-unreachable service marks it HEALTHY.

An UNREACHABLE service recovers by heartbeating again; an UNHEALTHY
service recovers when its probe passes.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .config import CoreConfig
from .models import ServiceStatus, utcnow
from .registry import ServiceRegistry

log = logging.getLogger("atlas.core.health")


class HealthMonitor:
    """Background watchdog + prober over the service registry."""

    def __init__(
        self,
        registry: ServiceRegistry,
        config: CoreConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._registry = registry
        self._config = config
        self._tasks: list[asyncio.Task] = []
        self._client: httpx.AsyncClient | None = client

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._config.probe_timeout_seconds)
        self._tasks = [
            asyncio.create_task(self._watchdog_loop(), name="atlas-health-watchdog"),
            asyncio.create_task(self._probe_loop(), name="atlas-health-prober"),
        ]
        log.info(
            "health monitor started (heartbeat window %ss x%s, probe every %ss)",
            self._config.heartbeat_interval_seconds,
            self._config.heartbeat_misses_allowed,
            self._config.probe_interval_seconds,
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- heartbeat watchdog -------------------------------------------------

    async def check_heartbeats_once(self) -> None:
        """One watchdog pass. Public for tests."""
        window = (
            self._config.heartbeat_interval_seconds * self._config.heartbeat_misses_allowed
        )
        now = utcnow()
        for record in await self._registry.find():
            if record.last_heartbeat_at is None:
                continue
            overdue = (now - record.last_heartbeat_at).total_seconds()
            if overdue > window and record.status != ServiceStatus.UNREACHABLE:
                await self._registry.mark_status(
                    record.instance_id,
                    ServiceStatus.UNREACHABLE,
                    reason=f"no heartbeat for {overdue:.0f}s (window {window}s)",
                )

    async def _watchdog_loop(self) -> None:
        interval = self._config.heartbeat_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.check_heartbeats_once()
            except Exception:
                log.exception("heartbeat watchdog pass failed")

    # -- active probes --------------------------------------------------------

    async def probe_all_once(self) -> None:
        """One probe pass over all live services. Public for tests."""
        records = await self._registry.find()
        if records:
            await asyncio.gather(*(self._probe(r) for r in records))

    async def _probe(self, record) -> None:
        assert self._client is not None
        try:
            response = await self._client.get(record.health_url)
            ok = response.status_code == 200
        except httpx.HTTPError:
            ok = False

        if ok:
            if record.status in (ServiceStatus.STARTING, ServiceStatus.UNHEALTHY):
                await self._registry.mark_status(
                    record.instance_id, ServiceStatus.HEALTHY, reason="probe ok"
                )
        else:
            # An unreachable service stays unreachable until it heartbeats;
            # a probe failure on anything else marks it unhealthy.
            if record.status != ServiceStatus.UNREACHABLE:
                await self._registry.mark_status(
                    record.instance_id, ServiceStatus.UNHEALTHY, reason="probe failed"
                )

    async def _probe_loop(self) -> None:
        interval = self._config.probe_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self.probe_all_once()
            except Exception:
                log.exception("health probe pass failed")
