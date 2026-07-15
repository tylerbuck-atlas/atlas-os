"""/v1/system — Core's own status, plus liveness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .. import SERVICE_NAME, __version__
from ..auth import require_caller
from ..models import Event

router = APIRouter(tags=["system"])


@router.get("/healthz", summary="Liveness (unauthenticated by design)")
async def healthz() -> dict:
    """Core follows the same service contract it enforces on everyone else."""
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@router.get("/v1/system/health", summary="Liveness alias (unauthenticated by design)")
async def system_health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@router.get(
    "/v1/system/status",
    dependencies=[Depends(require_caller)],
    summary="Boot stage and system overview",
)
async def system_status(request: Request) -> dict:
    registry = request.app.state.registry
    services = await registry.find()
    return {
        "service": SERVICE_NAME,
        "version": __version__,
        "boot_stage": request.app.state.boot_stage,
        "ready": request.app.state.ready,
        "instance_id": request.app.state.instance_id,
        "services_registered": len(services),
        "services": {r.name: r.status.value for r in services},
        "plugins": [
            {"name": p.name, "version": p.version}
            for p in request.app.state.plugin_manager.plugins
        ],
    }


@router.get(
    "/v1/system/events",
    response_model=list[Event],
    dependencies=[Depends(require_caller)],
    summary="Recent system events (newest first)",
)
async def system_events(request: Request, limit: int = 100) -> list[Event]:
    limit = max(1, min(limit, 1000))
    return await request.app.state.store.list_events(limit=limit)
