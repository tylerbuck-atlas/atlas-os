# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Caller identity resolution for Atlas services.

Every non-Core Atlas service authenticates callers the same way, so the
logic lives here once:

- **mtls mode** — identity is the verified peer certificate's SAN URI
  (verification against the Atlas CA happened at the TLS handshake; see
  :mod:`atlas_sdk.tls`).
- **token mode (development)** — the bearer token is introspected against
  Core (``POST /v1/auth/introspect``) and cached briefly.

Usage::

    from atlas_sdk.service_auth import Identity, require_identity

    @router.post("/v1/things")
    async def create(request: Request, identity: Identity = Depends(require_identity)):
        ...

The dependency expects ``request.app.state.config.security_mode`` and, in
token mode, ``request.app.state.introspector``.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .tls import peer_cert_der_for_scope, peer_identity_from_der

log = logging.getLogger("atlas.sdk.auth")

OPERATOR_NAME = "atlas.operator"

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Identity:
    name: str
    instance_id: str
    version: str

    @property
    def is_operator(self) -> bool:
        return self.name == OPERATOR_NAME


class CoreIntrospector:
    """Resolves bearer tokens to identities by asking Atlas Core.

    Token (development) mode only; mtls mode never needs it.
    """

    def __init__(
        self,
        *,
        core_url: str,
        own_token_provider,
        cache_ttl: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._core_url = core_url.rstrip("/")
        #: Callable returning this service's current token (read late —
        #: it can rotate when the service re-registers).
        self._own_token_provider = own_token_provider
        self._cache_ttl = cache_ttl
        self._client = client or httpx.AsyncClient(timeout=5.0)
        self._cache: dict[str, tuple[float, Identity | None]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def introspect(self, token: str) -> Identity | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < self._cache_ttl:
            return cached[1]

        own_token = self._own_token_provider()
        if not own_token:
            log.warning("cannot introspect: service has no token yet")
            return None
        try:
            response = await self._client.post(
                f"{self._core_url}/v1/auth/introspect",
                headers={"Authorization": f"Bearer {own_token}"},
                json={"token": token},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("introspection against Core failed: %s", exc)
            return None  # fail closed; do not cache the failure

        body = response.json()
        identity: Identity | None = None
        if body.get("active") and body.get("service"):
            svc = body["service"]
            identity = Identity(svc["name"], svc["instance_id"], svc["version"])
        self._cache[key] = (now, identity)
        if len(self._cache) > 10_000:
            self._cache.clear()
        return identity


async def require_identity(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Identity:
    """FastAPI dependency: resolve and require an authenticated identity."""
    if request.app.state.config.security_mode == "mtls":
        der = peer_cert_der_for_scope(request.scope)
        parsed = peer_identity_from_der(der) if der else None
        if parsed is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="client certificate required",
            )
        name, instance_id = parsed
        return Identity(name, instance_id, "cert")

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    identity = await request.app.state.introspector.introspect(credentials.credentials)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token not recognized by Atlas Core",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return identity
