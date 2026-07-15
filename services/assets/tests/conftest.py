# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Asset Manager test fixtures."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from atlas_assets.api.routes import router
from atlas_assets.config import AssetsConfig
from atlas_assets.store import AssetStore
from atlas_sdk.service_auth import Identity

UPLOADER_TOKEN = "tok-uploader"
OTHER_TOKEN = "tok-other"
OPERATOR_TOKEN = "tok-operator"

IDENTITIES = {
    UPLOADER_TOKEN: Identity("atlas.ingestor", "ing-1", "0.1.0"),
    OTHER_TOKEN: Identity("atlas.other", "other-1", "0.1.0"),
    OPERATOR_TOKEN: Identity("atlas.operator", "manual-1", "cert"),
}


class FakeIntrospector:
    async def introspect(self, token: str) -> Identity | None:
        return IDENTITIES.get(token)

    async def close(self) -> None:
        pass


@pytest.fixture
async def app(tmp_path):
    config = AssetsConfig(
        bootstrap_token="test-bootstrap-token-1234",
        database_path=":memory:",
        blob_dir=str(tmp_path / "blobs"),
        security_mode="token",
        max_upload_bytes=1024 * 1024,
        log_level="WARNING",
    )
    application = FastAPI()
    application.include_router(router)
    application.state.config = config
    store = AssetStore(config.database_path, config.blob_dir)
    await store.open()
    application.state.store = store
    application.state.introspector = FakeIntrospector()
    yield application
    await store.close()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://atlas-assets") as c:
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def upload(
    client, *, token=UPLOADER_TOKEN, content=b"MANUAL PDF BYTES",
    name="dishwasher-manual.pdf", kind="manual", data_class=0,
    tags="kitchen,appliance", metadata='{"brand": "Bosch"}',
):
    response = await client.post(
        "/v1/assets",
        files={"file": (name, content, "application/pdf")},
        data={
            "name": name, "kind": kind, "class": str(data_class),
            "tags": tags, "metadata": metadata,
        },
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()
