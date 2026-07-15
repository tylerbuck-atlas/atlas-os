# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Memory test fixtures: real app + in-memory store; fake introspector."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from atlas_memory.api.routes import router
from atlas_memory.config import MemoryConfig
from atlas_memory.service import MemoryService
from atlas_memory.store import MemoryStore
from atlas_sdk.service_auth import Identity

SENSOR_TOKEN = "tok-sensor"
OTHER_TOKEN = "tok-other"
OPERATOR_TOKEN = "tok-operator"

IDENTITIES = {
    SENSOR_TOKEN: Identity("atlas.sensor", "sensor-1", "0.1.0"),
    OTHER_TOKEN: Identity("atlas.other", "other-1", "0.1.0"),
    OPERATOR_TOKEN: Identity("atlas.operator", "manual-1", "cert"),
}


class FakeIntrospector:
    async def introspect(self, token: str) -> Identity | None:
        return IDENTITIES.get(token)

    async def close(self) -> None:
        pass


@pytest.fixture
async def app():
    config = MemoryConfig(
        bootstrap_token="test-bootstrap-token-1234",
        database_path=":memory:",
        security_mode="token",
        log_level="WARNING",
    )
    application = FastAPI()
    application.include_router(router)
    application.state.config = config
    store = MemoryStore(config.database_path)
    await store.open()
    application.state.store = store
    application.state.memory = MemoryService(store)
    application.state.introspector = FakeIntrospector()
    yield application
    await store.close()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://atlas-memory") as c:
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def put_fact(
    client, namespace="home.rooms", key="kitchen", *, token=SENSOR_TOKEN,
    payload=None, data_class=1, provenance="test", owner=None,
):
    response = await client.put(
        f"/v1/facts/{namespace}/{key}",
        json={
            "payload": payload or {"temp_c": 21},
            "class": data_class,
            "provenance": provenance,
            "owner": owner,
        },
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()
