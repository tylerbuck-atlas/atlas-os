# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""/v1/facts — versioned, provenance-stamped, class-aware household state."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status

from atlas_sdk.service_auth import Identity, require_identity

from .. import SERVICE_NAME, __version__
from ..models import FactRecord, FactWrite, KEY_PATTERN, NAMESPACE_PATTERN
from ..service import MemoryService

router = APIRouter(tags=["memory"])

_NS = Path(pattern=NAMESPACE_PATTERN.pattern, max_length=200)
_KEY = Path(pattern=KEY_PATTERN.pattern)


def _svc(request: Request) -> MemoryService:
    return request.app.state.memory


@router.get("/healthz", summary="Liveness (unauthenticated by design)")
async def healthz() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@router.put(
    "/v1/facts/{namespace}/{key}",
    response_model=FactRecord,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Write a fact (appends a new immutable version)",
)
async def write_fact(
    body: FactWrite,
    request: Request,
    namespace: str = _NS,
    key: str = _KEY,
    identity: Identity = Depends(require_identity),
) -> FactRecord:
    return await _svc(request).write_fact(namespace, key, body, source=identity.name)


@router.get(
    "/v1/facts/{namespace}/{key}",
    response_model=FactRecord,
    response_model_by_alias=True,
    summary="Latest version of a fact (policy-checked)",
)
async def read_fact(
    request: Request,
    namespace: str = _NS,
    key: str = _KEY,
    identity: Identity = Depends(require_identity),
) -> FactRecord:
    record, allowed = await _svc(request).read_latest(namespace, key, identity)
    if record is None or record.deleted:
        raise HTTPException(status_code=404, detail="unknown fact")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Class 3 facts are readable only by their steward or the operator",
        )
    return record


@router.get(
    "/v1/facts/{namespace}/{key}/history",
    response_model=list[FactRecord],
    response_model_by_alias=True,
    summary="Version history of a fact, newest first (policy-checked)",
)
async def fact_history(
    request: Request,
    namespace: str = _NS,
    key: str = _KEY,
    limit: int = 100,
    identity: Identity = Depends(require_identity),
) -> list[FactRecord]:
    limit = max(1, min(limit, 1000))
    records, allowed = await _svc(request).read_history(
        namespace, key, identity, limit=limit
    )
    if not records:
        raise HTTPException(status_code=404, detail="unknown fact")
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not the steward")
    return records


@router.get(
    "/v1/facts/{namespace}",
    response_model=list[FactRecord],
    response_model_by_alias=True,
    summary="Latest facts in a namespace the caller may see",
)
async def query_facts(
    request: Request,
    namespace: str = _NS,
    key_prefix: str | None = None,
    max_class: int | None = None,
    identity: Identity = Depends(require_identity),
) -> list[FactRecord]:
    return await _svc(request).query(
        namespace, identity, key_prefix=key_prefix, max_class=max_class
    )


@router.delete(
    "/v1/facts/{namespace}/{key}",
    response_model=FactRecord,
    response_model_by_alias=True,
    summary="Tombstone a fact (history is preserved)",
)
async def delete_fact(
    request: Request,
    namespace: str = _NS,
    key: str = _KEY,
    identity: Identity = Depends(require_identity),
) -> FactRecord:
    record = await _svc(request).tombstone(namespace, key, source=identity.name)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown fact")
    return record
