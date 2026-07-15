# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""The Planner engine: policy evaluation, plan lifecycle, execution.

Pipeline (docs/planner-sentinel.md):

    submit → validate against policy → rejected
                                     → awaiting_approval → approve → execute
                                     → approved (auto)             → execute

**Default deny.** A capability with no matching policy is refused. The
most privileged thing the operator can do is write an ``allow`` rule —
and every rule, plan, approval, and step lands in the audit trail and on
the Event Bus.

Execution: each step resolves its capability to a live service through
Core's registry and invokes the uniform capability endpoint
``POST {address}/v1/invoke/{capability}``. Steps run in order;
the first failure fails the plan (v1: no partial-success ambiguity).
"""

from __future__ import annotations

import asyncio
import logging
import ssl

import httpx

from atlas_sdk import AtlasService, discover_service
from atlas_sdk.service_auth import Identity

from .models import (
    ActionRequest,
    PlanRecord,
    PlanRequest,
    PlanStatus,
    PolicyEffect,
    PolicyRecord,
    StepStatus,
    pattern_matches,
)
from .store import PlannerStore

log = logging.getLogger("atlas.planner")


def evaluate_policy(
    policies: list[PolicyRecord], requester: str, capability: str
) -> tuple[PolicyEffect, PolicyRecord | None]:
    """First matching rule wins (policies pre-sorted by priority, id).

    No match → DENY. Atlas never assumes permission.
    """
    for policy in policies:
        if pattern_matches(policy.requester, requester) and pattern_matches(
            policy.capability, capability
        ):
            return policy.effect, policy
    return PolicyEffect.DENY, None


class PlannerEngine:
    def __init__(
        self,
        store: PlannerStore,
        atlas: AtlasService,
        *,
        invoke_timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._atlas = atlas
        self._invoke_timeout = invoke_timeout
        self._client = client  # injectable for tests
        self._tasks: set[asyncio.Task] = set()

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- submission & validation ------------------------------------------------

    async def submit(self, request: PlanRequest, identity: Identity) -> PlanRecord:
        policies = await self._store.list_policies()
        verdicts = [
            evaluate_policy(policies, identity.name, action.capability)
            for action in request.actions
        ]

        denied = [
            (request.actions[i].capability, policy)
            for i, (effect, policy) in enumerate(verdicts)
            if effect == PolicyEffect.DENY
        ]
        if denied:
            capability, policy = denied[0]
            reason = (
                f"capability {capability!r} denied for {identity.name!r} by "
                + (f"policy #{policy.id}" if policy else "default-deny (no matching policy)")
            )
            plan = await self._store.create_plan(
                goal=request.goal, requester=identity.name,
                status=PlanStatus.REJECTED, reason=reason, actions=request.actions,
            )
            await self._emit("planner.plan.rejected", plan, extra={"reason": reason})
            log.warning("plan %s rejected: %s", plan.id, reason)
            return plan

        needs_approval = any(
            effect == PolicyEffect.REQUIRE_APPROVAL for effect, _ in verdicts
        )
        status = PlanStatus.AWAITING_APPROVAL if needs_approval else PlanStatus.APPROVED
        plan = await self._store.create_plan(
            goal=request.goal, requester=identity.name,
            status=status, reason=None, actions=request.actions,
        )
        await self._emit("planner.plan.submitted", plan)
        if needs_approval:
            log.info("plan %s awaiting operator approval", plan.id)
            return plan

        await self._emit("planner.plan.approved", plan, extra={"approved_by": "policy"})
        self._spawn_execution(plan.id)
        return plan

    async def approve(self, plan_id: str, identity: Identity) -> PlanRecord | None:
        plan = await self._store.get_plan(plan_id)
        if plan is None or plan.status != PlanStatus.AWAITING_APPROVAL:
            return plan
        await self._store.set_plan_status(
            plan_id, PlanStatus.APPROVED, approved_by=identity.name
        )
        plan = await self._store.get_plan(plan_id)
        assert plan is not None
        await self._emit("planner.plan.approved", plan, extra={"approved_by": identity.name})
        self._spawn_execution(plan_id)
        return plan

    async def cancel(self, plan_id: str, identity: Identity) -> PlanRecord | None:
        plan = await self._store.get_plan(plan_id)
        if plan is None or plan.status != PlanStatus.AWAITING_APPROVAL:
            return plan
        await self._store.set_plan_status(
            plan_id, PlanStatus.CANCELLED,
            reason=f"cancelled by {identity.name}",
        )
        plan = await self._store.get_plan(plan_id)
        assert plan is not None
        await self._emit("planner.plan.cancelled", plan)
        return plan

    # -- execution -----------------------------------------------------------------

    def _spawn_execution(self, plan_id: str) -> None:
        task = asyncio.create_task(self.execute(plan_id), name=f"plan-{plan_id[:8]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            ctx = self._atlas.client_ssl_context()
            self._client = httpx.AsyncClient(
                timeout=self._invoke_timeout,
                verify=ctx if ctx is not None else True,
                headers=(
                    {}
                    if self._atlas.security_mode == "mtls"
                    else {"Authorization": f"Bearer {self._atlas.service_token or ''}"}
                ),
            )
        return self._client

    async def _resolve(self, action_capability: str, pinned: str | None) -> dict | None:
        token, ssl_ctx = self._atlas.bus_credentials()
        try:
            services = await discover_service(
                core_url=self._atlas.core_url, token=token, ssl_context=ssl_ctx,
                capability=action_capability,
            )
        except (httpx.HTTPError, ssl.SSLError):
            return None
        live = [
            s for s in services
            if s.get("status") in ("starting", "healthy")
            and (pinned is None or s.get("name") == pinned)
        ]
        return live[0] if live else None

    async def execute(self, plan_id: str) -> None:
        plan = await self._store.get_plan(plan_id)
        if plan is None or plan.status != PlanStatus.APPROVED:
            return
        await self._store.set_plan_status(plan_id, PlanStatus.EXECUTING)

        for step in plan.steps:
            await self._store.update_step(
                plan_id, step.index, status=StepStatus.RUNNING, started=True
            )
            target = await self._resolve(step.capability, step.target_service)
            if target is None:
                await self._fail_step(
                    plan, step.index,
                    f"no live service provides {step.capability!r}"
                    + (f" (pinned to {step.target_service})" if step.target_service else ""),
                )
                return
            try:
                response = await self._http().post(
                    f"{target['address'].rstrip('/')}/v1/invoke/{step.capability}",
                    json=step.params,
                )
            except (httpx.HTTPError, ssl.SSLError) as exc:
                await self._fail_step(
                    plan, step.index, f"invocation failed: {exc}",
                    resolved=target.get("name"),
                )
                return
            if response.status_code >= 300:
                await self._fail_step(
                    plan, step.index,
                    f"service returned {response.status_code}: {response.text[:300]}",
                    resolved=target.get("name"),
                )
                return
            try:
                result = response.json()
                if not isinstance(result, dict):
                    result = {"result": result}
            except ValueError:
                result = {"raw": response.text[:1000]}
            await self._store.update_step(
                plan_id, step.index, status=StepStatus.SUCCEEDED,
                resolved_service=target.get("name"), result=result, finished=True,
            )
            await self._store.append_event(
                "planner.step.completed",
                {
                    "plan_id": plan_id, "step": step.index,
                    "capability": step.capability, "service": target.get("name"),
                },
            )

        await self._store.set_plan_status(plan_id, PlanStatus.COMPLETED)
        final = await self._store.get_plan(plan_id)
        assert final is not None
        await self._emit("planner.plan.completed", final)
        log.info("plan %s completed (%d step(s))", plan_id, len(plan.steps))

    async def _fail_step(
        self, plan: PlanRecord, index: int, error: str, *, resolved: str | None = None
    ) -> None:
        await self._store.update_step(
            plan.id, index, status=StepStatus.FAILED, error=error,
            resolved_service=resolved, finished=True,
        )
        for later in plan.steps[index + 1:]:
            await self._store.update_step(plan.id, later.index, status=StepStatus.SKIPPED)
        await self._store.set_plan_status(plan.id, PlanStatus.FAILED, reason=error)
        final = await self._store.get_plan(plan.id)
        assert final is not None
        await self._emit("planner.plan.failed", final, extra={"error": error})
        log.warning("plan %s failed at step %d: %s", plan.id, index, error)

    # -- events ---------------------------------------------------------------------

    async def _emit(self, topic: str, plan: PlanRecord, *, extra: dict | None = None) -> None:
        payload = {
            "plan_id": plan.id,
            "goal": plan.goal,
            "requester": plan.requester,
            "status": plan.status.value,
            "capabilities": [s.capability for s in plan.steps],
            **(extra or {}),
        }
        await self._store.append_event(topic, payload)
