# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""/v1/plans and /v1/policies — the only path from intent to action."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from atlas_sdk.service_auth import Identity, require_identity

from .. import SERVICE_NAME, __version__
from ..engine import PlannerEngine
from ..models import PlanRecord, PlanRequest, PlanStatus, PolicyRecord, PolicyWrite

router = APIRouter(tags=["planner"])


def _engine(request: Request) -> PlannerEngine:
    return request.app.state.engine


def _require_operator(identity: Identity) -> None:
    if not identity.is_operator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="operator certificate required",
        )


@router.get("/healthz", summary="Liveness (unauthenticated by design)")
async def healthz() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


# -- policies (operator only) ---------------------------------------------------

@router.post(
    "/v1/policies",
    response_model=PolicyRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Add a policy rule (operator only)",
)
async def add_policy(
    body: PolicyWrite, request: Request, identity: Identity = Depends(require_identity)
) -> PolicyRecord:
    _require_operator(identity)
    record = await request.app.state.store.add_policy(
        priority=body.priority, requester=body.requester,
        capability=body.capability, effect=body.effect, note=body.note,
        created_by=identity.name,
    )
    await request.app.state.store.append_event(
        "planner.policy.added",
        {"policy_id": record.id, "requester": record.requester,
         "capability": record.capability, "effect": record.effect.value},
    )
    return record


@router.get(
    "/v1/policies",
    response_model=list[PolicyRecord],
    summary="List policy rules (any authenticated service)",
)
async def list_policies(
    request: Request, identity: Identity = Depends(require_identity)
) -> list[PolicyRecord]:
    return await request.app.state.store.list_policies()


@router.delete(
    "/v1/policies/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a policy rule (operator only)",
)
async def delete_policy(
    policy_id: int, request: Request, identity: Identity = Depends(require_identity)
) -> None:
    _require_operator(identity)
    if not await request.app.state.store.delete_policy(policy_id):
        raise HTTPException(status_code=404, detail="unknown policy")
    await request.app.state.store.append_event(
        "planner.policy.removed", {"policy_id": policy_id, "removed_by": identity.name}
    )


# -- plans -------------------------------------------------------------------------

@router.post(
    "/v1/plans",
    response_model=PlanRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a plan (validated against policy; may auto-execute)",
)
async def submit_plan(
    body: PlanRequest, request: Request, identity: Identity = Depends(require_identity)
) -> PlanRecord:
    return await _engine(request).submit(body, identity)


@router.get(
    "/v1/plans",
    response_model=list[PlanRecord],
    summary="Audit trail: list plans",
)
async def list_plans(
    request: Request,
    status_filter: PlanStatus | None = None,
    requester: str | None = None,
    limit: int = 100,
    identity: Identity = Depends(require_identity),
) -> list[PlanRecord]:
    # Services see their own plans; the operator sees everything.
    if not identity.is_operator:
        requester = identity.name
    return await request.app.state.store.list_plans(
        status=status_filter, requester=requester, limit=max(1, min(limit, 500))
    )


@router.get(
    "/v1/plans/{plan_id}",
    response_model=PlanRecord,
    summary="Full audit record of one plan",
)
async def get_plan(
    plan_id: str, request: Request, identity: Identity = Depends(require_identity)
) -> PlanRecord:
    plan = await request.app.state.store.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="unknown plan")
    if not identity.is_operator and plan.requester != identity.name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="not your plan"
        )
    return plan


@router.post(
    "/v1/plans/{plan_id}/approve",
    response_model=PlanRecord,
    summary="Approve a plan awaiting approval (operator only)",
)
async def approve_plan(
    plan_id: str, request: Request, identity: Identity = Depends(require_identity)
) -> PlanRecord:
    _require_operator(identity)
    plan = await _engine(request).approve(plan_id, identity)
    if plan is None:
        raise HTTPException(status_code=404, detail="unknown plan")
    if plan.status not in (PlanStatus.APPROVED, PlanStatus.EXECUTING,
                           PlanStatus.COMPLETED, PlanStatus.FAILED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"plan is {plan.status.value}, not awaiting approval",
        )
    return plan


@router.post(
    "/v1/plans/{plan_id}/cancel",
    response_model=PlanRecord,
    summary="Cancel a plan awaiting approval (operator only)",
)
async def cancel_plan(
    plan_id: str, request: Request, identity: Identity = Depends(require_identity)
) -> PlanRecord:
    _require_operator(identity)
    plan = await _engine(request).cancel(plan_id, identity)
    if plan is None:
        raise HTTPException(status_code=404, detail="unknown plan")
    if plan.status != PlanStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"plan is {plan.status.value}, not awaiting approval",
        )
    return plan
