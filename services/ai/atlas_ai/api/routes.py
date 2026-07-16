# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""/v1/ask and /v1/interactions — the reasoning service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from atlas_sdk.service_auth import Identity, require_identity

from .. import SERVICE_NAME, __version__
from ..engine import AIEngine
from ..store import Interaction

router = APIRouter(tags=["ai"])


class AskRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


@router.get("/healthz", summary="Liveness (unauthenticated by design)")
async def healthz() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@router.post(
    "/v1/ask",
    response_model=Interaction,
    summary="Ask Atlas — grounded in governed truth; actions become plans",
)
async def ask(
    body: AskRequest, request: Request, identity: Identity = Depends(require_identity)
) -> Interaction:
    engine: AIEngine = request.app.state.engine
    return await engine.ask(body.prompt, identity)


@router.get(
    "/v1/interactions",
    response_model=list[Interaction],
    summary="Interaction audit (own; operator sees all)",
)
async def interactions(
    request: Request,
    limit: int = 100,
    identity: Identity = Depends(require_identity),
) -> list[Interaction]:
    requester = None if identity.is_operator else identity.name
    return await request.app.state.store.list(
        requester=requester, limit=max(1, min(limit, 500))
    )
