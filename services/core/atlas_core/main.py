# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Atlas Core application factory and boot sequence.

Boot is explicit, ordered, and observable (docs/architecture.md §3):

    1. CONFIG    2. IDENTITY    3. REGISTRY    4. AUTH
    5. PLUGINS   6. HEALTH      7. API         8. READY

Any stage failing aborts boot — there is no half-booted mode. Stage 8
logs exactly: ``Atlas Ready.``

In mtls mode (the default), stage 4 also brings up the Atlas CA, and
Core serves HTTPS with client-certificate verification; see
docs/security.md.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import sys
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from atlas_sdk.tls import TLSRuntime, cert_seconds_remaining

from . import SERVICE_NAME, __version__
from .api import auth as auth_api
from .api import ca as ca_api
from .api import registry as registry_api
from .api import system as system_api
from .ca import CertificateAuthority
from .config import CoreConfig
from .health import HealthMonitor
from .plugins import PluginManager
from .publisher import EventPublisher
from .registry import ServiceRegistry
from .store import RegistryStore

log = logging.getLogger("atlas.core")

SELF_CERT_TTL_HOURS = 24


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


def _issue_self_credentials(app: FastAPI) -> None:
    """Issue (or re-issue) Core's own certificate from its CA."""
    config: CoreConfig = app.state.config
    ca: CertificateAuthority = app.state.ca
    hostnames = [h.strip() for h in config.core_hostnames.split(",") if h.strip()]
    key_pem, cert_pem = ca.issue_self(
        common_name=SERVICE_NAME,
        instance_id=app.state.instance_id,
        dns_names=hostnames,
        ttl_hours=SELF_CERT_TTL_HOURS,
    )
    app.state.core_tls.write(key_pem=key_pem, cert_pem=cert_pem, ca_pem=ca.cert_pem)
    app.state.core_cert_pem = cert_pem


def init_tls(app: FastAPI) -> None:
    """Bring up the CA and Core's own credentials (mtls mode).

    Called from run() before the server starts (the listener needs the
    certificate), and idempotently from boot stage 4.
    """
    if getattr(app.state, "ca", None) is not None:
        return
    config: CoreConfig = app.state.config
    ca = CertificateAuthority(config.ca_dir)
    created = ca.ensure()
    app.state.ca = ca
    app.state.ca_created = created
    app.state.core_tls = TLSRuntime.prepare(f"{config.ca_dir}/core-runtime")
    _issue_self_credentials(app)


def _outbound_client(app: FastAPI, *, timeout: float) -> httpx.AsyncClient:
    """HTTP client for Core's outbound calls (probes, bus publishing) —
    mutual TLS in mtls mode."""
    if getattr(app.state, "core_tls", None) is not None:
        ctx = app.state.core_tls.client_ssl_context()
        return httpx.AsyncClient(timeout=timeout, verify=ctx)
    return httpx.AsyncClient(timeout=timeout)


async def _self_cert_rotation_loop(app: FastAPI) -> None:
    """Re-issue Core's certificate at 2/3 lifetime and hot-reload the
    listener's SSL context (new handshakes pick it up immediately)."""
    while True:
        remaining = cert_seconds_remaining(app.state.core_cert_pem)
        await asyncio.sleep(max(remaining / 3, 60))
        try:
            _issue_self_credentials(app)
            reload_hook = getattr(app.state, "tls_reload", None)
            if reload_hook:
                reload_hook()
            log.info("core certificate rotated")
        except Exception:
            log.exception("core certificate rotation failed")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config: CoreConfig = app.state.config

    _stage(app, 1, "CONFIG")
    log.info("configuration loaded and validated (security_mode=%s)", config.security_mode)
    _stage(app, 2, "IDENTITY")
    log.info("identity: %s v%s instance=%s", SERVICE_NAME, __version__, app.state.instance_id)

    _stage(app, 3, "REGISTRY")
    store = RegistryStore(config.database_path)
    await store.open()
    app.state.store = store
    app.state.registry = ServiceRegistry(store)
    log.info("service registry initialized (db=%s)", config.database_path)

    _stage(app, 4, "AUTH")
    if config.security_mode == "mtls":
        init_tls(app)
        log.info(
            "Atlas CA %s; identity = peer certificates; tokens retired "
            "(bootstrap token valid for enrollment only)",
            "created" if app.state.ca_created else "loaded",
        )
    else:
        log.warning("token security mode — development only (docs/security.md)")

    _stage(app, 5, "PLUGINS")
    verifier = app.state.ca.verify_blob if getattr(app.state, "ca", None) else None
    plugin_manager = PluginManager(
        require_signed=config.signed_plugins_required(), verifier=verifier
    )
    discovered = plugin_manager.discover()
    app.state.plugin_manager = plugin_manager
    await plugin_manager.start_all(app)
    log.info(
        "plugins loaded: %d (signature verification %s)",
        len(discovered),
        "required" if config.signed_plugins_required() else "off",
    )

    _stage(app, 6, "HEALTH")
    monitor = HealthMonitor(
        app.state.registry, config,
        client=_outbound_client(app, timeout=config.probe_timeout_seconds),
    )
    await monitor.start()
    app.state.health_monitor = monitor
    publisher = EventPublisher(
        store, app.state.registry, config,
        client=_outbound_client(app, timeout=5.0),
    )
    await publisher.start()
    app.state.event_publisher = publisher
    rotation_task = None
    if config.security_mode == "mtls":
        rotation_task = asyncio.create_task(
            _self_cert_rotation_loop(app), name="atlas-core-cert-rotation"
        )

    _stage(app, 7, "API")
    scheme = "https" if config.security_mode == "mtls" else "http"
    log.info("API serving on %s://%s:%s", scheme, config.core_host, config.core_port)

    _stage(app, 8, "READY")
    app.state.ready = True
    log.info("Atlas Ready.")

    try:
        yield
    finally:
        app.state.ready = False
        log.info("Atlas Core shutting down")
        if rotation_task:
            rotation_task.cancel()
            try:
                await rotation_task
            except asyncio.CancelledError:
                pass
        await publisher.stop()
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
    app.state.instance_id = uuid.uuid4().hex

    app.include_router(system_api.router)
    app.include_router(registry_api.router)
    app.include_router(auth_api.router)
    app.include_router(ca_api.router)
    return app


def run() -> None:
    """Entry point: boot Atlas Core."""
    import uvicorn

    try:
        config = CoreConfig()
    except Exception as exc:
        print(f"FATAL: Atlas Core refused to boot: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    app = create_app(config)

    kwargs: dict = {}
    if config.security_mode == "mtls":
        # The listener needs Core's certificate before it can accept
        # connections, so the CA comes up before the server does.
        init_tls(app)
        kwargs = app.state.core_tls.uvicorn_kwargs()

    server_config = uvicorn.Config(
        app,
        host=config.core_host,
        port=config.core_port,
        log_config=None,  # Atlas owns its log format
        **kwargs,
    )
    server = uvicorn.Server(server_config)

    if config.security_mode == "mtls":
        # Hot-reload hook: rotation rewrites the cert files, then reloads
        # them into the LIVE SSLContext — new handshakes use the fresh
        # cert without a listener restart.
        tls = app.state.core_tls

        def _reload_tls() -> None:
            if server_config.ssl is not None:
                server_config.ssl.load_cert_chain(
                    str(tls.cert_path), str(tls.key_path)
                )

        app.state.tls_reload = _reload_tls

    server.run()


if __name__ == "__main__":
    run()
