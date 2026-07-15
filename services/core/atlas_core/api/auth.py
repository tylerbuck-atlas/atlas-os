# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""/v1/auth — token introspection.

Lets other Atlas services (today: the Event Bus) verify a caller's token
without ever holding Core's secrets. The introspecting service must itself
be authenticated. Retired in Milestone 3 when identity moves to mTLS peer
certificates (docs/security.md).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from .. import SERVICE_NAME, __version__
from ..auth import require_caller

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class IntrospectRequest(BaseModel):
    token: str


class IntrospectedIdentity(BaseModel):
    name: str
    instance_id: str
    version: str


class IntrospectResponse(BaseModel):
    active: bool
    service: IntrospectedIdentity | None = None


@router.post(
    "/introspect",
    response_model=IntrospectResponse,
    dependencies=[Depends(require_caller)],
    summary="Resolve a bearer token to a service identity",
)
async def introspect(body: IntrospectRequest, request: Request) -> IntrospectResponse:
    import hmac

    # The bootstrap token resolves to Core's own (operator) identity.
    if hmac.compare_digest(body.token, request.app.state.config.bootstrap_token):
        return IntrospectResponse(
            active=True,
            service=IntrospectedIdentity(
                name=SERVICE_NAME,
                instance_id=request.app.state.instance_id,
                version=__version__,
            ),
        )

    record = await request.app.state.registry.authenticate_token(body.token)
    if record is None:
        return IntrospectResponse(active=False, service=None)
    return IntrospectResponse(
        active=True,
        service=IntrospectedIdentity(
            name=record.name,
            instance_id=record.instance_id,
            version=record.version,
        ),
    )
