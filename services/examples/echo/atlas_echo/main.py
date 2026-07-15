"""atlas.echo — reference implementation of the Atlas service contract.

Lifecycle:
1. serve /healthz
2. register with Atlas Core (retrying until Core is ready)
3. heartbeat on the interval Core returned
4. deregister on clean shutdown
5. if Core reports our token gone/superseded (401/410), re-register
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from . import SERVICE_NAME, __version__

log = logging.getLogger("atlas.echo")

CORE_URL = os.environ.get("ATLAS_CORE_URL", "http://atlas-core:8000")
BOOTSTRAP_TOKEN = os.environ.get("ATLAS_BOOTSTRAP_TOKEN", "")
SELF_URL = os.environ.get("ATLAS_ECHO_URL", "http://atlas-echo:8100")
PORT = int(os.environ.get("ATLAS_ECHO_PORT", "8100"))


class AtlasClient:
    """Registration + heartbeat client. Future milestones lift this into an SDK."""

    def __init__(self) -> None:
        self.instance_id: str | None = None
        self._token: str | None = None
        self._interval: int = 10
        self._task: asyncio.Task | None = None
        self._client = httpx.AsyncClient(timeout=5.0)

    async def start(self) -> None:
        await self._register_with_retry()
        self._task = asyncio.create_task(self._heartbeat_loop(), name="atlas-echo-heartbeat")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.instance_id and self._token:
            try:
                await self._client.delete(
                    f"{CORE_URL}/v1/registry/services/{self.instance_id}",
                    headers={"Authorization": f"Bearer {self._token}"},
                )
                log.info("deregistered from Atlas Core")
            except httpx.HTTPError:
                log.warning("could not deregister cleanly; Core's monitor will notice")
        await self._client.aclose()

    async def _register_with_retry(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._register()
                return
            except (httpx.HTTPError, KeyError) as exc:
                log.info("Core not ready (%s); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)

    async def _register(self) -> None:
        response = await self._client.post(
            f"{CORE_URL}/v1/registry/services",
            headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
            json={
                "name": SERVICE_NAME,
                "version": __version__,
                "address": SELF_URL,
                "health_url": f"{SELF_URL}/healthz",
                "capabilities": ["echo.reply"],
                "metadata": {"description": "Reference Atlas service"},
            },
        )
        response.raise_for_status()
        body = response.json()
        self.instance_id = body["service"]["instance_id"]
        self._token = body["service_token"]
        self._interval = body["heartbeat_interval_seconds"]
        log.info(
            "registered with Atlas Core as %s (interval %ss)", self.instance_id, self._interval
        )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                response = await self._client.post(
                    f"{CORE_URL}/v1/registry/services/{self.instance_id}/heartbeat",
                    headers={"Authorization": f"Bearer {self._token}"},
                )
                if response.status_code in (401, 410):
                    log.warning("token invalidated; re-registering")
                    await self._register_with_retry()
            except httpx.HTTPError as exc:
                log.warning("heartbeat failed: %s", exc)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )
    if not BOOTSTRAP_TOKEN:
        print("FATAL: ATLAS_BOOTSTRAP_TOKEN is required", file=sys.stderr)
        raise SystemExit(1)
    client = AtlasClient()
    app.state.atlas = client
    await client.start()
    try:
        yield
    finally:
        await client.stop()


app = FastAPI(title="atlas.echo", version=__version__, lifespan=_lifespan)


class EchoRequest(BaseModel):
    message: str


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@app.post("/v1/echo")
async def echo(payload: EchoRequest) -> dict:
    """The capability this service publishes: echo.reply."""
    return {"reply": payload.message, "service": SERVICE_NAME}


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)


if __name__ == "__main__":
    run()
