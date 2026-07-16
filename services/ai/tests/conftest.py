# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""AI service fixtures: a fake world (memory, devices, planner) that also
records every HTTP call the AI makes — so tests can prove what it never does."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from atlas_ai.api.routes import router
from atlas_ai.backends import BuiltinBackend
from atlas_ai.config import AIConfig
from atlas_ai.engine import AssistEngine
from atlas_ai.store import AIStore
from atlas_ai.truth import TruthGatherer
from atlas_sdk.service_auth import Identity

USER_TOKEN = "tok-user"
OPERATOR_TOKEN = "tok-operator"

IDENTITIES = {
    USER_TOKEN: Identity("atlas.ui", "ui-1", "0.1.0"),
    OPERATOR_TOKEN: Identity("atlas.operator", "manual-1", "cert"),
}

LIGHT_ID = "device-light-1"


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
    def __init__(self) -> None:
        self.calls: list[dict] = []          # every request the AI makes
        self.submitted_plans: list[dict] = []
        self.plan_response = {"id": "plan-1", "status": "awaiting_approval"}
        self.facts = {
            "system.services": [
                {"key": "atlas.echo", "version": 2,
                 "payload": {"status": "healthy"}, "provenance": "event:x", "class": 1},
            ],
            "home.devices": [],
        }
        self.devices = [
            {"id": LIGHT_ID, "name": "Living room lamp", "kind": "light",
             "class": 1, "commands": ["turn_on", "turn_off"],
             "state": {"on": False}, "room": "living-room"},
            {"id": "device-pres-1", "name": "Front hall presence", "kind": "sensor",
             "class": 3, "commands": [], "state": {"redacted": True}, "room": "hallway"},
        ]
        self.services = {
            "atlas.memory": [{"name": "atlas.memory", "status": "healthy",
                              "address": "https://memory.test"}],
            "atlas.devices": [{"name": "atlas.devices", "status": "healthy",
                               "address": "https://devices.test"}],
            "atlas.planner": [{"name": "atlas.planner", "status": "healthy",
                               "address": "https://planner.test"}],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        record = {
            "host": request.url.host, "path": request.url.path,
            "method": request.method, "params": dict(request.url.params),
        }
        self.calls.append(record)
        if request.url.host == "memory.test":
            namespace = request.url.path.split("/v1/facts/")[1]
            return httpx.Response(200, json=self.facts.get(namespace, []))
        if request.url.host == "devices.test":
            return httpx.Response(200, json=self.devices)
        if request.url.host == "planner.test" and request.url.path == "/v1/plans":
            self.submitted_plans.append(json.loads(request.content))
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
    import atlas_ai.truth as truth_module

    async def fake_discover(*, core_url, token=None, ssl_context=None,
                            name=None, capability=None, timeout=5.0):
        return world.services.get(name, [])

    monkeypatch.setattr(engine_module, "discover_service", fake_discover)
    monkeypatch.setattr(truth_module, "discover_service", fake_discover)

    transport = httpx.MockTransport(world.handler)
    atlas = FakeAtlas()
    gatherer = TruthGatherer(
        atlas, fact_namespaces=["system.services", "home.devices"],
        client=httpx.AsyncClient(transport=transport),
    )
    engine = AssistEngine(
        store, atlas, gatherer, BuiltinBackend(),
        client=httpx.AsyncClient(transport=transport),
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


async def assist(client, prompt, *, token=USER_TOKEN):
    response = await client.post(
        "/v1/assist", json={"prompt": prompt}, headers=auth(token)
    )
    assert response.status_code == 200, response.text
    return response.json()
