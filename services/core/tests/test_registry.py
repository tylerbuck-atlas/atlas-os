# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Registration, discovery, auth scoping, deregistration."""

from __future__ import annotations

from .conftest import REGISTRATION, bootstrap_headers, register, token_headers


class TestRegistration:
    async def test_register_returns_record_and_one_time_token(self, client):
        body = await register(client)
        assert body["service"]["name"] == "atlas.echo"
        assert body["service"]["status"] == "starting"
        assert body["service"]["instance_id"]
        assert len(body["service_token"]) >= 32
        assert body["heartbeat_interval_seconds"] == 1

    async def test_register_requires_bootstrap_token(self, client):
        response = await client.post("/v1/registry/services", json=REGISTRATION)
        assert response.status_code == 401

    async def test_service_token_cannot_register(self, client):
        token = (await register(client))["service_token"]
        response = await client.post(
            "/v1/registry/services", json=REGISTRATION, headers=token_headers(token)
        )
        assert response.status_code == 401

    async def test_rejects_invalid_service_name(self, client):
        bad = dict(REGISTRATION, name="NotValid")
        response = await client.post(
            "/v1/registry/services", json=bad, headers=bootstrap_headers()
        )
        assert response.status_code == 422

    async def test_rejects_invalid_version(self, client):
        bad = dict(REGISTRATION, version="one.two")
        response = await client.post(
            "/v1/registry/services", json=bad, headers=bootstrap_headers()
        )
        assert response.status_code == 422

    async def test_reregistration_supersedes_and_revokes_old_token(self, client):
        first = await register(client)
        second = await register(client)
        assert first["service"]["instance_id"] != second["service"]["instance_id"]

        # Old token is dead: heartbeat with it → 410 (re-register signal).
        response = await client.post(
            f"/v1/registry/services/{first['service']['instance_id']}/heartbeat",
            headers=token_headers(first["service_token"]),
        )
        assert response.status_code == 410

        # Only the new instance is visible.
        listing = await client.get("/v1/registry/services", headers=bootstrap_headers())
        live = [s for s in listing.json() if s["name"] == "atlas.echo"]
        assert len(live) == 1
        assert live[0]["instance_id"] == second["service"]["instance_id"]


class TestDiscovery:
    async def test_discovery_requires_auth(self, client):
        assert (await client.get("/v1/registry/services")).status_code == 401

    async def test_registered_service_can_discover(self, client):
        token = (await register(client))["service_token"]
        response = await client.get(
            "/v1/registry/services", headers=token_headers(token)
        )
        assert response.status_code == 200
        assert len(response.json()) == 1

    async def test_filter_by_capability(self, client):
        await register(client)
        other = dict(
            REGISTRATION,
            name="atlas.other",
            capabilities=["other.thing"],
        )
        await register(client, other)

        response = await client.get(
            "/v1/registry/services",
            params={"capability": "echo.reply"},
            headers=bootstrap_headers(),
        )
        names = [s["name"] for s in response.json()]
        assert names == ["atlas.echo"]

    async def test_get_unknown_instance_404(self, client):
        response = await client.get(
            "/v1/registry/services/nope", headers=bootstrap_headers()
        )
        assert response.status_code == 404


class TestTokenScoping:
    async def test_service_cannot_heartbeat_for_another(self, client):
        a = await register(client)
        b = await register(client, dict(REGISTRATION, name="atlas.other"))

        response = await client.post(
            f"/v1/registry/services/{a['service']['instance_id']}/heartbeat",
            headers=token_headers(b["service_token"]),
        )
        assert response.status_code == 403

    async def test_service_cannot_deregister_another(self, client):
        a = await register(client)
        b = await register(client, dict(REGISTRATION, name="atlas.other"))

        response = await client.delete(
            f"/v1/registry/services/{a['service']['instance_id']}",
            headers=token_headers(b["service_token"]),
        )
        assert response.status_code == 403


class TestDeregistration:
    async def test_deregister_removes_and_revokes(self, client):
        body = await register(client)
        instance_id = body["service"]["instance_id"]
        token = body["service_token"]

        response = await client.delete(
            f"/v1/registry/services/{instance_id}", headers=token_headers(token)
        )
        assert response.status_code == 204

        # Gone from discovery.
        listing = await client.get("/v1/registry/services", headers=bootstrap_headers())
        assert listing.json() == []

        # Token revoked.
        response = await client.post(
            f"/v1/registry/services/{instance_id}/heartbeat",
            headers=token_headers(token),
        )
        assert response.status_code == 410


class TestEvents:
    async def test_lifecycle_emits_events(self, client):
        body = await register(client)
        await client.delete(
            f"/v1/registry/services/{body['service']['instance_id']}",
            headers=token_headers(body["service_token"]),
        )

        response = await client.get("/v1/system/events", headers=bootstrap_headers())
        topics = [e["topic"] for e in response.json()]
        assert "registry.service.registered" in topics
        assert "registry.service.status_changed" in topics
