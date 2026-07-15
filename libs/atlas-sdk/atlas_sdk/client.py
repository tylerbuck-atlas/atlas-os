"""Atlas SDK clients.

- :class:`AtlasService` — registration, heartbeats, re-registration, and
  clean deregistration against Atlas Core.
- :class:`EventBusClient` — publish, subscribe, pull, and ack against the
  Atlas Event Bus.
- :func:`discover_service` — find services through Core's registry.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("atlas.sdk")


class AtlasService:
    """Keeps one service instance registered and heartbeating with Core.

    Usage::

        atlas = AtlasService(
            name="atlas.echo", version="0.2.0",
            address="http://atlas-echo:8100",
            health_url="http://atlas-echo:8100/healthz",
            capabilities=["echo.reply"],
            core_url="http://atlas-core:8000",
            bootstrap_token=...,
        )
        await atlas.start()   # registers (retrying) + starts heartbeats
        ...
        await atlas.stop()    # deregisters cleanly
    """

    def __init__(
        self,
        *,
        name: str,
        version: str,
        address: str,
        health_url: str,
        capabilities: list[str],
        core_url: str,
        bootstrap_token: str,
        metadata: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.name = name
        self.version = version
        self.address = address
        self.health_url = health_url
        self.capabilities = capabilities
        self.metadata = metadata or {}
        self.core_url = core_url.rstrip("/")
        self._bootstrap_token = bootstrap_token
        self.instance_id: str | None = None
        self.service_token: str | None = None
        self._interval: int = 10
        self._task: asyncio.Task | None = None
        self._client = httpx.AsyncClient(timeout=timeout)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        await self._register_with_retry()
        self._task = asyncio.create_task(
            self._heartbeat_loop(), name=f"{self.name}-heartbeat"
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self.instance_id and self.service_token:
            try:
                await self._client.delete(
                    f"{self.core_url}/v1/registry/services/{self.instance_id}",
                    headers=self._auth(self.service_token),
                )
                log.info("%s deregistered from Atlas Core", self.name)
            except httpx.HTTPError:
                log.warning("%s could not deregister cleanly", self.name)
        await self._client.aclose()

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def _register(self) -> None:
        response = await self._client.post(
            f"{self.core_url}/v1/registry/services",
            headers=self._auth(self._bootstrap_token),
            json={
                "name": self.name,
                "version": self.version,
                "address": self.address,
                "health_url": self.health_url,
                "capabilities": self.capabilities,
                "metadata": self.metadata,
            },
        )
        response.raise_for_status()
        body = response.json()
        self.instance_id = body["service"]["instance_id"]
        self.service_token = body["service_token"]
        self._interval = body["heartbeat_interval_seconds"]
        log.info(
            "%s registered with Atlas Core (instance %s, heartbeat %ss)",
            self.name, self.instance_id, self._interval,
        )

    async def _register_with_retry(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._register()
                return
            except (httpx.HTTPError, KeyError) as exc:
                log.info("Core not ready (%s); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                response = await self._client.post(
                    f"{self.core_url}/v1/registry/services/{self.instance_id}/heartbeat",
                    headers=self._auth(self.service_token or ""),
                )
                if response.status_code in (401, 410):
                    log.warning("%s token invalidated; re-registering", self.name)
                    await self._register_with_retry()
            except httpx.HTTPError as exc:
                log.warning("%s heartbeat failed: %s", self.name, exc)


async def discover_service(
    *,
    core_url: str,
    token: str,
    name: str | None = None,
    capability: str | None = None,
    timeout: float = 5.0,
) -> list[dict]:
    """Query Core's registry. Returns service records (possibly empty)."""
    params: dict[str, str] = {}
    if name:
        params["name"] = name
    if capability:
        params["capability"] = capability
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            f"{core_url.rstrip('/')}/v1/registry/services",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json()


class EventBusClient:
    """Client for the Atlas Event Bus.

    Authenticate with the service token issued by Core at registration
    (the Bus verifies it via Core's introspection API).
    """

    def __init__(self, bus_url: str, token: str, *, timeout: float = 35.0) -> None:
        self.bus_url = bus_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout, headers={"Authorization": f"Bearer {token}"}
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def publish(
        self, topic: str, payload: dict, *, occurred_at: str | None = None
    ) -> dict:
        """Publish one event. Returns the stored envelope."""
        body: dict = {"topic": topic, "payload": payload}
        if occurred_at:
            body["occurred_at"] = occurred_at
        response = await self._client.post(f"{self.bus_url}/v1/events", json=body)
        response.raise_for_status()
        return response.json()

    async def ensure_subscription(self, name: str, topics: list[str]) -> str:
        """Create (or fetch, if it already exists) a named subscription.

        Returns the subscription id. Idempotent per (service, name).
        """
        response = await self._client.post(
            f"{self.bus_url}/v1/subscriptions",
            json={"name": name, "topics": topics},
        )
        response.raise_for_status()
        return response.json()["id"]

    async def pull(
        self, subscription_id: str, *, max_messages: int = 10, wait_seconds: int = 0
    ) -> list[dict]:
        """Pull up to `max_messages` deliveries. Long-polls up to `wait_seconds`."""
        response = await self._client.post(
            f"{self.bus_url}/v1/subscriptions/{subscription_id}/pull",
            json={"max_messages": max_messages, "wait_seconds": wait_seconds},
        )
        response.raise_for_status()
        return response.json()["messages"]

    async def ack(self, subscription_id: str, delivery_ids: list[int]) -> None:
        """Acknowledge processed deliveries so they are not redelivered."""
        response = await self._client.post(
            f"{self.bus_url}/v1/subscriptions/{subscription_id}/ack",
            json={"delivery_ids": delivery_ids},
        )
        response.raise_for_status()
