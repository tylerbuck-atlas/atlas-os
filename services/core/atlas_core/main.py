"""Atlas Core application factory and boot sequence.

Boot is explicit, ordered, and observable (docs/architecture.md §3):

    1. CONFIG    2. IDENTITY    3. REGISTRY    4. AUTH
    5. PLUGINS   6. HEALTH      7. API         8. READY

Any stage failing aborts boot — there is no half-booted mode. Stage 8
logs exactly: ``Atlas Ready.``
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import SERVICE_NAME, __version__
from .api import registry as registry_api
from .api import system as system_api
from .config import CoreConfig
from .health import HealthMonitor
from .plugins import PluginManager
from .registry import ServiceRegistry
from .store import RegistryStore

log = logging.getLogger("atlas.core")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )


def _stage(app: FastAPI, number: int, name: str) -> None:
    app.state.boot_stage = f"{number}/{8} {name}"
    log.info("boot stage %d/8: %s", number, name)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config: CoreConfig = app.state.config

    # Stages 1-2 (CONFIG, IDENTITY) completed in create_app(); recorded here
    # so the boot log reads as one sequence.
    _stage(app, 1, "CONFIG")
    log.info("configuration loaded and validated")
    _stage(app, 2, "IDENTITY")
    app.state.instance_id = uuid.uuid4().hex
    log.info("identity: %s v%s instance=%s", SERVICE_NAME, __version__, app.state.instance_id)

    _stage(app, 3, "REGISTRY")
    store = RegistryStore(config.database_path)
    await store.open()
    app.state.store = store
    app.state.registry = ServiceRegistry(store)
    log.info("service registry initialized (db=%s)", config.database_path)

    _stage(app, 4, "AUTH")
    # Config validation already refused weak/missing bootstrap secrets;
    # this stage exists so the certificate service (Milestone 3) has a home.
    log.info("token service ready (bootstrap + per-service tokens)")

    _stage(app, 5, "PLUGINS")
    plugin_manager = PluginManager()
    discovered = plugin_manager.discover()
    app.state.plugin_manager = plugin_manager
    await plugin_manager.start_all(app)
    log.info("plugins loaded: %d", len(discovered))

    _stage(app, 6, "HEALTH")
    monitor = HealthMonitor(app.state.registry, config)
    await monitor.start()
    app.state.health_monitor = monitor

    _stage(app, 7, "API")
    log.info("API serving on %s:%s", config.core_host, config.core_port)

    _stage(app, 8, "READY")
    app.state.ready = True
    log.info("Atlas Ready.")

    try:
        yield
    finally:
        app.state.ready = False
        log.info("Atlas Core shutting down")
        await monitor.stop()
        await plugin_manager.stop_all(app)
        await store.close()
        log.info("shutdown complete")


def create_app(config: CoreConfig | None = None) -> FastAPI:
    """Build the Atlas Core application.

    Stage 1 (CONFIG) effectively happens here: constructing CoreConfig
    validates the environment and raises on invalid configuration,
    refusing to boot.
    """
    if config is None:
        config = CoreConfig()

    _configure_logging(config.log_level)

    app = FastAPI(
        title="Atlas Core",
        version=__version__,
        description="Atlas OS Core — service registry, discovery, health, boot coordination.",
        lifespan=_lifespan,
    )
    app.state.config = config
    app.state.ready = False
    app.state.boot_stage = "0/8 INIT"

    app.include_router(system_api.router)
    app.include_router(registry_api.router)
    return app


def run() -> None:
    """Entry point: boot Atlas Core."""
    import uvicorn

    try:
        config = CoreConfig()
    except Exception as exc:
        print(f"FATAL: Atlas Core refused to boot: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    uvicorn.run(
        create_app(config),
        host=config.core_host,
        port=config.core_port,
        log_config=None,  # Atlas owns its log format
    )


if __name__ == "__main__":
    run()
