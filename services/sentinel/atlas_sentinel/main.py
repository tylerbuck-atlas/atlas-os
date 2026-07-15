# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Atlas Sentinel application factory and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from atlas_sdk import AtlasService, BusOutbox, CoreIntrospector
from fastapi import FastAPI

from . import SERVICE_NAME, __version__
from .api.routes import router
from .config import SentinelConfig
from .rules import RuleEngine
from .watcher import Watcher
from .store import SentinelStore

log = logging.getLogger("atlas.sentinel")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )


def build_atlas_service(config: SentinelConfig) -> AtlasService:
    return AtlasService(
        name=SERVICE_NAME,
        version=__version__,
        address=config.self_url,
        health_url=f"{config.self_url}/healthz",
        capabilities=["sentinel.alerts"],
        metadata={"description": "Atlas Sentinel — security monitoring and anomaly detection"},
        core_url=config.core_url,
        bootstrap_token=config.bootstrap_token,
        security_mode=config.security_mode,
        tls_dir=config.tls_dir if config.security_mode == "mtls" else None,
        ca_cert_file=config.ca_cert_file,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config: SentinelConfig = app.state.config

    store = SentinelStore(config.database_path)
    await store.open()
    app.state.store = store
    log.info("sentinel store initialized (db=%s)", config.database_path)

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

    rules = RuleEngine(
        flap_threshold=config.flap_threshold,
        flap_window_seconds=config.flap_window_seconds,
        rejection_threshold=config.rejection_threshold,
        rejection_window_seconds=config.rejection_window_seconds,
        cooldown_seconds=config.alert_cooldown_seconds,
    )
    watcher = Watcher(store, rules, atlas)
    await watcher.start()
    app.state.watcher = watcher

    log.info("Atlas Sentinel ready (%s v%s) — watching the event stream", SERVICE_NAME, __version__)
    try:
        yield
    finally:
        log.info("Atlas Sentinel shutting down")
        await watcher.stop()
        await outbox.stop()
        await atlas.stop()
        if app.state.introspector is not None:
            await app.state.introspector.close()
        await store.close()


def create_app(
    config: SentinelConfig | None = None, atlas: AtlasService | None = None
) -> FastAPI:
    if config is None:
        config = SentinelConfig()
    _configure_logging(config.log_level)
    app = FastAPI(
        title="Atlas Sentinel",
        version=__version__,
        description="Security monitoring and anomaly detection for Atlas OS.",
        lifespan=_lifespan,
    )
    app.state.config = config
    app.state.atlas = atlas or build_atlas_service(config)
    app.include_router(router)
    return app


def run() -> None:
    import uvicorn

    try:
        config = SentinelConfig()
    except Exception as exc:
        print(f"FATAL: Atlas Sentinel refused to boot: {exc}", file=sys.stderr)
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
