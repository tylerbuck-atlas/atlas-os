# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""/v1/skills — signed, versioned, discoverable capability packages."""

from __future__ import annotations

import ssl

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from atlas_sdk import discover_service
from atlas_sdk.service_auth import Identity, require_identity

from .. import SERVICE_NAME, __version__
from ..store import SkillManifest, SkillRecord
from ..verify import verify_manifest

router = APIRouter(tags=["skills"])


@router.get("/healthz", summary="Liveness (unauthenticated by design)")
async def healthz() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


async def _check_artifact(request: Request, manifest: SkillManifest) -> str | None:
    """Cross-check the manifest's artifact against the Asset Manager.

    Returns an error string, or None when the artifact exists and its
    content address matches the signed manifest.
    """
    atlas = request.app.state.atlas
    token, ssl_ctx = atlas.bus_credentials()
    try:
        services = await discover_service(
            core_url=atlas.core_url, token=token, ssl_context=ssl_ctx, name="atlas.assets"
        )
    except (httpx.HTTPError, ssl.SSLError):
        return "asset manager is not reachable to verify the artifact"
    live = [s for s in services if s.get("status") in ("starting", "healthy")]
    if not live:
        return "asset manager is not available to verify the artifact"

    headers = {} if atlas.security_mode == "mtls" else {
        "Authorization": f"Bearer {atlas.service_token or ''}"
    }
    async with httpx.AsyncClient(
        timeout=10.0, verify=ssl_ctx if ssl_ctx is not None else True, headers=headers
    ) as client:
        response = await client.get(
            f"{live[0]['address'].rstrip('/')}/v1/assets/{manifest.artifact_asset_id}"
        )
    if response.status_code != 200:
        return f"artifact asset not found ({response.status_code})"
    asset = response.json()
    if asset.get("sha256") != manifest.artifact_sha256:
        return (
            "artifact hash mismatch: manifest says "
            f"{manifest.artifact_sha256[:16]}…, asset store has "
            f"{asset.get('sha256', '?')[:16]}…"
        )
    return None


@router.post(
    "/v1/skills",
    response_model=SkillRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Publish a skill (signature + artifact verified; operator only)",
)
async def publish_skill(
    manifest: SkillManifest, request: Request, identity: Identity = Depends(require_identity)
) -> SkillRecord:
    if not identity.is_operator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="operator certificate required to publish skills",
        )
    ca_pem = request.app.state.ca_cert_pem
    if ca_pem is None:
        raise HTTPException(status_code=503, detail="CA certificate unavailable")
    if not verify_manifest(manifest.model_dump(), ca_pem):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manifest signature does not verify against the Atlas CA — "
                   "sign with scripts/sign_skill.py",
        )
    if await request.app.state.store.exists(manifest.name, manifest.version):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that skill version is already published (versions are immutable)",
        )
    artifact_error = await _check_artifact(request, manifest)
    if artifact_error is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail=artifact_error)

    record = await request.app.state.store.publish(manifest, published_by=identity.name)
    await request.app.state.store.append_event(
        "skills.skill.published",
        {"name": manifest.name, "version": manifest.version,
         "provides": manifest.provides, "published_by": identity.name},
    )
    return record


@router.get(
    "/v1/skills",
    response_model=list[SkillRecord],
    summary="Discover skills",
)
async def list_skills(
    request: Request,
    name: str | None = None,
    enabled_only: bool = False,
    identity: Identity = Depends(require_identity),
) -> list[SkillRecord]:
    return await request.app.state.store.list(name=name, enabled_only=enabled_only)


@router.get(
    "/v1/skills/{name}/{version}",
    response_model=SkillRecord,
    summary="One skill version",
)
async def get_skill(
    name: str, version: str, request: Request,
    identity: Identity = Depends(require_identity),
) -> SkillRecord:
    record = await request.app.state.store.get(name, version)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown skill version")
    return record


@router.post(
    "/v1/skills/{name}/{version}/enable",
    response_model=SkillRecord,
    summary="Enable a skill version (operator only)",
)
async def enable_skill(
    name: str, version: str, request: Request,
    identity: Identity = Depends(require_identity),
) -> SkillRecord:
    return await _set_enabled(name, version, True, request, identity)


@router.post(
    "/v1/skills/{name}/{version}/disable",
    response_model=SkillRecord,
    summary="Disable a skill version (operator only)",
)
async def disable_skill(
    name: str, version: str, request: Request,
    identity: Identity = Depends(require_identity),
) -> SkillRecord:
    return await _set_enabled(name, version, False, request, identity)


async def _set_enabled(
    name: str, version: str, enabled: bool, request: Request, identity: Identity
) -> SkillRecord:
    if not identity.is_operator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="operator certificate required",
        )
    if not await request.app.state.store.set_enabled(name, version, enabled):
        raise HTTPException(status_code=404, detail="unknown skill version")
    record = await request.app.state.store.get(name, version)
    assert record is not None
    await request.app.state.store.append_event(
        "skills.skill.enabled" if enabled else "skills.skill.disabled",
        {"name": name, "version": version, "by": identity.name},
    )
    return record
