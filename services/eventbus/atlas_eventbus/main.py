"""Atlas Event Bus application factory and lifecycle.

The bus is an Atlas service like any other: it validates its config,
opens its store, starts serving, registers itself with Atlas Core (via
the SDK, retrying until Core is up), and heartbeats. Core's outbox
publisher discovers it through the registry and begins streaming events.
"""

from __future__ import annotations

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

    atlas = AtlasService(
        name=SERVICE_NAME,
        version=__version__,
        address=config.self_url,
        health_url=f"{config.self_url}/healthz",
        capabilities=["eventbus.publish", "eventbus.subscribe", "eventbus.schemas"],
        metadata={"description": "Atlas Event Bus — durable at-least-once pub/sub"},
        core_url=config.core_url,
        bootstrap_token=config.bootstrap_token,
    )
    app.state.atlas = atlas
    await atlas.start()

    app.state.introspector = CoreIntrospector(
        core_url=config.core_url,
        own_token_provider=lambda: atlas.service_token,
        cache_ttl=config.introspect_cache_ttl_seconds,
    )

    log.info("Atlas Event Bus ready (%s v%s)", SERVICE_NAME, __version__)
    try:
        yield
    finally:
        log.info("Atlas Event Bus shutting down")
        await atlas.stop()
        await app.state.introspector.close()
        await bus.stop()
        await store.close()


def create_app(config: BusConfig | None = None) -> FastAPI:
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
    app.include_router(router)
    return app


def run() -> None:
    import uvicorn

    try:
        config = BusConfig()
    except Exception as exc:
        print(f"FATAL: Atlas Event Bus refused to boot: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    uvicorn.run(
        create_app(config), host=config.host, port=config.port, log_config=None
    )


if __name__ == "__main__":
    run()
