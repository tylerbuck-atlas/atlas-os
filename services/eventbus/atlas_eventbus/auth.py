# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Event Bus authentication.

Zero Trust: the bus holds no secrets and trusts no bearer token on sight.
Every unknown token is resolved to a service identity via Atlas Core's
introspection API (POST /v1/auth/introspect), using the bus's own service
token to authenticate the introspection call itself. Verified identities
are cached briefly (TTL) to keep hot paths off Core.

Replaced by mTLS peer identities in Milestone 3.
"""

from __future__ import annotations

import hashlib
import logging
import time

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

log = logging.getLogger("atlas.eventbus.auth")

_bearer = HTTPBearer(auto_error=False)


class Identity:
    __slots__ = ("name", "instance_id", "version")

    def __init__(self, name: str, instance_id: str, version: str) -> None:
        self.name = name
        self.instance_id = instance_id
        self.version = version


class CoreIntrospector:
    """Resolves bearer tokens to identities by asking Atlas Core."""

    def __init__(
        self,
        *,
        core_url: str,
        own_token_provider,
        cache_ttl: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._core_url = core_url.rstrip("/")
        #: Callable returning the bus's current service token (it can rotate
        #: when the bus re-registers, so it must be read late, not captured).
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
            log.warning("cannot introspect: bus has no service token yet")
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
            # Fail closed, but do not cache the failure.
            return None

        body = response.json()
        identity: Identity | None = None
        if body.get("active") and body.get("service"):
            svc = body["service"]
            identity = Identity(svc["name"], svc["instance_id"], svc["version"])
        self._cache[key] = (now, identity)
        # Bound the cache.
        if len(self._cache) > 10_000:
            self._cache.clear()
        return identity


async def require_identity(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Identity:
    """FastAPI dependency: resolve and require an authenticated identity."""
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
