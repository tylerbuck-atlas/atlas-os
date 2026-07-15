# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Authentication for Atlas Core APIs.

Two security modes (docs/security.md):

**mtls (default).** Identity is the verified peer certificate; its SAN
carries ``atlas://service/{name}/{instance_id}``. Service bearer tokens
are retired. The bootstrap token remains valid for exactly one thing:
enrolling (registering + receiving a certificate). Certificates are
short-lived; revocation is a registry state change — a certificate whose
instance is deregistered is refused even before it expires. The operator
identity (``atlas.operator``), mintable only by whoever holds the CA key,
is exempt from the registry check.

**token (development).** The Milestone-2 bearer-token behavior, kept for
tests and local development without a CA.

Liveness endpoints (`/healthz`, `/v1/system/health`) remain
unauthenticated by design in both modes.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from atlas_sdk.tls import peer_cert_der_for_scope, peer_identity_from_der

from .models import ServiceRecord, ServiceStatus
from .registry import ServiceRegistry

OPERATOR_NAME = "atlas.operator"

_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _get_registry(request: Request) -> ServiceRegistry:
    return request.app.state.registry


def _mode(request: Request) -> str:
    return request.app.state.config.security_mode


def _get_bootstrap_token(request: Request) -> str:
    return request.app.state.config.bootstrap_token


def peer_identity(request: Request) -> tuple[str, str] | None:
    """(service_name, instance_id) from the verified peer certificate."""
    der = peer_cert_der_for_scope(request.scope)
    if der is None:
        return None
    return peer_identity_from_der(der)


async def require_bootstrap(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """The enrollment credential — allows service registration only."""
    if credentials is None:
        raise _unauthorized("missing bearer token")
    expected = _get_bootstrap_token(request)
    if not hmac.compare_digest(credentials.credentials, expected):
        raise _unauthorized("invalid bootstrap token")


async def _caller_mtls(request: Request) -> ServiceRecord | None:
    identity = peer_identity(request)
    if identity is None:
        raise _unauthorized("client certificate required")
    name, instance_id = identity
    if name == OPERATOR_NAME:
        return None
    record = await _get_registry(request).get(instance_id)
    if (
        record is None
        or record.status == ServiceStatus.DEREGISTERED
        or record.name != name
    ):
        raise _unauthorized("certificate identity is not active in the registry")
    return record


async def _caller_token(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> ServiceRecord | None:
    if credentials is None:
        raise _unauthorized("missing bearer token")
    token = credentials.credentials
    if hmac.compare_digest(token, _get_bootstrap_token(request)):
        return None
    record = await _get_registry(request).authenticate_token(token)
    if record is None:
        raise _unauthorized("invalid or revoked service token")
    return record


async def require_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ServiceRecord | None:
    """Any authenticated caller.

    mtls: a live registered service (by peer cert) or the operator.
    token: a registered service's token or the bootstrap token.
    Returns the caller's record, or None for operator/bootstrap callers.
    """
    if _mode(request) == "mtls":
        return await _caller_mtls(request)
    return await _caller_token(request, credentials)


async def require_instance_owner(
    instance_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ServiceRecord:
    """Only the service instance that owns `instance_id`.

    Service A can never heartbeat for, or deregister, service B — in
    either mode. 410 signals "re-register" (superseded/deregistered).
    """
    if _mode(request) == "mtls":
        identity = peer_identity(request)
        if identity is None:
            raise _unauthorized("client certificate required")
        name, cert_instance = identity
        record = await _get_registry(request).get(cert_instance)
        if record is None or record.status == ServiceStatus.DEREGISTERED:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="certificate instance no longer active; re-register",
            )
        if cert_instance != instance_id or record.name != name:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="certificate is not valid for this instance",
            )
        return record

    if credentials is None:
        raise _unauthorized("missing bearer token")
    record = await _get_registry(request).authenticate_token(credentials.credentials)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="token no longer valid; re-register",
        )
    if record.instance_id != instance_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token is not valid for this instance",
        )
    return record
