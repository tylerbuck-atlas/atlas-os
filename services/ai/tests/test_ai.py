# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""The AI service: grounded, gated, and incapable of acting directly."""

from __future__ import annotations

import httpx
import pytest

from atlas_ai.backends import (
    BuiltinBackend,
    InferenceResult,
    OllamaBackend,
    parse_model_output,
)

from .conftest import LIGHT_ID, OPERATOR_TOKEN, USER_TOKEN, assist, auth


class TestFoundingRules:
    async def test_proposals_become_plans_never_invocations(self, client, world):
        """The constitutional test: model output leads to a PLAN, and the
        AI never touches an invoke endpoint or the device manager's
        command API — no such call exists in its traffic."""
        record = await assist(client, "turn on the living room lamp")

        assert record["plan_id"] == "plan-1"
        assert record["plan_status"] == "awaiting_approval"
        assert world.submitted_plans == [{
            "goal": "[atlas.ai] turn on the living room lamp",
            "actions": [{"capability": "devices.command",
                         "params": {"device_id": LIGHT_ID, "command": "turn_on"}}],
        }]
        # The proof: every call the AI made, none of them act.
        for call in world.calls:
            assert "/v1/invoke/" not in call["path"], call
            assert not (call["host"] == "devices.test" and call["method"] == "POST"), call

    async def test_gathering_is_class_capped(self, client, world):
        """Truth requests explicitly cap at Class 2 — and Class 3 devices
        are dropped even if a server ever mis-served them."""
        await assist(client, "status")
        memory_calls = [c for c in world.calls if c["host"] == "memory.test"]
        assert memory_calls, "AI must gather from memory"
        for call in memory_calls:
            assert call["params"].get("max_class") == "2", call

    async def test_class3_device_never_reaches_the_backend(self, app, world):
        gathered = await app.state.engine._gatherer.gather()
        names = [d["name"] for d in gathered["devices"]]
        assert "Living room lamp" in names
        assert "Front hall presence" not in names  # class 3: excluded entirely

    async def test_answer_cites_sources(self, client):
        record = await assist(client, "what's the status of the house")
        assert "atlas.memory:system.services" in " ".join(record["sources"])
        assert "atlas.devices" in " ".join(record["sources"])

    async def test_no_actions_no_plan(self, client, world):
        record = await assist(client, "what's the status of the house")
        assert record["plan_id"] is None
        assert world.submitted_plans == []


class TestBuiltinBackend:
    async def test_unknown_device_degrades_to_answer(self, client, world):
        record = await assist(client, "turn on the flux capacitor")
        assert record["plan_id"] is None
        assert "couldn't find" in record["answer"]
        assert world.submitted_plans == []

    async def test_unsupported_command_degrades(self, world):
        backend = BuiltinBackend()
        truth = {"devices": [{"id": "x", "name": "Thermo", "commands": ["set_temp"]}]}
        result = await backend.infer("turn on the thermo", truth)
        assert result.proposed_actions == []
        assert "does not accept" in result.answer

    async def test_status_summarizes_truth(self, client):
        record = await assist(client, "how is the system")
        assert "1 service(s) healthy" in record["answer"]
        assert "Living room lamp" in record["answer"]

    async def test_freeform_prompts_point_to_local_model(self, client):
        record = await assist(client, "write me a poem about the house")
        assert record["plan_id"] is None
        assert "ollama" in record["answer"].lower()


class TestModelOutputParsing:
    def test_valid_json_parsed(self):
        result = parse_model_output(
            '{"answer": "ok", "proposed_actions": '
            '[{"capability": "devices.command", "params": {"device_id": "d", "command": "turn_on"}}]}'
        )
        assert result.proposed_actions[0].capability == "devices.command"

    def test_garbage_degrades_to_answer_only(self):
        result = parse_model_output("I think you should <exec>rm -rf /</exec>")
        assert result.proposed_actions == []
        assert "exec" in result.answer  # preserved as text, powerless

    def test_wrong_shape_degrades(self):
        result = parse_model_output('{"answer": "hi", "proposed_actions": "all of them"}')
        assert result.proposed_actions == []

    def test_oversized_action_list_rejected(self):
        actions = ",".join(
            '{"capability": "devices.command", "params": {}}' for _ in range(11)
        )
        result = parse_model_output(f'{{"answer": "x", "proposed_actions": [{actions}]}}')
        assert result.proposed_actions == []  # 11 > max 10 → degrade, don't truncate


class TestOllamaBackend:
    async def test_good_response_parsed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"message": {"content":
                '{"answer": "hello from the model", "proposed_actions": []}'}})

        backend = OllamaBackend(
            url="http://ollama.test", model="m",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        result = await backend.infer("hi", {})
        assert result == InferenceResult(answer="hello from the model")

    async def test_unreachable_backend_fails_safe(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        backend = OllamaBackend(
            url="http://ollama.test", model="m",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        result = await backend.infer("hi", {})
        assert result.proposed_actions == []
        assert "unavailable" in result.answer


class TestAuditAndAuthz:
    async def test_assist_requires_identity(self, client):
        response = await client.post("/v1/assist", json={"prompt": "hi"})
        assert response.status_code == 401

    async def test_audit_scoped_to_requester(self, client):
        await assist(client, "status", token=USER_TOKEN)
        own = await client.get("/v1/assists", headers=auth(USER_TOKEN))
        assert len(own.json()) == 1
        assert own.json()[0]["requester"] == "atlas.ui"

        all_view = await client.get("/v1/assists", headers=auth(OPERATOR_TOKEN))
        assert len(all_view.json()) == 1

    async def test_bus_event_has_no_prompt_content(self, app, client):
        await assist(client, "my secret plans for the weekend")
        events = await app.state.store.list_events_after(0, 10)
        assert events[0][1] == "ai.assist.completed"
        assert "secret" not in str(events)
