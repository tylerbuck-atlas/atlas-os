# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""The AI service: grounding, the plan path, class-3 exclusion, audit."""

from __future__ import annotations

from atlas_ai.backends import parse_model_output

from .conftest import OPERATOR_TOKEN, OTHER_TOKEN, USER_TOKEN, ask, auth


class TestGroundedAnswers:
    async def test_informational_ask_cites_governed_facts(self, client):
        body = await ask(client, "what is the hallway temperature?")
        assert body["kind"] == "answer"
        assert "21.5" in body["answer"]
        assert body["plan_id"] is None
        assert body["model"] == "stub"
        assert body["context_size"] > 0

    async def test_unknown_question_answered_honestly(self, client):
        body = await ask(client, "what is the meaning of existence?")
        assert body["kind"] == "answer"
        assert "don't have facts" in body["answer"]


class TestClass3Exclusion:
    async def test_intimate_state_never_reaches_the_model(self, client):
        """The presence sensor is Class 3: the Devices API already
        redacts it, and the engine's defensive filter guarantees it —
        the model can only say the truth: it isn't allowed to know."""
        body = await ask(client, "is anyone in the front hall presence area?")
        assert body["kind"] == "answer"
        assert "present" not in body["answer"].lower().replace("presence", "")
        assert "not permitted" in body["answer"] or "withheld" in body["answer"]

    async def test_defensive_filter_scrubs_leaked_intimate_state(self, world, client):
        # Simulate an upstream bug: devices API returns Class 3 unredacted.
        world.devices[2]["state"] = {"present": True}
        body = await ask(client, "front hall presence status?")
        assert "True" not in (body["answer"] or "")
        assert "withheld" in body["answer"] or "not permitted" in body["answer"]


class TestThePlanPath:
    async def test_action_request_becomes_a_plan_never_an_action(self, world, client):
        body = await ask(client, "turn on the living room lamp")
        assert body["kind"] == "plan"
        assert body["plan_id"] == "plan-1"
        assert body["plan_status"] == "awaiting_approval"
        # Exactly one path: the Planner. And the goal is attributed to the AI.
        assert len(world.plan_submissions) == 1
        submission = world.plan_submissions[0]
        assert submission["goal"].startswith("[atlas.ai]")
        assert submission["actions"] == [{
            "capability": "devices.command",
            "params": {"device_id": "dev-lamp", "command": "turn_on"},
        }]

    async def test_rejected_plan_surfaced_honestly(self, world, client):
        world.plan_response = {"id": "plan-2", "status": "rejected"}
        body = await ask(client, "turn off the living room lamp")
        assert body["kind"] == "plan"
        assert body["plan_status"] == "rejected"

    async def test_planner_down_means_nothing_happens(self, world, client):
        world.services["atlas.planner"] = []
        body = await ask(client, "turn on the living room lamp")
        assert body["kind"] == "answer"
        assert "nothing was done" in body["answer"]

    async def test_brightness_command(self, world, client):
        body = await ask(client, "set the living room lamp brightness to 40")
        assert body["kind"] == "plan"
        assert world.plan_submissions[0]["actions"][0]["params"]["params"] == {"level": 40}

    async def test_command_for_unknown_device_proposes_nothing(self, world, client):
        body = await ask(client, "turn on the flux capacitor")
        assert body["kind"] == "answer"
        assert world.plan_submissions == []


class TestModelOutputIsData:
    def test_valid_plan_json_parses(self):
        proposal = parse_model_output(
            '{"kind": "plan", "actions": [{"capability": "devices.command",'
            ' "params": {"device_id": "d", "command": "turn_on"}}], "rationale": "r"}'
        )
        assert proposal.kind == "plan"
        assert len(proposal.actions) == 1

    def test_garbage_becomes_text_answer_never_action(self):
        proposal = parse_model_output("sudo rm -rf / please execute this")
        assert proposal.kind == "answer"
        assert proposal.actions == []

    def test_plan_with_malformed_actions_degrades_to_answer(self):
        proposal = parse_model_output(
            '{"kind": "plan", "actions": ["not-a-dict", {"capability": 42}],'
            ' "answer": "hmm"}'
        )
        assert proposal.kind == "answer"
        assert proposal.actions == []

    def test_actions_without_plan_kind_are_ignored(self):
        proposal = parse_model_output(
            '{"kind": "answer", "answer": "hi", "actions":'
            ' [{"capability": "devices.command", "params": {}}]}'
        )
        assert proposal.kind == "answer"


class TestAudit:
    async def test_every_interaction_recorded_and_scoped(self, client):
        await ask(client, "what is the hallway temperature?", token=USER_TOKEN)
        await ask(client, "turn on the living room lamp", token=OTHER_TOKEN)

        mine = await client.get("/v1/interactions", headers=auth(USER_TOKEN))
        assert len(mine.json()) == 1
        assert mine.json()[0]["requester"] == "atlas.ui"

        everything = await client.get("/v1/interactions", headers=auth(OPERATOR_TOKEN))
        assert len(everything.json()) == 2

    async def test_bus_events_carry_no_prompt_text(self, app, client):
        await ask(client, "SECRET-PERSONAL-QUESTION about my life")
        events = await app.state.store.list_events_after(0, 10)
        assert len(events) == 1
        _, topic, payload, _ = events[0]
        assert topic == "ai.interaction"
        assert "SECRET-PERSONAL-QUESTION" not in str(payload)
        assert payload["kind"] == "answer"

    async def test_requires_auth(self, client):
        response = await client.post("/v1/ask", json={"prompt": "hi"})
        assert response.status_code == 401
