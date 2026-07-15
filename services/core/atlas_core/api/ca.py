# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""/v1/ca — the Atlas CA's public material.

The CA *certificate* is public by definition and served without
authentication so new services can establish trust at first contact.
For a hardened bootstrap, distribute the CA cert out-of-band instead
(ATLAS_CA_CERT in the SDK) — see docs/security.md.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

router = APIRouter(prefix="/v1/ca", tags=["ca"])


@router.get("/certificate", summary="The Atlas CA certificate (PEM, public)")
async def ca_certificate(request: Request) -> Response:
    ca = getattr(request.app.state, "ca", None)
    if ca is None:
        raise HTTPException(status_code=404, detail="CA not enabled (token mode)")
    return Response(content=ca.cert_pem, media_type="application/x-pem-file")
