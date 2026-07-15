# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Atlas SDK clients.

- :class:`AtlasService` — enrollment (mtls) / registration (token),
  heartbeats, certificate rotation, clean deregistration.
- :class:`EventBusClient` — publish, subscribe, pull, ack.
- :func:`discover_service` — find services through Core's registry.

Security modes follow docs/security.md:

**mtls** — at startup the service generates a key, submits a CSR with the
bootstrap token, and receives a short-lived certificate binding
``atlas://service/{name}/{instance_id}``. All subsequent calls are mutual
TLS; no bearer tokens exist. The SDK re-enrolls automatically at 2/3 of
certificate lifetime (rotation = re-registration; Core supersedes the old
instance and its certificate is refused from that moment).

**token** — Milestone-2 bearer-token behavior for development.
"""

from __future__ import annotations

import asyncio
import logging
import ssl

import httpx

from .tls import (
    TLSRuntime,
    cert_seconds_remaining,
    create_csr_pem,
    generate_private_key_pem,
)

log = logging.getLogger("atlas.sdk")


class AtlasService:
    """Keeps one service instance enrolled, heartbeating, and rotated."""

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
        security_mode: str = "token",
        tls_dir: str | None = None,
        ca_cert_file: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.name = name
        self.version = version
        self.address = address
        self.health_url = health_url
        self.capabilities = capabilities
        self.metadata = metadata or {}
        self.core_url = core_url.rstrip("/")
        self.security_mode = security_mode
        self._bootstrap_token = bootstrap_token
        self._timeout = timeout

        self.instance_id: str | None = None
        self.service_token: str | None = None
        self._interval: int = 10

        # mtls state
        self.tls: TLSRuntime | None = (
            TLSRuntime.prepare(tls_dir) if security_mode == "mtls" and tls_dir else None
        )
        self._ca_cert_file = ca_cert_file
        self._key_pem: bytes | None = None
        self._cert_pem: bytes | None = None
        #: Optional hook: called after each enrollment so the server can
        #: reload its TLS context with the fresh certificate.
        self.on_credentials_rotated = None

        self._tasks: list[asyncio.Task] = []
        self._client: httpx.AsyncClient | None = None

    # -- public lifecycle ---------------------------------------------------

    async def enroll(self) -> None:
        """Register with Core (retrying until it is up).

        mtls: generates a key + CSR, receives and installs the
        certificate. Must complete before the service starts serving TLS.
        """
        delay = 1.0
        while True:
            try:
                await self._register_once()
                return
            except (httpx.HTTPError, KeyError, ssl.SSLError) as exc:
                log.info("Core not ready (%s); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)

    def start_background(self) -> None:
        """Start heartbeats (+ certificate rotation in mtls mode)."""
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name=f"{self.name}-heartbeat")
        ]
        if self.security_mode == "mtls":
            self._tasks.append(
                asyncio.create_task(self._rotation_loop(), name=f"{self.name}-cert-rotation")
            )

    async def start(self) -> None:
        """Convenience for token mode: enroll + background loops."""
        await self.enroll()
        self.start_background()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        if self.instance_id:
            try:
                await self._http().delete(
                    f"{self.core_url}/v1/registry/services/{self.instance_id}",
                    headers=self._auth_headers(),
                )
                log.info("%s deregistered from Atlas Core", self.name)
            except (httpx.HTTPError, ssl.SSLError):
                log.warning("%s could not deregister cleanly", self.name)
        if self._client:
            await self._client.aclose()
            self._client = None

    def client_ssl_context(self) -> ssl.SSLContext | None:
        """Outbound mTLS context (trust CA, present our cert), or None."""
        if self.security_mode != "mtls" or self.tls is None:
            return None
        return self.tls.client_ssl_context()

    def bus_credentials(self) -> tuple[str | None, ssl.SSLContext | None]:
        """(token, ssl_context) for talking to Core/the bus — exactly one is set."""
        if self.security_mode == "mtls":
            return None, self.client_ssl_context()
        return self.service_token, None

    # -- internals -----------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        if self.security_mode == "mtls":
            return {}  # identity is the client certificate
        return {"Authorization": f"Bearer {self.service_token or ''}"}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            verify = self.client_ssl_context()
            self._client = httpx.AsyncClient(
                timeout=self._timeout, verify=verify if verify is not None else True
            )
        return self._client

    async def _rebuild_http(self) -> None:
        if self._client:
            await self._client.aclose()
        self._client = None

    async def _ensure_ca_cert(self) -> bytes:
        """Trust anchor for talking to Core.

        Preferred: a CA cert distributed out-of-band (ca_cert_file).
        Fallback: fetch from Core unauthenticated (trust-on-first-use on
        the private network; see docs/security.md)."""
        if self._ca_cert_file:
            from pathlib import Path

            return Path(self._ca_cert_file).read_bytes()
        async with httpx.AsyncClient(timeout=self._timeout, verify=False) as bootstrap:
            response = await bootstrap.get(f"{self.core_url}/v1/ca/certificate")
            response.raise_for_status()
            log.warning(
                "CA certificate fetched via TOFU from %s — distribute "
                "ATLAS_CA_CERT out-of-band to harden bootstrap", self.core_url,
            )
            return response.content

    async def _register_once(self) -> None:
        payload = {
            "name": self.name,
            "version": self.version,
            "address": self.address,
            "health_url": self.health_url,
            "capabilities": self.capabilities,
            "metadata": self.metadata,
        }

        if self.security_mode == "mtls":
            assert self.tls is not None, "mtls mode requires tls_dir"
            ca_pem = await self._ensure_ca_cert()
            self.tls.ca_path.write_bytes(ca_pem)
            if self._key_pem is None:
                self._key_pem = generate_private_key_pem()
            payload["csr"] = create_csr_pem(self._key_pem, self.name).decode()

            verify_ctx = ssl.create_default_context(cadata=ca_pem.decode())
            async with httpx.AsyncClient(timeout=self._timeout, verify=verify_ctx) as c:
                response = await c.post(
                    f"{self.core_url}/v1/registry/services",
                    headers={"Authorization": f"Bearer {self._bootstrap_token}"},
                    json=payload,
                )
            response.raise_for_status()
            body = response.json()
            self.instance_id = body["service"]["instance_id"]
            self._interval = body["heartbeat_interval_seconds"]
            self._cert_pem = body["certificate"].encode()
            self.tls.write(
                key_pem=self._key_pem,
                cert_pem=self._cert_pem,
                ca_pem=body["ca_certificate"].encode(),
            )
            await self._rebuild_http()
            if self.on_credentials_rotated:
                try:
                    self.on_credentials_rotated()
                except Exception:
                    log.exception("credentials-rotated hook failed")
            log.info(
                "%s enrolled (instance %s, cert valid %.0fh, heartbeat %ss)",
                self.name, self.instance_id,
                cert_seconds_remaining(self._cert_pem) / 3600, self._interval,
            )
            return

        response = await self._http().post(
            f"{self.core_url}/v1/registry/services",
            headers={"Authorization": f"Bearer {self._bootstrap_token}"},
            json=payload,
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

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                response = await self._http().post(
                    f"{self.core_url}/v1/registry/services/{self.instance_id}/heartbeat",
                    headers=self._auth_headers(),
                )
                if response.status_code in (401, 403, 410):
                    log.warning("%s credentials invalidated; re-enrolling", self.name)
                    await self.enroll()
            except (httpx.HTTPError, ssl.SSLError) as exc:
                log.warning("%s heartbeat failed: %s", self.name, exc)

    async def _rotation_loop(self) -> None:
        """Re-enroll at 2/3 of certificate lifetime."""
        while True:
            if self._cert_pem is None:
                await asyncio.sleep(5)
                continue
            remaining = cert_seconds_remaining(self._cert_pem)
            await asyncio.sleep(max(remaining / 3, 10))
            if self._cert_pem and cert_seconds_remaining(self._cert_pem) < remaining * 0.67:
                log.info("%s rotating certificate", self.name)
                await self.enroll()


async def discover_service(
    *,
    core_url: str,
    token: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
    name: str | None = None,
    capability: str | None = None,
    timeout: float = 5.0,
) -> list[dict]:
    """Query Core's registry. Authenticate with a token (token mode) or an
    mTLS client context (mtls mode)."""
    params: dict[str, str] = {}
    if name:
        params["name"] = name
    if capability:
        params["capability"] = capability
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(
        timeout=timeout, verify=ssl_context if ssl_context is not None else True
    ) as client:
        response = await client.get(
            f"{core_url.rstrip('/')}/v1/registry/services",
            params=params,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()


class EventBusClient:
    """Client for the Atlas Event Bus.

    token mode: pass the Core-issued service token.
    mtls mode: pass the service's client SSL context — identity travels
    in the certificate, no token needed.
    """

    def __init__(
        self,
        bus_url: str,
        token: str | None = None,
        *,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float = 35.0,
    ) -> None:
        self.bus_url = bus_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            verify=ssl_context if ssl_context is not None else True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def publish(
        self, topic: str, payload: dict, *, occurred_at: str | None = None
    ) -> dict:
        body: dict = {"topic": topic, "payload": payload}
        if occurred_at:
            body["occurred_at"] = occurred_at
        response = await self._client.post(f"{self.bus_url}/v1/events", json=body)
        response.raise_for_status()
        return response.json()

    async def ensure_subscription(self, name: str, topics: list[str]) -> str:
        response = await self._client.post(
            f"{self.bus_url}/v1/subscriptions",
            json={"name": name, "topics": topics},
        )
        response.raise_for_status()
        return response.json()["id"]

    async def pull(
        self, subscription_id: str, *, max_messages: int = 10, wait_seconds: int = 0
    ) -> list[dict]:
        response = await self._client.post(
            f"{self.bus_url}/v1/subscriptions/{subscription_id}/pull",
            json={"max_messages": max_messages, "wait_seconds": wait_seconds},
        )
        response.raise_for_status()
        return response.json()["messages"]

    async def ack(self, subscription_id: str, delivery_ids: list[int]) -> None:
        response = await self._client.post(
            f"{self.bus_url}/v1/subscriptions/{subscription_id}/ack",
            json={"delivery_ids": delivery_ids},
        )
        response.raise_for_status()
