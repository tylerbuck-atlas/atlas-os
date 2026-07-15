# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Memory service logic: writes, reads, policy, change events, and
materialization of bus events into queryable state."""

from __future__ import annotations

import logging

from atlas_sdk.service_auth import Identity

from . import SERVICE_NAME
from .models import CLASS_HOUSEHOLD, FactRecord, FactWrite, can_read
from .store import MemoryStore

log = logging.getLogger("atlas.memory")

SYSTEM_SERVICES_NS = "system.services"


class MemoryService:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    # -- writes ---------------------------------------------------------------

    async def write_fact(
        self, namespace: str, key: str, write: FactWrite, *, source: str
    ) -> FactRecord:
        record = await self._store.append_version(
            namespace=namespace,
            key=key,
            payload=write.payload,
            data_class=write.data_class,
            provenance=write.provenance,
            source=source,
            owner=write.owner,
        )
        await self._emit_change(record)
        return record

    async def tombstone(self, namespace: str, key: str, *, source: str) -> FactRecord | None:
        latest = await self._store.latest(namespace, key)
        if latest is None or latest.deleted:
            return None
        record = await self._store.append_version(
            namespace=namespace,
            key=key,
            payload={},
            data_class=latest.data_class,
            provenance=f"tombstone:{source}",
            source=source,
            owner=latest.owner,
            deleted=True,
        )
        await self._emit_change(record)
        return record

    async def _emit_change(self, record: FactRecord) -> None:
        """Publish fact changes — with class-aware redaction.

        Class 0–1 events carry the payload; Class 2–3 events announce
        only that *something* changed (namespace/key/version/class), so
        the bus never becomes a side channel around the read policy.
        """
        event = {
            "namespace": record.namespace,
            "key": record.key,
            "version": record.version,
            "class": record.data_class,
            "deleted": record.deleted,
            "source": record.source,
        }
        if record.data_class <= CLASS_HOUSEHOLD:
            event["payload"] = record.payload
        else:
            event["redacted"] = True
        await self._store.append_event("memory.fact.changed", event)

    # -- reads (policy-checked) --------------------------------------------------

    async def read_latest(
        self, namespace: str, key: str, identity: Identity
    ) -> tuple[FactRecord | None, bool]:
        """(record, allowed). Missing and forbidden are distinguished by
        the API layer as 404 vs 403."""
        record = await self._store.latest(namespace, key)
        if record is None:
            return None, True
        return record, can_read(identity, record)

    async def read_history(
        self, namespace: str, key: str, identity: Identity, *, limit: int
    ) -> tuple[list[FactRecord], bool]:
        records = await self._store.history(namespace, key, limit=limit)
        if not records:
            return [], True
        return records, can_read(identity, records[0])

    async def query(
        self,
        namespace: str,
        identity: Identity,
        *,
        key_prefix: str | None,
        max_class: int | None,
    ) -> list[FactRecord]:
        """Latest facts in a namespace the caller is allowed to see.

        Unreadable (Class 3, non-steward) facts are filtered out, not
        errored: discovery must not leak existence patterns to bulk scans.
        """
        records = await self._store.list_latest(
            namespace, key_prefix=key_prefix, max_class=max_class
        )
        return [r for r in records if can_read(identity, r)]

    # -- event materialization ------------------------------------------------------

    async def materialize_registry_event(self, event: dict) -> None:
        """Fold a registry.* bus event into queryable system state.

        This is the Event Bus becoming *memory*: transient events turn
        into durable, versioned facts about what the system looks like.
        """
        topic = event.get("topic", "")
        payload = event.get("payload", {})
        name = payload.get("name")
        if not name:
            return
        if topic == "registry.service.registered":
            state = {
                "status": "starting",
                "instance_id": payload.get("instance_id"),
                "version": payload.get("version"),
                "capabilities": payload.get("capabilities", []),
            }
        elif topic == "registry.service.status_changed":
            current = await self._store.latest(SYSTEM_SERVICES_NS, name)
            state = dict(current.payload) if current and not current.deleted else {}
            state["status"] = payload.get("to")
            state["status_reason"] = payload.get("reason")
            if payload.get("instance_id"):
                state["instance_id"] = payload["instance_id"]
        else:
            return
        await self._store.append_version(
            namespace=SYSTEM_SERVICES_NS,
            key=name,
            payload=state,
            data_class=CLASS_HOUSEHOLD,
            provenance=f"event:{topic}",
            source=SERVICE_NAME,
            owner=None,
        )
        log.debug("materialized %s -> %s", topic, name)
