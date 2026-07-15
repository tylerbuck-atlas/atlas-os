# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Facts: versioning, provenance, tombstones, and the data-class policy."""

from __future__ import annotations

from .conftest import OPERATOR_TOKEN, OTHER_TOKEN, SENSOR_TOKEN, auth, put_fact


class TestVersioning:
    async def test_writes_append_versions(self, client):
        first = await put_fact(client, payload={"temp_c": 21})
        second = await put_fact(client, payload={"temp_c": 22})
        assert first["version"] == 1
        assert second["version"] == 2

        latest = await client.get("/v1/facts/home.rooms/kitchen", headers=auth(SENSOR_TOKEN))
        assert latest.json()["payload"] == {"temp_c": 22}

    async def test_history_preserved_newest_first(self, client):
        await put_fact(client, payload={"temp_c": 21})
        await put_fact(client, payload={"temp_c": 22})
        history = await client.get(
            "/v1/facts/home.rooms/kitchen/history", headers=auth(SENSOR_TOKEN)
        )
        versions = [h["version"] for h in history.json()]
        payloads = [h["payload"]["temp_c"] for h in history.json()]
        assert versions == [2, 1]
        assert payloads == [22, 21]

    async def test_source_is_identity_not_client_claim(self, client):
        record = await put_fact(client, token=SENSOR_TOKEN)
        assert record["source"] == "atlas.sensor"

    async def test_provenance_required(self, client):
        response = await client.put(
            "/v1/facts/home.rooms/kitchen",
            json={"payload": {}, "class": 1, "provenance": "  "},
            headers=auth(SENSOR_TOKEN),
        )
        assert response.status_code == 422

    async def test_tombstone_preserves_history(self, client):
        await put_fact(client)
        response = await client.delete(
            "/v1/facts/home.rooms/kitchen", headers=auth(SENSOR_TOKEN)
        )
        assert response.status_code == 200
        assert response.json()["deleted"] is True

        gone = await client.get("/v1/facts/home.rooms/kitchen", headers=auth(SENSOR_TOKEN))
        assert gone.status_code == 404

        history = await client.get(
            "/v1/facts/home.rooms/kitchen/history", headers=auth(SENSOR_TOKEN)
        )
        assert len(history.json()) == 2  # original + tombstone

    async def test_unknown_fact_404(self, client):
        response = await client.get("/v1/facts/home.rooms/nope", headers=auth(SENSOR_TOKEN))
        assert response.status_code == 404

    async def test_requires_auth(self, client):
        assert (await client.get("/v1/facts/home.rooms/kitchen")).status_code == 401


class TestQuery:
    async def test_namespace_query_latest_only(self, client):
        await put_fact(client, key="kitchen", payload={"v": 1})
        await put_fact(client, key="kitchen", payload={"v": 2})
        await put_fact(client, key="garage", payload={"v": 1})

        response = await client.get("/v1/facts/home.rooms", headers=auth(SENSOR_TOKEN))
        facts = {f["key"]: f for f in response.json()}
        assert set(facts) == {"kitchen", "garage"}
        assert facts["kitchen"]["version"] == 2

    async def test_prefix_and_class_filters(self, client):
        await put_fact(client, key="kitchen.temp", data_class=1)
        await put_fact(client, key="kitchen.motion", data_class=3)
        await put_fact(client, key="garage.temp", data_class=1)

        response = await client.get(
            "/v1/facts/home.rooms",
            params={"key_prefix": "kitchen.", "max_class": 1},
            headers=auth(SENSOR_TOKEN),
        )
        keys = [f["key"] for f in response.json()]
        assert keys == ["kitchen.temp"]


class TestDataClassPolicy:
    async def test_class3_readable_by_steward(self, client):
        await put_fact(client, key="presence", data_class=3, token=SENSOR_TOKEN)
        response = await client.get(
            "/v1/facts/home.rooms/presence", headers=auth(SENSOR_TOKEN)
        )
        assert response.status_code == 200

    async def test_class3_hidden_from_other_services(self, client):
        await put_fact(client, key="presence", data_class=3, token=SENSOR_TOKEN)
        response = await client.get(
            "/v1/facts/home.rooms/presence", headers=auth(OTHER_TOKEN)
        )
        assert response.status_code == 403

        # And filtered out of bulk queries, not errored.
        listing = await client.get("/v1/facts/home.rooms", headers=auth(OTHER_TOKEN))
        assert listing.json() == []

    async def test_class3_readable_by_operator(self, client):
        await put_fact(client, key="presence", data_class=3, token=SENSOR_TOKEN)
        response = await client.get(
            "/v1/facts/home.rooms/presence", headers=auth(OPERATOR_TOKEN)
        )
        assert response.status_code == 200

    async def test_class2_readable_across_services(self, client):
        await put_fact(client, key="notes", data_class=2, token=SENSOR_TOKEN)
        response = await client.get("/v1/facts/home.rooms/notes", headers=auth(OTHER_TOKEN))
        assert response.status_code == 200

    async def test_class3_history_protected(self, client):
        await put_fact(client, key="presence", data_class=3, token=SENSOR_TOKEN)
        response = await client.get(
            "/v1/facts/home.rooms/presence/history", headers=auth(OTHER_TOKEN)
        )
        assert response.status_code == 403


class TestChangeEvents:
    async def test_household_change_carries_payload(self, app, client):
        await put_fact(client, data_class=1, payload={"temp_c": 21})
        events = await app.state.store.list_events_after(0, 10)
        assert len(events) == 1
        _, topic, payload, _ = events[0]
        assert topic == "memory.fact.changed"
        assert payload["payload"] == {"temp_c": 21}

    async def test_personal_change_is_redacted(self, app, client):
        await put_fact(client, key="presence", data_class=3, payload={"who": "tyler"})
        events = await app.state.store.list_events_after(0, 10)
        _, _, payload, _ = events[0]
        assert payload["redacted"] is True
        assert "payload" not in payload
        assert "who" not in str(payload)


class TestMaterialization:
    async def test_registry_events_become_facts(self, app, client):
        memory = app.state.memory
        await memory.materialize_registry_event({
            "topic": "registry.service.registered",
            "payload": {"instance_id": "i1", "name": "atlas.echo",
                        "version": "0.3.0", "capabilities": ["echo.reply"]},
        })
        await memory.materialize_registry_event({
            "topic": "registry.service.status_changed",
            "payload": {"instance_id": "i1", "name": "atlas.echo",
                        "from": "starting", "to": "healthy", "reason": "probe ok"},
        })

        response = await client.get(
            "/v1/facts/system.services/atlas.echo", headers=auth(OTHER_TOKEN)
        )
        fact = response.json()
        assert fact["payload"]["status"] == "healthy"
        assert fact["payload"]["capabilities"] == ["echo.reply"]
        assert fact["provenance"] == "event:registry.service.status_changed"
        assert fact["version"] == 2  # full audit trail of state transitions
