# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Device Manager logic: sync, events, command routing, Memory sync.

Truth flow: adapter observes the physical world → syncs here → the
Device Manager emits class-redacted events and writes the state into
Atlas Memory as a fact (`home.devices/{id}`) with adapter provenance.
Action flow: Planner (only) → `devices.command` → routed to the
steward adapter's `adapter.command` → adapter acts → syncs new state.
"""

from __future__ import annotations

import asyncio
import logging
import ssl

import httpx

from atlas_sdk import AtlasService, discover_service

from .models import CLASS_INTIMATE, CommandRequest, CommandResult, DeviceRecord, DeviceSync
from .store import DeviceStore

log = logging.getLogger("atlas.devices")


class CommandError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class DeviceService:
    def __init__(
        self,
        store: DeviceStore,
        atlas: AtlasService,
        *,
        command_timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._atlas = atlas
        self._command_timeout = command_timeout
        self._client = client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            ctx = self._atlas.client_ssl_context()
            self._client = httpx.AsyncClient(
                timeout=self._command_timeout,
                verify=ctx if ctx is not None else True,
                headers=(
                    {}
                    if self._atlas.security_mode == "mtls"
                    else {"Authorization": f"Bearer {self._atlas.service_token or ''}"}
                ),
            )
        return self._client

    # -- adapter sync -----------------------------------------------------------

    async def sync_device(
        self, *, adapter: str, native_id: str, sync: DeviceSync
    ) -> DeviceRecord:
        record, created = await self._store.upsert(
            adapter=adapter, native_id=native_id, sync=sync
        )
        await self._emit_device_event(
            "devices.device.discovered" if created else "devices.device.state_changed",
            record,
        )
        await self._sync_memory(record)
        return record

    async def mark_offline(self, device_id: str) -> DeviceRecord | None:
        record = await self._store.get(device_id)
        if record is None:
            return None
        await self._store.set_offline(device_id)
        record = await self._store.get(device_id)
        assert record is not None
        await self._emit_device_event("devices.device.offline", record)
        await self._sync_memory(record)
        return record

    async def _emit_device_event(self, topic: str, record: DeviceRecord) -> None:
        """Class-redacted, like Memory's change events: intimate device
        states never cross the bus in the clear."""
        payload = {
            "device_id": record.id,
            "adapter": record.adapter,
            "kind": record.kind,
            "room": record.room,
            "class": record.data_class,
            "online": record.online,
        }
        if record.data_class <= 1:
            payload["name"] = record.name
            payload["state"] = record.state
        else:
            payload["redacted"] = True
        await self._store.append_event(topic, payload)

    async def _sync_memory(self, record: DeviceRecord) -> None:
        """Write device state into Atlas Memory as a fact (best effort;
        the device store remains the steward copy)."""
        memory = await self._find_service("atlas.memory")
        if memory is None:
            return
        try:
            response = await self._http().put(
                f"{memory['address'].rstrip('/')}/v1/facts/home.devices/{record.id}",
                json={
                    "payload": {
                        "name": record.name, "kind": record.kind, "room": record.room,
                        "state": record.state, "online": record.online,
                        "adapter": record.adapter,
                    },
                    "class": record.data_class,
                    "provenance": f"adapter:{record.adapter}",
                },
            )
            if response.status_code >= 300:
                log.warning("memory sync rejected: %s", response.status_code)
        except (httpx.HTTPError, ssl.SSLError) as exc:
            log.debug("memory sync skipped (%s)", exc)

    async def _find_service(self, name: str) -> dict | None:
        token, ssl_ctx = self._atlas.bus_credentials()
        try:
            services = await discover_service(
                core_url=self._atlas.core_url, token=token, ssl_context=ssl_ctx, name=name
            )
        except (httpx.HTTPError, ssl.SSLError):
            return None
        live = [s for s in services if s.get("status") in ("starting", "healthy")]
        return live[0] if live else None

    # -- commands (the Planner's door) ----------------------------------------------

    async def execute_command(self, request: CommandRequest) -> CommandResult:
        record = await self._store.get(request.device_id)
        if record is None:
            raise CommandError(404, "unknown device")
        if not record.online:
            raise CommandError(409, f"device {record.name!r} is offline")
        if request.command not in record.commands:
            raise CommandError(
                422,
                f"device {record.name!r} does not accept {request.command!r} "
                f"(accepts: {record.commands})",
            )
        adapter = await self._find_service(record.adapter)
        if adapter is None:
            raise CommandError(503, f"adapter {record.adapter!r} is not available")

        try:
            response = await self._http().post(
                f"{adapter['address'].rstrip('/')}/v1/invoke/adapter.command",
                json={
                    "native_id": record.native_id,
                    "command": request.command,
                    "params": request.params,
                },
            )
        except (httpx.HTTPError, ssl.SSLError) as exc:
            raise CommandError(502, f"adapter unreachable: {exc}")
        if response.status_code >= 300:
            raise CommandError(
                502, f"adapter returned {response.status_code}: {response.text[:200]}"
            )
        body = response.json() if response.content else {}
        new_state = body.get("state", record.state)

        updated, _ = await self._store.upsert(
            adapter=record.adapter,
            native_id=record.native_id,
            sync=DeviceSync(
                name=record.name, kind=record.kind, room=record.room,
                data_class=record.data_class, commands=record.commands,
                state=new_state, online=True, metadata=record.metadata,
            ),
        )
        await self._emit_device_event("devices.device.state_changed", updated)
        await self._sync_memory(updated)
        await self._store.append_event(
            "devices.command.executed",
            {
                "device_id": record.id, "adapter": record.adapter,
                "command": request.command, "class": record.data_class,
            },
        )
        return CommandResult(
            device_id=record.id, command=request.command,
            adapter=record.adapter, result=body.get("result", {}), state=updated.state,
        )


def can_read_state(identity_name: str, is_operator: bool, record: DeviceRecord) -> bool:
    """Class 3 device state: steward adapter, the Planner (it must act on
    the home), and the operator."""
    if record.data_class < CLASS_INTIMATE:
        return True
    return is_operator or identity_name in (record.adapter, "atlas.planner")
