# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Event Bus test fixtures.

The bus is tested against a real app with an in-memory store. Only the
introspector is replaced — Core is not required — with a fake that maps
known tokens to identities, exercising the same auth dependency chain.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from atlas_eventbus.api.routes import router
from atlas_eventbus.auth import Identity
from atlas_eventbus.bus import EventBus
from atlas_eventbus.config import BusConfig
from atlas_eventbus.store import BusStore

CORE_TOKEN = "tok-core"
ECHO_TOKEN = "tok-echo"
OTHER_TOKEN = "tok-other"

IDENTITIES = {
    CORE_TOKEN: Identity("atlas.core", "core-1", "0.2.0"),
    ECHO_TOKEN: Identity("atlas.echo", "echo-1", "0.2.0"),
    OTHER_TOKEN: Identity("atlas.other", "other-1", "0.1.0"),
}


class FakeIntrospector:
    """Maps fixed tokens to identities; everything else is rejected."""

    async def introspect(self, token: str) -> Identity | None:
        return IDENTITIES.get(token)

    async def close(self) -> None:
        pass


@pytest.fixture
def config() -> BusConfig:
    return BusConfig(
        bootstrap_token="test-bootstrap-token-1234",
        database_path=":memory:",
        visibility_timeout_seconds=1,
        max_wait_seconds=5,
        log_level="WARNING",
        security_mode="token",
    )


@pytest.fixture
async def app(config):
    application = FastAPI()
    application.include_router(router)
    application.state.config = config

    store = BusStore(config.database_path)
    await store.open()
    application.state.store = store

    bus = EventBus(store, config)
    application.state.bus = bus

    application.state.introspector = FakeIntrospector()
    yield application
    await store.close()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://atlas-eventbus") as c:
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def make_subscription(
    client, *, token: str = ECHO_TOKEN, name: str = "main", topics=None
) -> str:
    response = await client.post(
        "/v1/subscriptions",
        json={"name": name, "topics": topics or ["registry.*"]},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def publish(
    client, *, token: str = CORE_TOKEN, topic="registry.service.registered", payload=None
) -> dict:
    response = await client.post(
        "/v1/events",
        json={"topic": topic, "payload": payload or {"name": "atlas.echo"}},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()
