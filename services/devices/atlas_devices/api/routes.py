# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""/v1/devices and /v1/adapters — the home's devices behind one abstraction."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status

from atlas_sdk.service_auth import Identity, require_identity

from .. import SERVICE_NAME, __version__
from ..models import (
    CommandRequest,
    CommandResult,
    DeviceRecord,
    DeviceSync,
    NATIVE_ID_PATTERN,
)
from ..service import CommandError, DeviceService, can_read_state

router = APIRouter(tags=["devices"])

_NATIVE = Path(pattern=NATIVE_ID_PATTERN.pattern)

PLANNER_NAME = "atlas.planner"


def _svc(request: Request) -> DeviceService:
    return request.app.state.devices


def _redact(record: DeviceRecord, identity: Identity) -> DeviceRecord:
    if can_read_state(identity.name, identity.is_operator, record):
        return record
    return record.model_copy(update={"state": {"redacted": True}})


@router.get("/healthz", summary="Liveness (unauthenticated by design)")
async def healthz() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


# -- adapter-facing -------------------------------------------------------------

@router.put(
    "/v1/adapters/{adapter}/devices/{native_id}",
    response_model=DeviceRecord,
    response_model_by_alias=True,
    summary="Adapter sync: create/update one of the adapter's own devices",
)
async def sync_device(
    adapter: str,
    body: DeviceSync,
    request: Request,
    native_id: str = _NATIVE,
    identity: Identity = Depends(require_identity),
) -> DeviceRecord:
    # An adapter may only sync devices as itself (operator may repair).
    if not identity.is_operator and identity.name != adapter:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"identity {identity.name!r} cannot sync devices for adapter {adapter!r}",
        )
    return await _svc(request).sync_device(
        adapter=adapter, native_id=native_id, sync=body
    )


@router.delete(
    "/v1/devices/{device_id}",
    response_model=DeviceRecord,
    response_model_by_alias=True,
    summary="Mark a device offline (steward adapter or operator)",
)
async def offline_device(
    device_id: str, request: Request, identity: Identity = Depends(require_identity)
) -> DeviceRecord:
    record = await request.app.state.store.get(device_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown device")
    if not identity.is_operator and identity.name != record.adapter:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not the steward")
    updated = await _svc(request).mark_offline(device_id)
    assert updated is not None
    return updated


# -- client-facing -----------------------------------------------------------------

@router.get(
    "/v1/devices",
    response_model=list[DeviceRecord],
    response_model_by_alias=True,
    summary="List devices (Class 3 states redacted for non-stewards)",
)
async def list_devices(
    request: Request,
    kind: str | None = None,
    room: str | None = None,
    adapter: str | None = None,
    identity: Identity = Depends(require_identity),
) -> list[DeviceRecord]:
    records = await request.app.state.store.list(kind=kind, room=room, adapter=adapter)
    return [_redact(r, identity) for r in records]


@router.get(
    "/v1/devices/{device_id}",
    response_model=DeviceRecord,
    response_model_by_alias=True,
    summary="One device (Class 3 state redacted for non-stewards)",
)
async def get_device(
    device_id: str, request: Request, identity: Identity = Depends(require_identity)
) -> DeviceRecord:
    record = await request.app.state.store.get(device_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown device")
    return _redact(record, identity)


# -- the Planner's door ---------------------------------------------------------------

@router.post(
    "/v1/invoke/devices.command",
    response_model=CommandResult,
    summary="Execute a device command (Planner or operator ONLY)",
)
async def invoke_command(
    body: CommandRequest, request: Request, identity: Identity = Depends(require_identity)
) -> CommandResult:
    if not identity.is_operator and identity.name != PLANNER_NAME:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "actions on the home flow through the Planner: submit a plan "
                "with capability 'devices.command'"
            ),
        )
    try:
        return await _svc(request).execute_command(body)
    except CommandError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
