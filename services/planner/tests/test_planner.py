# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Planner: default deny, policy evaluation, lifecycle, execution, audit."""

from __future__ import annotations

from atlas_planner.models import PolicyEffect, PolicyRecord, pattern_matches
from atlas_planner.engine import evaluate_policy
from atlas_planner.models import utcnow

from .conftest import (
    AI_TOKEN,
    OPERATOR_TOKEN,
    OTHER_TOKEN,
    add_policy,
    auth,
    submit,
    wait_for_status,
)


def _policy(id, priority, requester, capability, effect) -> PolicyRecord:
    return PolicyRecord(
        id=id, priority=priority, requester=requester, capability=capability,
        effect=effect, note="", created_by="test", created_at=utcnow(),
    )


class TestPolicyEvaluation:
    def test_default_deny_with_no_policies(self):
        effect, matched = evaluate_policy([], "atlas.ai", "echo.reply")
        assert effect == PolicyEffect.DENY
        assert matched is None

    def test_first_match_wins_by_priority(self):
        policies = [
            _policy(1, 10, "atlas.ai", "echo.reply", PolicyEffect.DENY),
            _policy(2, 20, "atlas.ai", "echo.*", PolicyEffect.ALLOW),
        ]
        effect, matched = evaluate_policy(policies, "atlas.ai", "echo.reply")
        assert effect == PolicyEffect.DENY
        assert matched.id == 1

    def test_wildcards(self):
        assert pattern_matches("*", "anything")
        assert pattern_matches("echo.*", "echo.reply")
        assert not pattern_matches("echo.*", "devices.light.on")
        assert not pattern_matches("echo.*", "echox.reply")

    def test_requester_scoping(self):
        policies = [_policy(1, 10, "atlas.ai", "echo.reply", PolicyEffect.ALLOW)]
        effect, _ = evaluate_policy(policies, "atlas.other", "echo.reply")
        assert effect == PolicyEffect.DENY


class TestPlanLifecycle:
    async def test_default_deny_rejects_plan(self, client, world):
        plan = await submit(client)
        assert plan["status"] == "rejected"
        assert "default-deny" in plan["reason"]
        assert world.invocations == []  # nothing ever executed

    async def test_allowed_plan_executes(self, client, world):
        await add_policy(client, effect="allow")
        plan = await submit(client)
        final = await wait_for_status(client, plan["id"], {"completed"})
        assert final["steps"][0]["status"] == "succeeded"
        assert final["steps"][0]["result"] == {"reply": "ok"}
        assert final["steps"][0]["resolved_service"] == "atlas.echo"
        assert world.invocations == [{
            "host": "echo.test", "capability": "echo.reply",
            "params": {"message": "hi"},
        }]

    async def test_deny_policy_beats_allow_by_priority(self, client, world):
        await add_policy(client, effect="deny", priority=10)
        await add_policy(client, effect="allow", priority=100)
        plan = await submit(client)
        assert plan["status"] == "rejected"
        assert world.invocations == []

    async def test_require_approval_waits_then_executes(self, client, world):
        await add_policy(client, effect="require_approval")
        plan = await submit(client)
        assert plan["status"] == "awaiting_approval"
        assert world.invocations == []

        response = await client.post(
            f"/v1/plans/{plan['id']}/approve", headers=auth(OPERATOR_TOKEN)
        )
        assert response.status_code == 200
        final = await wait_for_status(client, plan["id"], {"completed"})
        assert final["approved_by"] == "atlas.operator"
        assert len(world.invocations) == 1

    async def test_non_operator_cannot_approve(self, client):
        await add_policy(client, effect="require_approval")
        plan = await submit(client)
        response = await client.post(
            f"/v1/plans/{plan['id']}/approve", headers=auth(AI_TOKEN)
        )
        assert response.status_code == 403

    async def test_cancel_prevents_execution(self, client, world):
        await add_policy(client, effect="require_approval")
        plan = await submit(client)
        response = await client.post(
            f"/v1/plans/{plan['id']}/cancel", headers=auth(OPERATOR_TOKEN)
        )
        assert response.json()["status"] == "cancelled"
        assert world.invocations == []

    async def test_step_failure_fails_plan_and_skips_rest(self, client, world):
        await add_policy(client, capability="echo.*", effect="allow")
        world.invoke_results["echo.reply"] = 500
        response = await client.post(
            "/v1/plans",
            json={"goal": "two-step", "actions": [
                {"capability": "echo.reply", "params": {"message": "a"}},
                {"capability": "echo.reply", "params": {"message": "b"}},
            ]},
            headers=auth(AI_TOKEN),
        )
        plan = response.json()
        final = await wait_for_status(client, plan["id"], {"failed"})
        assert final["steps"][0]["status"] == "failed"
        assert "500" in final["steps"][0]["error"]
        assert final["steps"][1]["status"] == "skipped"
        assert len(world.invocations) == 1  # second step never ran

    async def test_no_live_provider_fails_plan(self, client, world):
        await add_policy(client, capability="ghost.cap", effect="allow")
        world.services["ghost.cap"] = []
        plan = await submit(client, capability="ghost.cap")
        final = await wait_for_status(client, plan["id"], {"failed"})
        assert "no live service" in final["steps"][0]["error"]


class TestAuditAndScoping:
    async def test_requester_sees_only_own_plans(self, client):
        await add_policy(client, requester="*", capability="echo.reply", effect="allow")
        mine = await submit(client, token=AI_TOKEN)
        await submit(client, token=OTHER_TOKEN)

        listing = await client.get("/v1/plans", headers=auth(AI_TOKEN))
        ids = [p["id"] for p in listing.json()]
        assert ids == [mine["id"]]

        response = await client.get(f"/v1/plans/{mine['id']}", headers=auth(OTHER_TOKEN))
        assert response.status_code == 403

    async def test_operator_sees_everything(self, client):
        await add_policy(client, requester="*", capability="echo.reply", effect="allow")
        await submit(client, token=AI_TOKEN)
        await submit(client, token=OTHER_TOKEN)
        listing = await client.get("/v1/plans", headers=auth(OPERATOR_TOKEN))
        assert len(listing.json()) == 2

    async def test_rejected_plan_is_audited_with_events(self, app, client):
        plan = await submit(client)
        assert plan["status"] == "rejected"
        events = await app.state.store.list_events_after(0, 50)
        topics = [e[1] for e in events]
        assert "planner.plan.rejected" in topics

    async def test_completed_plan_emits_lifecycle_events(self, app, client):
        await add_policy(client, effect="allow")
        plan = await submit(client)
        await wait_for_status(client, plan["id"], {"completed"})
        events = await app.state.store.list_events_after(0, 50)
        topics = [e[1] for e in events]
        for expected in ("planner.policy.added", "planner.plan.submitted",
                         "planner.plan.approved", "planner.step.completed",
                         "planner.plan.completed"):
            assert expected in topics, topics


class TestPolicyAdministration:
    async def test_only_operator_manages_policies(self, client):
        response = await client.post(
            "/v1/policies",
            json={"requester": "*", "capability": "*", "effect": "allow"},
            headers=auth(AI_TOKEN),
        )
        assert response.status_code == 403

    async def test_policy_delete_restores_default_deny(self, client, world):
        policy = await add_policy(client, effect="allow")
        assert (await submit(client))["status"] == "approved"

        await client.delete(
            f"/v1/policies/{policy['id']}", headers=auth(OPERATOR_TOKEN)
        )
        assert (await submit(client))["status"] == "rejected"
