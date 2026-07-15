"""Schema registry: versioning and publish-time validation."""

from __future__ import annotations

from .conftest import CORE_TOKEN, auth, publish

REGISTERED_SCHEMA = {
    "type": "object",
    "properties": {
        "instance_id": {"type": "string"},
        "name": {"type": "string"},
    },
    "required": ["instance_id", "name"],
    "additionalProperties": True,
}


async def register(client, topic="registry.service.registered", schema=None):
    response = await client.put(
        f"/v1/schemas/{topic}",
        json={"json_schema": schema or REGISTERED_SCHEMA},
        headers=auth(CORE_TOKEN),
    )
    return response


class TestSchemaRegistry:
    async def test_register_and_get(self, client):
        response = await register(client)
        assert response.status_code == 201
        assert response.json()["version"] == 1

        got = await client.get(
            "/v1/schemas/registry.service.registered", headers=auth(CORE_TOKEN)
        )
        assert got.json()["json_schema"]["required"] == ["instance_id", "name"]

    async def test_versions_increment(self, client):
        assert (await register(client)).json()["version"] == 1
        assert (await register(client)).json()["version"] == 2

    async def test_invalid_schema_document_rejected(self, client):
        response = await register(client, schema={"type": "not-a-real-type"})
        assert response.status_code == 422

    async def test_unknown_topic_schema_404(self, client):
        response = await client.get("/v1/schemas/no.such.topic", headers=auth(CORE_TOKEN))
        assert response.status_code == 404

    async def test_requires_auth(self, client):
        response = await client.get("/v1/schemas")
        assert response.status_code == 401


class TestPublishValidation:
    async def test_valid_payload_stamped_with_schema_version(self, client):
        await register(client)
        envelope = await publish(
            client,
            topic="registry.service.registered",
            payload={"instance_id": "abc", "name": "atlas.echo"},
        )
        assert envelope["schema_version"] == 1

    async def test_invalid_payload_rejected(self, client):
        await register(client)
        response = await client.post(
            "/v1/events",
            json={
                "topic": "registry.service.registered",
                "payload": {"name": "missing instance_id"},
            },
            headers=auth(CORE_TOKEN),
        )
        assert response.status_code == 422
        assert "does not match schema v1" in response.json()["detail"]

    async def test_topic_without_schema_publishes_freely(self, client):
        envelope = await publish(client, topic="unregulated.topic", payload={"x": 1})
        assert envelope["schema_version"] is None
