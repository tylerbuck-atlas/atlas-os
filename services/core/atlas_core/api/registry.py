# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""/v1/registry — service registration, discovery, heartbeats."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import require_bootstrap, require_caller, require_instance_owner
from ..models import (
    RegistrationResponse,
    ServiceRecord,
    ServiceRegistration,
    ServiceStatus,
)
from ..registry import ServiceRegistry, UnknownServiceError

router = APIRouter(prefix="/v1/registry", tags=["registry"])


def _registry(request: Request) -> ServiceRegistry:
    return request.app.state.registry


@router.post(
    "/services",
    response_model=RegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_bootstrap)],
    summary="Register a service instance",
)
async def register_service(
    registration: ServiceRegistration, request: Request
) -> RegistrationResponse:
    record, token = await _registry(request).register(registration)
    return RegistrationResponse(
        service=record,
        service_token=token,
        heartbeat_interval_seconds=request.app.state.config.heartbeat_interval_seconds,
    )


@router.get(
    "/services",
    response_model=list[ServiceRecord],
    dependencies=[Depends(require_caller)],
    summary="Discover services",
)
async def list_services(
    request: Request,
    name: str | None = None,
    capability: str | None = None,
    status_filter: ServiceStatus | None = None,
) -> list[ServiceRecord]:
    return await _registry(request).find(
        name=name, capability=capability, status=status_filter
    )


@router.get(
    "/services/{instance_id}",
    response_model=ServiceRecord,
    dependencies=[Depends(require_caller)],
    summary="Get one service instance",
)
async def get_service(instance_id: str, request: Request) -> ServiceRecord:
    record = await _registry(request).get(instance_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown instance")
    return record


@router.post(
    "/services/{instance_id}/heartbeat",
    summary="Service heartbeat (instance-scoped token required)",
)
async def heartbeat(
    instance_id: str,
    request: Request,
    _owner: ServiceRecord = Depends(require_instance_owner),
) -> dict:
    try:
        record = await _registry(request).heartbeat(instance_id)
    except UnknownServiceError:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="re-register")
    return {"status": record.status.value}


@router.delete(
    "/services/{instance_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deregister (instance-scoped token required)",
)
async def deregister(
    instance_id: str,
    request: Request,
    _owner: ServiceRecord = Depends(require_instance_owner),
) -> None:
    try:
        await _registry(request).deregister(instance_id)
    except UnknownServiceError:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="already gone")
