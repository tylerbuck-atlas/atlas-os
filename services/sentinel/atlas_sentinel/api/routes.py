# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""/v1/alerts — Sentinel's findings."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from atlas_sdk.service_auth import Identity, require_identity

from .. import SERVICE_NAME, __version__
from ..store import Alert

router = APIRouter(tags=["sentinel"])


@router.get("/healthz", summary="Liveness (unauthenticated by design)")
async def healthz() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@router.get(
    "/v1/alerts",
    response_model=list[Alert],
    summary="Alerts (open by default)",
)
async def list_alerts(
    request: Request,
    include_acknowledged: bool = False,
    severity: str | None = None,
    limit: int = 200,
    identity: Identity = Depends(require_identity),
) -> list[Alert]:
    return await request.app.state.store.list_alerts(
        include_acknowledged=include_acknowledged,
        severity=severity,
        limit=max(1, min(limit, 1000)),
    )


@router.post(
    "/v1/alerts/{alert_id}/ack",
    response_model=Alert,
    summary="Acknowledge an alert (operator only)",
)
async def ack_alert(
    alert_id: str, request: Request, identity: Identity = Depends(require_identity)
) -> Alert:
    if not identity.is_operator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="operator certificate required",
        )
    alert = await request.app.state.store.ack_alert(alert_id, acked_by=identity.name)
    if alert is None:
        raise HTTPException(status_code=404, detail="unknown alert")
    return alert
