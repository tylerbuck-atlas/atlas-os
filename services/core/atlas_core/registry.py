# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""The Atlas service registry.

Owns all registry state transitions and emits an event for each one.
Events are written to Core's internal event log today and will be
published on the Atlas Event Bus (Milestone 2) with identical shape.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid

from .models import Event, ServiceRecord, ServiceRegistration, ServiceStatus, utcnow
from .store import RegistryStore

log = logging.getLogger("atlas.core.registry")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RegistryError(Exception):
    """Base error for registry operations."""


class UnknownServiceError(RegistryError):
    """The instance ID does not exist or is deregistered."""


class ServiceRegistry:
    """Registration, discovery, and lifecycle transitions for Atlas services."""

    def __init__(self, store: RegistryStore) -> None:
        self._store = store

    # -- registration -----------------------------------------------------

    async def register(self, registration: ServiceRegistration) -> tuple[ServiceRecord, str]:
        """Register a service instance.

        A repeat registration for the same service name supersedes any
        previous live instance: the old instance is deregistered and its
        token revoked. This makes restarts and redeploys safe by default.

        Returns the new record and its plaintext service token (the only
        time the token ever exists outside the calling service).
        """
        now = utcnow()

        for previous in await self._store.list_services(name=registration.name):
            await self._transition(previous, ServiceStatus.DEREGISTERED, reason="superseded")
            await self._store.revoke_token(previous.instance_id)

        token = secrets.token_urlsafe(32)
        record = ServiceRecord(
            instance_id=uuid.uuid4().hex,
            name=registration.name,
            version=registration.version,
            address=registration.address,
            health_url=registration.health_url,
            capabilities=registration.capabilities,
            metadata=registration.metadata,
            status=ServiceStatus.STARTING,
            registered_at=now,
            status_changed_at=now,
            last_heartbeat_at=now,
        )
        await self._store.insert_service(record, hash_token(token))
        await self._emit(
            "registry.service.registered",
            {
                "instance_id": record.instance_id,
                "name": record.name,
                "version": record.version,
                "capabilities": record.capabilities,
            },
        )
        log.info("registered %s (%s) v%s", record.name, record.instance_id, record.version)
        return record, token

    async def deregister(self, instance_id: str) -> None:
        record = await self._require(instance_id)
        await self._transition(record, ServiceStatus.DEREGISTERED, reason="requested")
        await self._store.revoke_token(instance_id)
        log.info("deregistered %s (%s)", record.name, instance_id)

    # -- heartbeats & health transitions -----------------------------------

    async def heartbeat(self, instance_id: str) -> ServiceRecord:
        record = await self._require(instance_id)
        now = utcnow()
        await self._store.record_heartbeat(instance_id, now)
        if record.status in (ServiceStatus.STARTING, ServiceStatus.UNREACHABLE):
            await self._transition(record, ServiceStatus.HEALTHY, reason="heartbeat")
        record = await self._store.get_service(instance_id)
        assert record is not None
        return record

    async def mark_status(
        self, instance_id: str, status: ServiceStatus, *, reason: str
    ) -> None:
        record = await self._store.get_service(instance_id)
        if record is None or record.status == ServiceStatus.DEREGISTERED:
            return
        if record.status != status:
            await self._transition(record, status, reason=reason)

    # -- discovery ---------------------------------------------------------

    async def get(self, instance_id: str) -> ServiceRecord | None:
        return await self._store.get_service(instance_id)

    async def find(
        self,
        *,
        name: str | None = None,
        capability: str | None = None,
        status: ServiceStatus | None = None,
    ) -> list[ServiceRecord]:
        return await self._store.list_services(name=name, capability=capability, status=status)

    async def authenticate_token(self, token: str) -> ServiceRecord | None:
        """Resolve a service token to its live service record, or None."""
        record = await self._store.get_service_by_token_hash(hash_token(token))
        if record is None or record.status == ServiceStatus.DEREGISTERED:
            return None
        return record

    # -- internals ----------------------------------------------------------

    async def _require(self, instance_id: str) -> ServiceRecord:
        record = await self._store.get_service(instance_id)
        if record is None or record.status == ServiceStatus.DEREGISTERED:
            raise UnknownServiceError(instance_id)
        return record

    async def _transition(
        self, record: ServiceRecord, status: ServiceStatus, *, reason: str
    ) -> None:
        now = utcnow()
        await self._store.set_status(record.instance_id, status, now)
        await self._emit(
            "registry.service.status_changed",
            {
                "instance_id": record.instance_id,
                "name": record.name,
                "from": record.status.value,
                "to": status.value,
                "reason": reason,
            },
        )
        if status in (ServiceStatus.UNHEALTHY, ServiceStatus.UNREACHABLE):
            log.warning(
                "%s (%s): %s -> %s (%s)",
                record.name, record.instance_id, record.status.value, status.value, reason,
            )
        else:
            log.info(
                "%s (%s): %s -> %s (%s)",
                record.name, record.instance_id, record.status.value, status.value, reason,
            )

    async def _emit(self, topic: str, payload: dict) -> None:
        await self._store.append_event(Event(topic=topic, payload=payload))
