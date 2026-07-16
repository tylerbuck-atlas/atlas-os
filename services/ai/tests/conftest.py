# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""AI service test fixtures: fake memory/devices/planner over MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from atlas_ai.api.routes import router
from atlas_ai.backends import StubBackend
from atlas_ai.config import AIConfig
from atlas_ai.engine import AIEngine
from atlas_ai.store import AIStore
from atlas_sdk.service_auth import Identity

USER_TOKEN = "tok-user"
OTHER_TOKEN = "tok-other"
OPERATOR_TOKEN = "tok-operator"

IDENTITIES = {
    USER_TOKEN: Identity("atlas.ui", "ui-1", "0.1.0"),
    OTHER_TOKEN: Identity("atlas.other", "o-1", "0.1.0"),
    OPERATOR_TOKEN: Identity("atlas.operator", "manual-1", "cert"),
}


class FakeIntrospector:
    async def introspect(self, token):
        return IDENTITIES.get(token)

    async def close(self):
        pass


class FakeAtlas:
    core_url = "https://core.test"
    security_mode = "token"
    service_token = "tok-ai"

    def bus_credentials(self):
        return (self.service_token, None)

    def client_ssl_context(self):
        return None


class FakeWorld:
    """Memory, Devices, and Planner behind one MockTransport."""

    def __init__(self) -> None:
        self.devices = [
            {"id": "dev-lamp", "name": "Living room lamp", "kind": "light",
             "room": "living-room", "class": 1, "online": True,
             "commands": ["turn_on", "turn_off", "set_brightness"],
             "state": {"on": False, "brightness": 100}},
            {"id": "dev-temp", "name": "Hallway temperature", "kind": "sensor",
             "room": "hallway", "class": 1, "online": True, "commands": [],
             "state": {"temp_c": 21.5}},
            # Devices API redacts Class 3 for non-stewards — as it would live:
            {"id": "dev-presence", "name": "Front hall presence", "kind": "sensor",
             "room": "hallway", "class": 3, "online": True, "commands": [],
             "state": {"redacted": True}},
        ]
        self.facts = {
            "system.services": [
                {"key": "atlas.echo", "payload": {"status": "healthy"}, "class": 1},
            ],
            "home.rooms": [],
        }
        self.plan_response: dict = {"id": "plan-1", "status": "awaiting_approval"}
        self.plan_submissions: list[dict] = []
        self.services = {
            "atlas.devices": [{"name": "atlas.devices", "status": "healthy",
                               "address": "https://devices.test"}],
            "atlas.memory": [{"name": "atlas.memory", "status": "healthy",
                              "address": "https://memory.test"}],
            "atlas.planner": [{"name": "atlas.planner", "status": "healthy",
                               "address": "https://planner.test"}],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "devices.test" and path == "/v1/devices":
            return httpx.Response(200, json=self.devices)
        if host == "memory.test" and path.startswith("/v1/facts/"):
            namespace = path.split("/v1/facts/")[1]
            return httpx.Response(200, json=self.facts.get(namespace, []))
        if host == "planner.test" and path == "/v1/plans":
            self.plan_submissions.append(json.loads(request.content))
            return httpx.Response(201, json=self.plan_response)
        return httpx.Response(404)


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld()


@pytest.fixture
async def app(world, monkeypatch):
    config = AIConfig(
        bootstrap_token="test-bootstrap-token-1234",
        database_path=":memory:",
        security_mode="token",
        log_level="WARNING",
    )
    application = FastAPI()
    application.include_router(router)
    application.state.config = config

    store = AIStore(config.database_path)
    await store.open()
    application.state.store = store

    import atlas_ai.engine as engine_module

    async def fake_discover(*, core_url, token=None, ssl_context=None,
                            name=None, capability=None, timeout=5.0):
        return world.services.get(name, [])

    monkeypatch.setattr(engine_module, "discover_service", fake_discover)

    engine = AIEngine(
        store, FakeAtlas(), StubBackend(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(world.handler)),
    )
    application.state.engine = engine
    application.state.introspector = FakeIntrospector()
    yield application
    await engine.close()
    await store.close()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://atlas-ai") as c:
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def ask(client, prompt: str, *, token=USER_TOKEN) -> dict:
    response = await client.post("/v1/ask", json={"prompt": prompt}, headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()
