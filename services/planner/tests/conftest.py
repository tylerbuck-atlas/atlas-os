# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Planner test fixtures.

The engine gets a stub AtlasService (discovery + execution HTTP both
faked via httpx.MockTransport) so plan execution is tested end to end
without sockets.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from atlas_planner.api.routes import router
from atlas_planner.config import PlannerConfig
from atlas_planner.engine import PlannerEngine
from atlas_planner.store import PlannerStore
from atlas_sdk.service_auth import Identity

AI_TOKEN = "tok-ai"
OTHER_TOKEN = "tok-other"
OPERATOR_TOKEN = "tok-operator"

IDENTITIES = {
    AI_TOKEN: Identity("atlas.ai", "ai-1", "0.1.0"),
    OTHER_TOKEN: Identity("atlas.other", "other-1", "0.1.0"),
    OPERATOR_TOKEN: Identity("atlas.operator", "manual-1", "cert"),
}


class FakeIntrospector:
    async def introspect(self, token: str) -> Identity | None:
        return IDENTITIES.get(token)

    async def close(self) -> None:
        pass


class FakeAtlas:
    """Stub of atlas_sdk.AtlasService: token-mode credentials."""

    core_url = "https://core.test"
    security_mode = "token"
    service_token = "tok-planner"

    def bus_credentials(self):
        return (self.service_token, None)

    def client_ssl_context(self):
        return None


class FakeWorld:
    """One MockTransport playing Core's registry AND the target services."""

    def __init__(self) -> None:
        #: capability -> list of service records Core "returns"
        self.services: dict[str, list[dict]] = {
            "echo.reply": [{
                "name": "atlas.echo", "status": "healthy",
                "address": "https://echo.test",
            }],
        }
        #: (host, capability) -> handler result: dict | int (status code)
        self.invoke_results: dict[str, object] = {"echo.reply": {"reply": "ok"}}
        self.invocations: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "core.test":
            capability = request.url.params.get("capability")
            return httpx.Response(200, json=self.services.get(capability, []))
        if "/v1/invoke/" in request.url.path:
            capability = request.url.path.split("/v1/invoke/")[1]
            self.invocations.append({
                "host": request.url.host,
                "capability": capability,
                "params": json.loads(request.content or b"{}"),
            })
            result = self.invoke_results.get(capability, 404)
            if isinstance(result, int):
                return httpx.Response(result, text="invocation error")
            return httpx.Response(200, json=result)
        return httpx.Response(404)


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld()


@pytest.fixture
async def app(world, monkeypatch):
    config = PlannerConfig(
        bootstrap_token="test-bootstrap-token-1234",
        database_path=":memory:",
        security_mode="token",
        log_level="WARNING",
    )
    application = FastAPI()
    application.include_router(router)
    application.state.config = config

    store = PlannerStore(config.database_path)
    await store.open()
    application.state.store = store

    transport = httpx.MockTransport(world.handler)
    engine = PlannerEngine(
        store, FakeAtlas(), client=httpx.AsyncClient(transport=transport)
    )
    # discovery goes through atlas_sdk.discover_service — route it through
    # the same fake world.
    import atlas_planner.engine as engine_module

    async def fake_discover(*, core_url, token=None, ssl_context=None,
                            name=None, capability=None, timeout=5.0):
        return world.services.get(capability, [])

    monkeypatch.setattr(engine_module, "discover_service", fake_discover)
    application.state.engine = engine
    application.state.introspector = FakeIntrospector()
    yield application
    await engine.close()
    await store.close()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://atlas-planner") as c:
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def add_policy(
    client, *, requester="atlas.ai", capability="echo.reply",
    effect="allow", priority=100,
):
    response = await client.post(
        "/v1/policies",
        json={"requester": requester, "capability": capability,
              "effect": effect, "priority": priority},
        headers=auth(OPERATOR_TOKEN),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def submit(client, *, token=AI_TOKEN, capability="echo.reply", params=None):
    response = await client.post(
        "/v1/plans",
        json={"goal": "test goal",
              "actions": [{"capability": capability, "params": params or {"message": "hi"}}]},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def wait_for_status(client, plan_id, statuses, *, token=OPERATOR_TOKEN, tries=50):
    import asyncio

    for _ in range(tries):
        response = await client.get(f"/v1/plans/{plan_id}", headers=auth(token))
        plan = response.json()
        if plan["status"] in statuses:
            return plan
        await asyncio.sleep(0.05)
    raise AssertionError(f"plan never reached {statuses}: {plan['status']}")
