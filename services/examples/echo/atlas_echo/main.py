# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""atlas.echo — reference implementation of the Atlas service contract.

Enrollment, mutual TLS, heartbeats, certificate rotation, and clean
shutdown are all handled by the Atlas SDK; this file is what a minimal
Atlas service actually has to write.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from atlas_sdk import AtlasService
from fastapi import FastAPI
from pydantic import BaseModel

from . import SERVICE_NAME, __version__

log = logging.getLogger("atlas.echo")

CORE_URL = os.environ.get("ATLAS_CORE_URL", "https://atlas-core:8000")
BOOTSTRAP_TOKEN = os.environ.get("ATLAS_BOOTSTRAP_TOKEN", "")
SELF_URL = os.environ.get("ATLAS_ECHO_URL", "https://atlas-echo:8100")
PORT = int(os.environ.get("ATLAS_ECHO_PORT", "8100"))
SECURITY_MODE = os.environ.get("ATLAS_SECURITY_MODE", "mtls").lower()
TLS_DIR = os.environ.get("ATLAS_ECHO_TLS_DIR", "data/tls")
CA_CERT = os.environ.get("ATLAS_CA_CERT")


def build_atlas_service() -> AtlasService:
    return AtlasService(
        name=SERVICE_NAME,
        version=__version__,
        address=SELF_URL,
        health_url=f"{SELF_URL}/healthz",
        capabilities=["echo.reply"],
        metadata={"description": "Reference Atlas service"},
        core_url=CORE_URL,
        bootstrap_token=BOOTSTRAP_TOKEN,
        security_mode=SECURITY_MODE,
        tls_dir=TLS_DIR if SECURITY_MODE == "mtls" else None,
        ca_cert_file=CA_CERT,
    )


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
    atlas: AtlasService = app.state.atlas
    if SECURITY_MODE == "mtls":
        atlas.start_background()  # enrolled pre-listener in run()
    else:
        await atlas.start()
    try:
        yield
    finally:
        await atlas.stop()


class EchoRequest(BaseModel):
    message: str


def create_app(atlas: AtlasService | None = None) -> FastAPI:
    app = FastAPI(title="atlas.echo", version=__version__, lifespan=_lifespan)
    app.state.atlas = atlas or build_atlas_service()

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": SERVICE_NAME, "version": __version__}

    @app.post("/v1/echo")
    async def echo(payload: EchoRequest) -> dict:
        """The capability this service publishes: echo.reply."""
        return {"reply": payload.message, "service": SERVICE_NAME}

    @app.post("/v1/invoke/echo.reply")
    async def invoke_echo(payload: EchoRequest) -> dict:
        """Uniform capability invocation endpoint (docs/service-contract.md §5a):
        this is how the Planner executes plan steps against this service."""
        return {"reply": payload.message, "service": SERVICE_NAME}

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
