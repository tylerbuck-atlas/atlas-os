# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Atlas Event Bus application factory and lifecycle.

The bus is an Atlas service like any other. In mtls mode it enrolls with
Core *before* it starts listening (the listener needs its certificate),
then serves mutual TLS; caller identity comes from verified peer
certificates. In token (development) mode it behaves exactly as in
Milestone 2, introspecting bearer tokens against Core.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from atlas_sdk import AtlasService
from fastapi import FastAPI

from . import SERVICE_NAME, __version__
from .api.routes import router
from .auth import CoreIntrospector
from .bus import EventBus
from .config import BusConfig
from .store import BusStore

log = logging.getLogger("atlas.eventbus")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )


def build_atlas_service(config: BusConfig) -> AtlasService:
    return AtlasService(
        name=SERVICE_NAME,
        version=__version__,
        address=config.self_url,
        health_url=f"{config.self_url}/healthz",
        capabilities=["eventbus.publish", "eventbus.subscribe", "eventbus.schemas"],
        metadata={"description": "Atlas Event Bus — durable at-least-once pub/sub"},
        core_url=config.core_url,
        bootstrap_token=config.bootstrap_token,
        security_mode=config.security_mode,
        tls_dir=config.tls_dir if config.security_mode == "mtls" else None,
        ca_cert_file=config.ca_cert_file,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config: BusConfig = app.state.config

    store = BusStore(config.database_path)
    await store.open()
    app.state.store = store
    log.info("bus store initialized (db=%s)", config.database_path)

    bus = EventBus(store, config)
    await bus.start()
    app.state.bus = bus

    atlas: AtlasService = app.state.atlas
    if config.security_mode == "mtls":
        # Enrollment already happened in run() (pre-listener); here we
        # only start heartbeats + certificate rotation.
        atlas.start_background()
        app.state.introspector = None
        log.info("mtls mode: identity from peer certificates; tokens retired")
    else:
        await atlas.start()
        app.state.introspector = CoreIntrospector(
            core_url=config.core_url,
            own_token_provider=lambda: atlas.service_token,
            cache_ttl=config.introspect_cache_ttl_seconds,
        )
        log.warning("token security mode — development only")

    log.info("Atlas Event Bus ready (%s v%s)", SERVICE_NAME, __version__)
    try:
        yield
    finally:
        log.info("Atlas Event Bus shutting down")
        await atlas.stop()
        if app.state.introspector is not None:
            await app.state.introspector.close()
        await bus.stop()
        await store.close()


def create_app(config: BusConfig | None = None, atlas: AtlasService | None = None) -> FastAPI:
    if config is None:
        config = BusConfig()
    _configure_logging(config.log_level)
    app = FastAPI(
        title="Atlas Event Bus",
        version=__version__,
        description="Durable, at-least-once inter-service messaging for Atlas OS.",
        lifespan=_lifespan,
    )
    app.state.config = config
    app.state.atlas = atlas or build_atlas_service(config)
    app.include_router(router)
    return app


def run() -> None:
    import uvicorn

    try:
        config = BusConfig()
    except Exception as exc:
        print(f"FATAL: Atlas Event Bus refused to boot: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    atlas = build_atlas_service(config)
    app = create_app(config, atlas)

    kwargs: dict = {}
    if config.security_mode == "mtls":
        # Enroll before listening: the TLS listener needs the certificate.
        asyncio.run(atlas.enroll())
        assert atlas.tls is not None
        kwargs = atlas.tls.uvicorn_kwargs()

    server_config = uvicorn.Config(
        app, host=config.host, port=config.port, log_config=None, **kwargs
    )
    server = uvicorn.Server(server_config)

    if config.security_mode == "mtls":
        tls = atlas.tls

        def _reload_tls() -> None:
            if server_config.ssl is not None:
                server_config.ssl.load_cert_chain(str(tls.cert_path), str(tls.key_path))

        atlas.on_credentials_rotated = _reload_tls

    server.run()


if __name__ == "__main__":
    run()
