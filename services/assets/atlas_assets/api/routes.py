# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""/v1/assets — the home's files as first-class, auditable truth sources."""

from __future__ import annotations

import json

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)

from atlas_sdk.service_auth import Identity, require_identity

from .. import SERVICE_NAME, __version__
from ..store import AssetRecord, AssetStore, IntegrityError

router = APIRouter(tags=["assets"])

KINDS = {"manual", "document", "photo", "firmware", "other"}
CLASS_INTIMATE = 3


def _store(request: Request) -> AssetStore:
    return request.app.state.store


def _can_read(identity: Identity, record: AssetRecord) -> bool:
    """Same Milestone-4 rule as Memory: Class 3 is steward-or-operator only."""
    if record.data_class < CLASS_INTIMATE:
        return True
    return identity.is_operator or identity.name == record.uploaded_by


@router.get("/healthz", summary="Liveness (unauthenticated by design)")
async def healthz() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@router.post(
    "/v1/assets",
    response_model=AssetRecord,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest an asset (multipart upload)",
)
async def upload_asset(
    request: Request,
    file: UploadFile,
    name: str | None = Form(default=None, max_length=300),
    kind: str = Form(default="document"),
    data_class: int = Form(default=1, alias="class", ge=0, le=3),
    tags: str = Form(default="", description="comma-separated"),
    metadata: str = Form(default="{}", description="JSON object"),
    owner: str | None = Form(default=None, max_length=100),
    identity: Identity = Depends(require_identity),
) -> AssetRecord:
    if kind not in KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {sorted(KINDS)}")
    try:
        metadata_obj = json.loads(metadata)
        assert isinstance(metadata_obj, dict)
    except (json.JSONDecodeError, AssertionError):
        raise HTTPException(status_code=422, detail="metadata must be a JSON object")

    content = await file.read()
    max_bytes = request.app.state.config.max_upload_bytes
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"asset exceeds {max_bytes} bytes")
    if not content:
        raise HTTPException(status_code=422, detail="empty upload")

    store = _store(request)
    sha256 = store.write_blob(content)
    record = await store.insert(
        name=name or file.filename or sha256[:16],
        kind=kind,
        data_class=data_class,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        metadata=metadata_obj,
        sha256=sha256,
        size=len(content),
        content_type=file.content_type or "application/octet-stream",
        uploaded_by=identity.name,
        owner=owner,
    )
    # Ingest event: metadata only, never content; Class 2+ redacts the name.
    await store.append_event(
        "assets.asset.ingested",
        {
            "asset_id": record.id,
            "kind": record.kind,
            "class": record.data_class,
            "sha256": record.sha256,
            "size": record.size,
            "uploaded_by": record.uploaded_by,
            **({"name": record.name} if record.data_class <= 1 else {"redacted": True}),
        },
    )
    return record


@router.get(
    "/v1/assets",
    response_model=list[AssetRecord],
    response_model_by_alias=True,
    summary="List assets the caller may see",
)
async def list_assets(
    request: Request,
    kind: str | None = None,
    tag: str | None = None,
    max_class: int | None = None,
    identity: Identity = Depends(require_identity),
) -> list[AssetRecord]:
    records = await _store(request).list(kind=kind, tag=tag, max_class=max_class)
    return [r for r in records if _can_read(identity, r)]


async def _readable(request: Request, asset_id: str, identity: Identity) -> AssetRecord:
    record = await _store(request).get(asset_id)
    if record is None or record.deleted:
        raise HTTPException(status_code=404, detail="unknown asset")
    if not _can_read(identity, record):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Class 3 assets are readable only by their steward or the operator",
        )
    return record


@router.get(
    "/v1/assets/{asset_id}",
    response_model=AssetRecord,
    response_model_by_alias=True,
    summary="Asset metadata (policy-checked)",
)
async def get_asset(
    asset_id: str, request: Request, identity: Identity = Depends(require_identity)
) -> AssetRecord:
    return await _readable(request, asset_id, identity)


@router.get(
    "/v1/assets/{asset_id}/content",
    summary="Asset content, integrity-verified against its address",
)
async def get_content(
    asset_id: str, request: Request, identity: Identity = Depends(require_identity)
) -> Response:
    record = await _readable(request, asset_id, identity)
    try:
        content = _store(request).read_blob(record.sha256)
    except FileNotFoundError:
        raise HTTPException(status_code=410, detail="blob missing from store")
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="blob failed integrity verification — refusing to serve",
        )
    return Response(
        content=content,
        media_type=record.content_type,
        headers={
            "X-Atlas-SHA256": record.sha256,
            "Content-Disposition": f'attachment; filename="{record.name}"',
        },
    )


@router.delete(
    "/v1/assets/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Tombstone an asset (blob removed when unreferenced)",
)
async def delete_asset(
    asset_id: str, request: Request, identity: Identity = Depends(require_identity)
) -> None:
    record = await _readable(request, asset_id, identity)
    store = _store(request)
    await store.tombstone(record.id)
    await store.delete_blob_if_unreferenced(record.sha256)
    await store.append_event(
        "assets.asset.deleted",
        {"asset_id": record.id, "class": record.data_class, "deleted_by": identity.name},
    )
