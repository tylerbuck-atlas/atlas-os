# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Atlas Device Manager application factory and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from atlas_sdk import AtlasService, BusOutbox, CoreIntrospector
from fastapi import FastAPI

from . import SERVICE_NAME, __version__
from .api.routes import router
from .config import DevicesConfig
from .service import DeviceService
from .store import DeviceStore

log = logging.getLogger("atlas.devices")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )


def build_atlas_service(config: DevicesConfig) -> AtlasService:
    return AtlasService(
        name=SERVICE_NAME,
        version=__version__,
        address=config.self_url,
        health_url=f"{config.self_url}/healthz",
        capabilities=["devices.command", "devices.read", "devices.adapter-sync"],
        metadata={"description": "Atlas Device Manager — the home devices behind one abstraction"},
        core_url=config.core_url,
        bootstrap_token=config.bootstrap_token,
        security_mode=config.security_mode,
        tls_dir=config.tls_dir if config.security_mode == "mtls" else None,
        ca_cert_file=config.ca_cert_file,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config: DevicesConfig = app.state.config

    store = DeviceStore(config.database_path)
    await store.open()
    app.state.store = store
    log.info("device store initialized (db=%s)", config.database_path)

    atlas: AtlasService = app.state.atlas
    if config.security_mode == "mtls":
        atlas.start_background()
        app.state.introspector = None
    else:
        await atlas.start()
        app.state.introspector = CoreIntrospector(
            core_url=config.core_url,
            own_token_provider=lambda: atlas.service_token,
            cache_ttl=config.introspect_cache_ttl_seconds,
        )
        log.warning("token security mode — development only")

    outbox = BusOutbox(
        core_url=config.core_url,
        credentials=atlas.bus_credentials,
        list_events_after=store.list_events_after,
        get_cursor=store.get_cursor,
        set_cursor=store.set_cursor,
    )
    await outbox.start()

    devices = DeviceService(store, atlas, command_timeout=config.command_timeout_seconds)
    app.state.devices = devices

    log.info("Atlas Device Manager ready (%s v%s)", SERVICE_NAME, __version__)
    try:
        yield
    finally:
        log.info("Atlas Device Manager shutting down")
        await devices.close()
        await outbox.stop()
        await atlas.stop()
        if app.state.introspector is not None:
            await app.state.introspector.close()
        await store.close()


def create_app(
    config: DevicesConfig | None = None, atlas: AtlasService | None = None
) -> FastAPI:
    if config is None:
        config = DevicesConfig()
    _configure_logging(config.log_level)
    app = FastAPI(
        title="Atlas Device Manager",
        version=__version__,
        description="The home devices behind one abstraction; adapters own protocols.",
        lifespan=_lifespan,
    )
    app.state.config = config
    app.state.atlas = atlas or build_atlas_service(config)
    app.include_router(router)
    return app


def run() -> None:
    import uvicorn

    try:
        config = DevicesConfig()
    except Exception as exc:
        print(f"FATAL: Atlas Device Manager refused to boot: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    atlas = build_atlas_service(config)
    app = create_app(config, atlas)

    kwargs: dict = {}
    if config.security_mode == "mtls":
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
