# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Inference backends — where the model runs, always at arm's length.

Per the privacy contract (docs/privacy.md): **local inference is the
default**. Two backends ship:

- ``builtin`` — a deterministic, zero-model reasoner. No network, no
  weights, no surprises: it parses a handful of household intents and
  summarizes gathered truth. Atlas is fully functional without any LLM.
- ``ollama`` — local inference against an Ollama server on the
  operator's own hardware. Reaching it is an *explicit local-inference
  grant* declared in Compose; the model still only sees gathered truth.

Cloud backends are deliberately absent from v1: the privacy contract
allows them only as opt-in adapters, never eligible for Class-3 data,
and none is implemented until someone consciously builds that adapter.

Whatever any backend returns is DATA. It is parsed strictly into
:class:`InferenceResult`; malformed output degrades to answer-only —
never into action.
"""

from __future__ import annotations

import json
import logging
import re

import httpx
from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger("atlas.ai.backend")


class ActionProposal(BaseModel):
    capability: str = Field(max_length=200)
    params: dict = Field(default_factory=dict)


class InferenceResult(BaseModel):
    answer: str = Field(max_length=8000)
    proposed_actions: list[ActionProposal] = Field(default_factory=list, max_length=10)


# -- builtin (deterministic, zero-model) ---------------------------------------

_TURN = re.compile(r"\bturn\s+(on|off)\s+(?:the\s+)?(.+?)\s*$", re.IGNORECASE)
_STATUS = re.compile(r"\b(status|health|how\s+is|what.?s\s+(going\s+on|the\s+state))\b", re.IGNORECASE)


def _match_device(name_fragment: str, devices: list[dict]) -> dict | None:
    fragment = name_fragment.strip().lower()
    for device in devices:
        if fragment == device.get("name", "").lower():
            return device
    for device in devices:
        if fragment in device.get("name", "").lower():
            return device
    return None


class BuiltinBackend:
    """No model at all — legible, testable household intents."""

    name = "builtin"

    async def infer(self, prompt: str, truth: dict) -> InferenceResult:
        devices = truth.get("devices", [])

        match = _TURN.search(prompt)
        if match:
            verb, target = match.group(1).lower(), match.group(2)
            device = _match_device(target, devices)
            if device is None:
                known = ", ".join(d["name"] for d in devices) or "none I can see"
                return InferenceResult(
                    answer=f"I couldn't find a device matching {target!r}. "
                           f"Devices I can see: {known}."
                )
            command = f"turn_{verb}"
            if command not in device.get("commands", []):
                return InferenceResult(
                    answer=f"{device['name']} does not accept {command!r} "
                           f"(accepts: {device.get('commands', [])})."
                )
            return InferenceResult(
                answer=f"Proposing to {verb.replace('_', ' ')} {device['name']} — "
                       "the Planner and your policies decide whether it happens.",
                proposed_actions=[ActionProposal(
                    capability="devices.command",
                    params={"device_id": device["id"], "command": command},
                )],
            )

        if _STATUS.search(prompt):
            services = truth.get("facts", {}).get("system.services", {})
            up = [k for k, v in services.items() if v["payload"].get("status") == "healthy"]
            down = [k for k, v in services.items() if v["payload"].get("status") != "healthy"]
            device_bits = [
                f"{d['name']}: {json.dumps(d.get('state', {}))}" for d in devices[:10]
            ]
            parts = [f"{len(up)} service(s) healthy" + (f", attention needed: {down}" if down else "")]
            if device_bits:
                parts.append("devices — " + "; ".join(device_bits))
            sources = truth.get("sources", [])
            return InferenceResult(
                answer=". ".join(parts) + f" (sources: {', '.join(sources)})"
            )

        return InferenceResult(
            answer="I can report household status and propose device commands "
                   "(e.g. 'turn on the living room lamp'). For open-ended "
                   "reasoning, configure a local model: ATLAS_AI_BACKEND=ollama "
                   "(docs/ai.md)."
        )


# -- ollama (local inference) -----------------------------------------------------

_SYSTEM_PROMPT = """You are Atlas AI, the reasoning service of a home operating system.
You will receive GROUNDED TRUTH (facts and devices) and a user request.
Rules you cannot break (the OS enforces them anyway):
- Only the provided truth is real. Never invent devices, states, or facts.
- You cannot act. You may only PROPOSE actions; a policy engine and a human decide.
Respond with ONLY a JSON object: {"answer": "<text for the user>",
"proposed_actions": [{"capability": "devices.command",
"params": {"device_id": "<id from truth>", "command": "<command from that device's list>"}}]}
Use an empty proposed_actions list when no action is needed."""


class OllamaBackend:
    """Local model via Ollama — the operator's own hardware."""

    name = "ollama"

    def __init__(
        self, *, url: str, model: str, timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def infer(self, prompt: str, truth: dict) -> InferenceResult:
        try:
            response = await self._client.post(
                f"{self._url}/api/chat",
                json={
                    "model": self._model,
                    "stream": False,
                    "format": "json",
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": (
                            "GROUNDED TRUTH:\n" + json.dumps(truth, default=str)
                            + "\n\nREQUEST:\n" + prompt
                        )},
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("ollama backend unavailable: %s", exc)
            return InferenceResult(
                answer="The local model backend is unavailable "
                       f"({type(exc).__name__}); no reasoning was performed."
            )
        return parse_model_output(content)


def parse_model_output(content: str) -> InferenceResult:
    """Model output is data: parse strictly, degrade safely."""
    try:
        raw = json.loads(content)
        return InferenceResult.model_validate(raw)
    except (json.JSONDecodeError, ValidationError):
        log.warning("model output failed strict parsing; degrading to answer-only")
        return InferenceResult(answer=content[:8000], proposed_actions=[])
