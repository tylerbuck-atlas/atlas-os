# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Device Manager test fixtures: fake adapter + fake memory over MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from atlas_devices.api.routes import router
from atlas_devices.config import DevicesConfig
from atlas_devices.service import DeviceService
from atlas_devices.store import DeviceStore
from atlas_sdk.service_auth import Identity

ADAPTER_TOKEN = "tok-adapter"
OTHER_ADAPTER_TOKEN = "tok-other-adapter"
PLANNER_TOKEN = "tok-planner"
SERVICE_TOKEN = "tok-service"
OPERATOR_TOKEN = "tok-operator"

IDENTITIES = {
    ADAPTER_TOKEN: Identity("atlas.adapter.virtual", "a-1", "0.1.0"),
    OTHER_ADAPTER_TOKEN: Identity("atlas.adapter.other", "b-1", "0.1.0"),
    PLANNER_TOKEN: Identity("atlas.planner", "p-1", "0.1.0"),
    SERVICE_TOKEN: Identity("atlas.random", "r-1", "0.1.0"),
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
    service_token = "tok-devices"

    def bus_credentials(self):
        return (self.service_token, None)

    def client_ssl_context(self):
        return None


class FakeWorld:
    """MockTransport backing: adapter endpoint + memory facts endpoint."""

    def __init__(self) -> None:
        self.adapter_result: object = {"result": {"applied": "x"}, "state": {"on": True}}
        self.adapter_calls: list[dict] = []
        self.memory_writes: list[dict] = []
        self.services = {
            "atlas.adapter.virtual": [{"name": "atlas.adapter.virtual",
                                       "status": "healthy",
                                       "address": "https://adapter.test"}],
            "atlas.memory": [{"name": "atlas.memory", "status": "healthy",
                              "address": "https://memory.test"}],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "adapter.test":
            self.adapter_calls.append(json.loads(request.content))
            if isinstance(self.adapter_result, int):
                return httpx.Response(self.adapter_result, text="adapter error")
            return httpx.Response(200, json=self.adapter_result)
        if request.url.host == "memory.test":
            self.memory_writes.append({
                "path": request.url.path, "body": json.loads(request.content),
            })
            return httpx.Response(201, json={"ok": True})
        return httpx.Response(404)


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld()


@pytest.fixture
async def app(world, monkeypatch):
    config = DevicesConfig(
        bootstrap_token="test-bootstrap-token-1234",
        database_path=":memory:",
        security_mode="token",
        log_level="WARNING",
    )
    application = FastAPI()
    application.include_router(router)
    application.state.config = config

    store = DeviceStore(config.database_path)
    await store.open()
    application.state.store = store

    import atlas_devices.service as service_module

    async def fake_discover(*, core_url, token=None, ssl_context=None,
                            name=None, capability=None, timeout=5.0):
        return world.services.get(name, [])

    monkeypatch.setattr(service_module, "discover_service", fake_discover)

    devices = DeviceService(
        store, FakeAtlas(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(world.handler)),
    )
    application.state.devices = devices
    application.state.introspector = FakeIntrospector()
    yield application
    await devices.close()
    await store.close()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://atlas-devices") as c:
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def sync(
    client, *, token=ADAPTER_TOKEN, adapter="atlas.adapter.virtual",
    native_id="virtual-light-1", name="Living room lamp", kind="light",
    data_class=1, commands=None, state=None,
):
    response = await client.put(
        f"/v1/adapters/{adapter}/devices/{native_id}",
        json={
            "name": name, "kind": kind, "room": "living-room",
            "class": data_class,
            "commands": commands if commands is not None else ["turn_on", "turn_off"],
            "state": state or {"on": False},
        },
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    return response.json()
