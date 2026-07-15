# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""atlas.echo — reference implementation of the Atlas service contract.

Registration, heartbeats, and clean shutdown are handled by the Atlas SDK
(:class:`atlas_sdk.AtlasService`); this file is what a minimal Atlas
service actually has to write.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from atlas_sdk import AtlasService
from fastapi import FastAPI
from pydantic import BaseModel

from . import SERVICE_NAME, __version__

log = logging.getLogger("atlas.echo")

CORE_URL = os.environ.get("ATLAS_CORE_URL", "http://atlas-core:8000")
BOOTSTRAP_TOKEN = os.environ.get("ATLAS_BOOTSTRAP_TOKEN", "")
SELF_URL = os.environ.get("ATLAS_ECHO_URL", "http://atlas-echo:8100")
PORT = int(os.environ.get("ATLAS_ECHO_PORT", "8100"))


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
    atlas = AtlasService(
        name=SERVICE_NAME,
        version=__version__,
        address=SELF_URL,
        health_url=f"{SELF_URL}/healthz",
        capabilities=["echo.reply"],
        metadata={"description": "Reference Atlas service"},
        core_url=CORE_URL,
        bootstrap_token=BOOTSTRAP_TOKEN,
    )
    app.state.atlas = atlas
    await atlas.start()
    try:
        yield
    finally:
        await atlas.stop()


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
