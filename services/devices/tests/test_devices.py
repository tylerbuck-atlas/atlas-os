# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Devices: adapter scoping, class policy, command routing, memory sync."""

from __future__ import annotations

from .conftest import (
    ADAPTER_TOKEN,
    OPERATOR_TOKEN,
    OTHER_ADAPTER_TOKEN,
    PLANNER_TOKEN,
    SERVICE_TOKEN,
    auth,
    sync,
)


class TestAdapterSync:
    async def test_sync_creates_and_updates(self, client):
        created = await sync(client, state={"on": False})
        assert created["adapter"] == "atlas.adapter.virtual"
        updated = await sync(client, state={"on": True})
        assert updated["id"] == created["id"]
        assert updated["state"] == {"on": True}

    async def test_adapter_cannot_sync_as_another(self, client):
        response = await client.put(
            "/v1/adapters/atlas.adapter.virtual/devices/x",
            json={"name": "X", "kind": "light"},
            headers=auth(OTHER_ADAPTER_TOKEN),
        )
        assert response.status_code == 403

    async def test_unknown_kind_rejected(self, client):
        response = await client.put(
            "/v1/adapters/atlas.adapter.virtual/devices/x",
            json={"name": "X", "kind": "flux-capacitor"},
            headers=auth(ADAPTER_TOKEN),
        )
        assert response.status_code == 422

    async def test_events_emitted_and_class_redacted(self, app, client):
        await sync(client, native_id="lamp", data_class=1, state={"on": True})
        await sync(client, native_id="presence", kind="sensor", data_class=3,
                   commands=[], state={"present": True}, name="Presence")
        events = await app.state.store.list_events_after(0, 10)
        by_topic = {}
        for _, topic, payload, _ in events:
            by_topic.setdefault(topic, []).append(payload)
        discovered = by_topic["devices.device.discovered"]
        lamp = next(p for p in discovered if p.get("kind") == "light")
        presence = next(p for p in discovered if p.get("kind") == "sensor")
        assert lamp["state"] == {"on": True}
        assert presence.get("redacted") is True
        assert "present" not in str(presence)

    async def test_state_synced_to_memory_with_class_and_provenance(self, world, client):
        record = await sync(client, data_class=1)
        assert len(world.memory_writes) == 1
        write = world.memory_writes[0]
        assert write["path"] == f"/v1/facts/home.devices/{record['id']}"
        assert write["body"]["class"] == 1
        assert write["body"]["provenance"] == "adapter:atlas.adapter.virtual"
        assert write["body"]["payload"]["state"] == {"on": False}


class TestClassPolicy:
    async def test_class3_state_redacted_for_random_services(self, client):
        record = await sync(client, native_id="presence", kind="sensor",
                            data_class=3, commands=[], state={"present": True})
        response = await client.get(
            f"/v1/devices/{record['id']}", headers=auth(SERVICE_TOKEN)
        )
        assert response.json()["state"] == {"redacted": True}

    async def test_class3_state_visible_to_steward_planner_operator(self, client):
        record = await sync(client, native_id="presence", kind="sensor",
                            data_class=3, commands=[], state={"present": True})
        for token in (ADAPTER_TOKEN, PLANNER_TOKEN, OPERATOR_TOKEN):
            response = await client.get(
                f"/v1/devices/{record['id']}", headers=auth(token)
            )
            assert response.json()["state"] == {"present": True}, token

    async def test_listing_filters(self, client):
        await sync(client, native_id="lamp", kind="light")
        await sync(client, native_id="temp", kind="sensor", commands=[],
                   state={"temp_c": 21}, name="Temp")
        response = await client.get(
            "/v1/devices", params={"kind": "light"}, headers=auth(SERVICE_TOKEN)
        )
        assert [d["kind"] for d in response.json()] == ["light"]


class TestCommands:
    async def test_random_service_cannot_command(self, client, world):
        record = await sync(client)
        response = await client.post(
            "/v1/invoke/devices.command",
            json={"device_id": record["id"], "command": "turn_on"},
            headers=auth(SERVICE_TOKEN),
        )
        assert response.status_code == 403
        assert "Planner" in response.json()["detail"]
        assert world.adapter_calls == []

    async def test_even_the_adapter_cannot_command(self, client, world):
        record = await sync(client)
        response = await client.post(
            "/v1/invoke/devices.command",
            json={"device_id": record["id"], "command": "turn_on"},
            headers=auth(ADAPTER_TOKEN),
        )
        assert response.status_code == 403

    async def test_planner_command_routes_to_adapter_and_updates_state(
        self, client, world
    ):
        record = await sync(client, state={"on": False})
        world.adapter_result = {"result": {"applied": "turn_on"}, "state": {"on": True}}
        response = await client.post(
            "/v1/invoke/devices.command",
            json={"device_id": record["id"], "command": "turn_on"},
            headers=auth(PLANNER_TOKEN),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == {"on": True}
        assert world.adapter_calls == [{
            "native_id": "virtual-light-1", "command": "turn_on", "params": {},
        }]
        # State persisted + memory re-synced
        device = await client.get(f"/v1/devices/{record['id']}", headers=auth(PLANNER_TOKEN))
        assert device.json()["state"] == {"on": True}
        assert any(w["body"]["payload"]["state"] == {"on": True}
                   for w in world.memory_writes)

    async def test_unsupported_command_rejected_before_adapter(self, client, world):
        record = await sync(client, commands=["turn_on"])
        response = await client.post(
            "/v1/invoke/devices.command",
            json={"device_id": record["id"], "command": "self_destruct"},
            headers=auth(PLANNER_TOKEN),
        )
        assert response.status_code == 422
        assert world.adapter_calls == []

    async def test_adapter_failure_reported(self, client, world):
        record = await sync(client)
        world.adapter_result = 500
        response = await client.post(
            "/v1/invoke/devices.command",
            json={"device_id": record["id"], "command": "turn_on"},
            headers=auth(OPERATOR_TOKEN),
        )
        assert response.status_code == 502

    async def test_offline_device_refuses_commands(self, client):
        record = await sync(client)
        await client.delete(f"/v1/devices/{record['id']}", headers=auth(ADAPTER_TOKEN))
        response = await client.post(
            "/v1/invoke/devices.command",
            json={"device_id": record["id"], "command": "turn_on"},
            headers=auth(PLANNER_TOKEN),
        )
        assert response.status_code == 409


class TestOffline:
    async def test_only_steward_or_operator_marks_offline(self, client):
        record = await sync(client)
        response = await client.delete(
            f"/v1/devices/{record['id']}", headers=auth(OTHER_ADAPTER_TOKEN)
        )
        assert response.status_code == 403
        response = await client.delete(
            f"/v1/devices/{record['id']}", headers=auth(ADAPTER_TOKEN)
        )
        assert response.json()["online"] is False
