# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""/v1/assist — ask the household's reasoning service.

Note what is absent: there is no endpoint here that acts on anything.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from atlas_sdk.service_auth import Identity, require_identity

from .. import SERVICE_NAME, __version__
from ..store import AssistRecord

router = APIRouter(tags=["ai"])


class AssistRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


@router.get("/healthz", summary="Liveness (unauthenticated by design)")
async def healthz() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@router.get("/v1/ai/info", summary="Backend info")
async def info(request: Request, identity: Identity = Depends(require_identity)) -> dict:
    return {
        "service": SERVICE_NAME,
        "version": __version__,
        "backend": request.app.state.engine._backend.name,
        "acts_directly": False,  # and always will
    }


@router.post(
    "/v1/assist",
    response_model=AssistRecord,
    summary="Gather truth, reason, and (maybe) propose a plan",
)
async def assist(
    body: AssistRequest, request: Request, identity: Identity = Depends(require_identity)
) -> AssistRecord:
    return await request.app.state.engine.assist(body.prompt, requester=identity.name)


@router.get(
    "/v1/assists",
    response_model=list[AssistRecord],
    summary="Assist audit log (own; operator sees all)",
)
async def list_assists(
    request: Request,
    limit: int = 100,
    identity: Identity = Depends(require_identity),
) -> list[AssistRecord]:
    requester = None if identity.is_operator else identity.name
    return await request.app.state.store.list_assists(
        requester=requester, limit=max(1, min(limit, 500))
    )
