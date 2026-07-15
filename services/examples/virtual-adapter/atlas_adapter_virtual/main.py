# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""atlas.adapter.virtual — reference implementation of the adapter contract.

An adapter (docs/devices-skills.md):
1. registers with Core like any service;
2. syncs its devices to the Device Manager
   (``PUT /v1/adapters/{self}/devices/{native_id}``);
3. executes commands at ``POST /v1/invoke/adapter.command``
   (called by the Device Manager, which itself only accepts commands
   from the Planner);
4. reports state changes by re-syncing.

This one simulates: a light, a drifting temperature sensor, and a
Class-3 presence sensor — enough to exercise every path, including the
privacy redaction, with zero hardware.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import ssl
import sys
from contextlib import asynccontextmanager

import httpx
from atlas_sdk import AtlasService, discover_service
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import SERVICE_NAME, __version__

log = logging.getLogger("atlas.adapter.virtual")

CORE_URL = os.environ.get("ATLAS_CORE_URL", "https://atlas-core:8000")
BOOTSTRAP_TOKEN = os.environ.get("ATLAS_BOOTSTRAP_TOKEN", "")
SELF_URL = os.environ.get("ATLAS_ADAPTER_URL", "https://atlas-adapter-virtual:8900")
PORT = int(os.environ.get("ATLAS_ADAPTER_PORT", "8900"))
SECURITY_MODE = os.environ.get("ATLAS_SECURITY_MODE", "mtls").lower()
TLS_DIR = os.environ.get("ATLAS_ADAPTER_TLS_DIR", "data/tls")
CA_CERT = os.environ.get("ATLAS_CA_CERT")
DRIFT_SECONDS = float(os.environ.get("ATLAS_ADAPTER_DRIFT_SECONDS", "15"))

#: The simulated household.
DEVICES: dict[str, dict] = {
    "virtual-light-1": {
        "descriptor": {
            "name": "Living room lamp", "kind": "light", "room": "living-room",
            "class": 1, "commands": ["turn_on", "turn_off", "set_brightness"],
        },
        "state": {"on": False, "brightness": 100},
    },
    "virtual-temp-1": {
        "descriptor": {
            "name": "Hallway temperature", "kind": "sensor", "room": "hallway",
            "class": 1, "commands": [],
        },
        "state": {"temp_c": 21.0},
    },
    "virtual-presence-1": {
        "descriptor": {
            "name": "Front hall presence", "kind": "sensor", "room": "hallway",
            "class": 3, "commands": [],
        },
        "state": {"present": False},
    },
}


class AdapterCommand(BaseModel):
    native_id: str
    command: str
    params: dict = {}


class VirtualAdapter:
    def __init__(self, atlas: AtlasService) -> None:
        self._atlas = atlas
        self._client: httpx.AsyncClient | None = None
        self._drift_task: asyncio.Task | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            ctx = self._atlas.client_ssl_context()
            self._client = httpx.AsyncClient(
                timeout=10.0,
                verify=ctx if ctx is not None else True,
                headers=(
                    {}
                    if self._atlas.security_mode == "mtls"
                    else {"Authorization": f"Bearer {self._atlas.service_token or ''}"}
                ),
            )
        return self._client

    async def _device_manager(self) -> str | None:
        token, ssl_ctx = self._atlas.bus_credentials()
        try:
            services = await discover_service(
                core_url=self._atlas.core_url, token=token, ssl_context=ssl_ctx,
                name="atlas.devices",
            )
        except (httpx.HTTPError, ssl.SSLError):
            return None
        live = [s for s in services if s.get("status") in ("starting", "healthy")]
        return live[0]["address"] if live else None

    async def sync_device(self, native_id: str) -> bool:
        manager = await self._device_manager()
        if manager is None:
            return False
        device = DEVICES[native_id]
        try:
            response = await self._http().put(
                f"{manager.rstrip('/')}/v1/adapters/{SERVICE_NAME}/devices/{native_id}",
                json={**device["descriptor"], "state": device["state"], "online": True},
            )
            return response.status_code < 300
        except (httpx.HTTPError, ssl.SSLError) as exc:
            log.warning("sync failed for %s: %s", native_id, exc)
            return False

    async def sync_all_with_retry(self) -> None:
        delay = 1.0
        remaining = set(DEVICES)
        while remaining:
            done = {nid for nid in remaining if await self.sync_device(nid)}
            remaining -= done
            if remaining:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)
        log.info("all %d virtual devices synced", len(DEVICES))

    def apply_command(self, request: AdapterCommand) -> dict:
        """Apply a command to the simulated device; returns new state."""
        device = DEVICES.get(request.native_id)
        if device is None:
            raise HTTPException(status_code=404, detail="unknown native device")
        state = device["state"]
        if request.command == "turn_on":
            state["on"] = True
        elif request.command == "turn_off":
            state["on"] = False
        elif request.command == "set_brightness":
            level = int(request.params.get("level", 100))
            state["brightness"] = max(1, min(100, level))
            state["on"] = True
        else:
            raise HTTPException(status_code=422, detail=f"unsupported command {request.command!r}")
        log.info("%s <- %s%s => %s", request.native_id, request.command, request.params, state)
        return state

    async def start_drift(self) -> None:
        self._drift_task = asyncio.create_task(self._drift_loop(), name="virtual-drift")

    async def stop(self) -> None:
        if self._drift_task:
            self._drift_task.cancel()
            try:
                await self._drift_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()

    async def _drift_loop(self) -> None:
        """The simulated world changes on its own — like a real one."""
        while True:
            await asyncio.sleep(DRIFT_SECONDS)
            temp = DEVICES["virtual-temp-1"]["state"]
            temp["temp_c"] = round(temp["temp_c"] + random.uniform(-0.4, 0.4), 1)
            presence = DEVICES["virtual-presence-1"]["state"]
            if random.random() < 0.3:
                presence["present"] = not presence["present"]
            await self.sync_device("virtual-temp-1")
            await self.sync_device("virtual-presence-1")


def build_atlas_service() -> AtlasService:
    return AtlasService(
        name=SERVICE_NAME,
        version=__version__,
        address=SELF_URL,
        health_url=f"{SELF_URL}/healthz",
        capabilities=["adapter.command"],
        metadata={"description": "Reference (virtual) device adapter"},
        core_url=CORE_URL,
        bootstrap_token=BOOTSTRAP_TOKEN,
        security_mode=SECURITY_MODE,
        tls_dir=TLS_DIR if SECURITY_MODE == "mtls" else None,
        ca_cert_file=CA_CERT,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if not BOOTSTRAP_TOKEN:
        print("FATAL: ATLAS_BOOTSTRAP_TOKEN is required", file=sys.stderr)
        raise SystemExit(1)
    atlas: AtlasService = app.state.atlas
    if SECURITY_MODE == "mtls":
        atlas.start_background()
    else:
        await atlas.start()
    adapter = VirtualAdapter(atlas)
    app.state.adapter = adapter
    sync_task = asyncio.create_task(adapter.sync_all_with_retry())
    await adapter.start_drift()
    try:
        yield
    finally:
        sync_task.cancel()
        await adapter.stop()
        await atlas.stop()


def create_app(atlas: AtlasService | None = None) -> FastAPI:
    app = FastAPI(title=SERVICE_NAME, version=__version__, lifespan=_lifespan)
    app.state.atlas = atlas or build_atlas_service()

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": SERVICE_NAME, "version": __version__}

    @app.post("/v1/invoke/adapter.command")
    async def invoke_command(request: AdapterCommand) -> dict:
        """The adapter contract: the Device Manager calls here.
        (Which itself only accepts commands from the Planner.)"""
        state = app.state.adapter.apply_command(request)
        return {"result": {"applied": request.command}, "state": state}

    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )
    atlas = build_atlas_service()
    app = create_app(atlas)

    kwargs: dict = {}
    if SECURITY_MODE == "mtls":
        asyncio.run(atlas.enroll())
        assert atlas.tls is not None
        kwargs = atlas.tls.uvicorn_kwargs()

    server_config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_config=None, **kwargs)
    server = uvicorn.Server(server_config)

    if SECURITY_MODE == "mtls":
        tls = atlas.tls

        def _reload_tls() -> None:
            if server_config.ssl is not None:
                server_config.ssl.load_cert_chain(str(tls.cert_path), str(tls.key_path))

        atlas.on_credentials_rotated = _reload_tls

    server.run()


if __name__ == "__main__":
    run()
