"""Token introspection: /v1/auth/introspect."""

from __future__ import annotations

from .conftest import BOOTSTRAP_TOKEN, bootstrap_headers, register, token_headers


class TestIntrospection:
    async def test_requires_auth(self, client):
        response = await client.post("/v1/auth/introspect", json={"token": "x"})
        assert response.status_code == 401

    async def test_service_token_resolves_to_identity(self, client):
        body = await register(client)
        response = await client.post(
            "/v1/auth/introspect",
            json={"token": body["service_token"]},
            headers=bootstrap_headers(),
        )
        data = response.json()
        assert data["active"] is True
        assert data["service"]["name"] == "atlas.echo"
        assert data["service"]["instance_id"] == body["service"]["instance_id"]

    async def test_bootstrap_token_resolves_to_core(self, client):
        response = await client.post(
            "/v1/auth/introspect",
            json={"token": BOOTSTRAP_TOKEN},
            headers=bootstrap_headers(),
        )
        data = response.json()
        assert data["active"] is True
        assert data["service"]["name"] == "atlas.core"

    async def test_garbage_token_is_inactive(self, client):
        response = await client.post(
            "/v1/auth/introspect",
            json={"token": "garbage"},
            headers=bootstrap_headers(),
        )
        assert response.json() == {"active": False, "service": None}

    async def test_revoked_token_is_inactive(self, client):
        body = await register(client)
        token = body["service_token"]
        await client.delete(
            f"/v1/registry/services/{body['service']['instance_id']}",
            headers=token_headers(token),
        )
        response = await client.post(
            "/v1/auth/introspect", json={"token": token}, headers=bootstrap_headers()
        )
        assert response.json()["active"] is False

    async def test_services_can_introspect(self, client):
        """A registered service (like the bus) can introspect other tokens."""
        bus = await register(client, {
            "name": "atlas.eventbus",
            "version": "0.1.0",
            "address": "http://atlas-eventbus:8200",
            "health_url": "http://atlas-eventbus:8200/healthz",
            "capabilities": ["eventbus.publish"],
            "metadata": {},
        })
        echo = await register(client)
        response = await client.post(
            "/v1/auth/introspect",
            json={"token": echo["service_token"]},
            headers=token_headers(bus["service_token"]),
        )
        assert response.json()["service"]["name"] == "atlas.echo"
