"""Authentication for Atlas Core APIs.

Zero Trust: every endpoint is authenticated except the liveness probes
(`/healthz`, `/v1/system/health`), which must be reachable by
infrastructure that holds no credentials.

Milestone 1 model (see docs/security.md):
- the bootstrap token authorizes service registration;
- per-service tokens (issued at registration, stored hashed) authorize
  everything else and are scoped to the owning instance for mutations.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .models import ServiceRecord
from .registry import ServiceRegistry

_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _get_registry(request: Request) -> ServiceRegistry:
    return request.app.state.registry


def _get_bootstrap_token(request: Request) -> str:
    return request.app.state.config.bootstrap_token


async def require_bootstrap(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Allow only the holder of the bootstrap token (service registration)."""
    if credentials is None:
        raise _unauthorized("missing bearer token")
    expected = _get_bootstrap_token(request)
    if not hmac.compare_digest(credentials.credentials, expected):
        raise _unauthorized("invalid bootstrap token")


async def require_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ServiceRecord | None:
    """Allow any authenticated caller: a registered service or the
    bootstrap-token holder (the operator).

    Returns the calling service's record, or None when authenticated via
    the bootstrap token.
    """
    if credentials is None:
        raise _unauthorized("missing bearer token")
    token = credentials.credentials
    if hmac.compare_digest(token, _get_bootstrap_token(request)):
        return None
    record = await _get_registry(request).authenticate_token(token)
    if record is None:
        raise _unauthorized("invalid or revoked service token")
    return record


async def require_instance_owner(
    instance_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ServiceRecord:
    """Allow only the service instance that owns `instance_id`.

    Service A's token can never heartbeat for, or deregister, service B.
    A token for a superseded instance yields 410 so the service knows to
    re-register.
    """
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
