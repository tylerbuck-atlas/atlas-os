# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Test fixtures for Atlas Core.

Tests run against a real app instance with an in-memory database — no
mocks of Atlas's own components.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from atlas_core.config import CoreConfig
from atlas_core.main import create_app

BOOTSTRAP_TOKEN = "test-bootstrap-token-1234"


@pytest.fixture
def config() -> CoreConfig:
    return CoreConfig(
        bootstrap_token=BOOTSTRAP_TOKEN,
        database_path=":memory:",
        heartbeat_interval_seconds=1,
        heartbeat_misses_allowed=2,
        probe_interval_seconds=1,
        probe_timeout_seconds=0.5,
        log_level="WARNING",
    )


@pytest.fixture
async def app(config):
    application = create_app(config)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://atlas-core") as c:
        yield c


def bootstrap_headers() -> dict:
    return {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}


def token_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


REGISTRATION = {
    "name": "atlas.echo",
    "version": "0.1.0",
    "address": "http://atlas-echo:8100",
    "health_url": "http://atlas-echo:8100/healthz",
    "capabilities": ["echo.reply"],
    "metadata": {"description": "test"},
}


async def register(client, payload: dict | None = None) -> dict:
    response = await client.post(
        "/v1/registry/services",
        json=payload or REGISTRATION,
        headers=bootstrap_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()
