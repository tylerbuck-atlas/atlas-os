# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Model backends.

The model is a replaceable component behind a small protocol — exactly
like every other Atlas backend. Two implementations ship:

- **OllamaBackend** — local inference on the operator's own hardware
  (privacy contract: local is the default; the model URL is an explicit,
  visible grant).
- **StubBackend** — deterministic keyword reasoning. No model at all;
  used for tests, CI, and first boot so the OS never *requires* a model
  to exist. Honest about being a stub.

A cloud backend is deliberately absent. The privacy contract permits one
only as an explicit opt-in adapter that can never receive Class 3 data;
implementing it is a conscious future decision, not a default.

Whatever the backend replies is a **proposal** — parsed, never executed.
Malformed output degrades to an informational answer; hallucinated
actions die in the Planner's default-deny. The pipes, not the prompt,
enforce safety.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("atlas.ai.backend")


@dataclass
class Proposal:
    kind: str  # "answer" | "plan"
    answer: str | None = None
    actions: list[dict] = field(default_factory=list)
    rationale: str = ""


class StubBackend:
    """Deterministic, model-free reasoning over the gathered context.

    Understands two things: device commands ("turn on/off X",
    "set X brightness to N") and simple lookups over facts. Everything
    else gets an honest 'I need a real model for that.'
    """

    name = "stub"

    async def propose(self, goal: str, devices: list[dict], facts: list[dict]) -> Proposal:
        lower = goal.lower()

        m = re.search(r"\bset\s+(?:the\s+)?(.+?)\s+brightness\s+to\s+(\d+)", lower)
        if m:
            device = self._find_device(devices, m.group(1), "set_brightness")
            if device:
                return Proposal(
                    kind="plan",
                    actions=[{
                        "capability": "devices.command",
                        "params": {"device_id": device["id"], "command": "set_brightness",
                                   "params": {"level": int(m.group(2))}},
                    }],
                    rationale=f"Set {device['name']!r} brightness to {m.group(2)}%.",
                )

        m = re.search(r"\bturn\s+(on|off)\s+(?:the\s+)?(.+)", lower)
        if m:
            command = f"turn_{m.group(1)}"
            device = self._find_device(devices, m.group(2).strip(" .?!"), command)
            if device:
                return Proposal(
                    kind="plan",
                    actions=[{
                        "capability": "devices.command",
                        "params": {"device_id": device["id"], "command": command},
                    }],
                    rationale=f"{command} on {device['name']!r} via its adapter.",
                )
            return Proposal(
                kind="answer",
                answer="I could not find an online device matching that description "
                       "which accepts that command.",
                rationale="no matching device",
            )

        # Informational: surface matching facts/devices, honestly.
        hits: list[str] = []
        for device in devices:
            haystack = f"{device.get('name','')} {device.get('kind','')} {device.get('room','')}".lower()
            if any(w in haystack for w in lower.split() if len(w) > 3):
                state = device.get("state", {})
                if state.get("redacted"):
                    hits.append(
                        f"{device['name']}: state withheld (Class {device.get('class')} — "
                        "I am not permitted to read it)"
                    )
                else:
                    hits.append(f"{device['name']}: {state}")
        for fact in facts:
            if any(w in fact.get("key", "").lower() for w in lower.split() if len(w) > 3):
                hits.append(f"{fact['key']}: {fact.get('payload')}")
        if hits:
            return Proposal(kind="answer",
                            answer="From the household's records: " + "; ".join(hits[:5]),
                            rationale="fact lookup")
        return Proposal(
            kind="answer",
            answer="I don't have facts matching that question, and the stub backend "
                   "cannot reason beyond lookups — configure a local model "
                   "(ATLAS_AI_BACKEND=ollama) for open-ended questions.",
            rationale="stub limitation",
        )

    @staticmethod
    def _find_device(devices: list[dict], fragment: str, command: str) -> dict | None:
        fragment = fragment.strip()
        for device in devices:
            if not device.get("online", True):
                continue
            if command not in device.get("commands", []):
                continue
            if fragment in device.get("name", "").lower():
                return device
        return None


class OllamaBackend:
    """Local inference via an Ollama server on the operator's hardware."""

    def __init__(self, *, url: str, model: str, timeout: float = 120.0) -> None:
        self._url = url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self.name = f"ollama:{model}"

    _SYSTEM = (
        "You are Atlas, the reasoning service of a home operating system. "
        "You are NOT the source of truth: answer ONLY from the provided facts "
        "and devices; if they do not contain the answer, say so. You cannot "
        "act directly: to act, propose a plan; every plan is validated against "
        "operator policy before anything runs.\n"
        "Respond ONLY with a JSON object: "
        '{"kind": "answer"|"plan", "answer": string|null, '
        '"actions": [{"capability": "devices.command", "params": '
        '{"device_id": string, "command": string, "params": object}}], '
        '"rationale": string}. '
        "Use device ids and commands exactly as given; never invent them."
    )

    async def propose(self, goal: str, devices: list[dict], facts: list[dict]) -> Proposal:
        context = json.dumps({"devices": devices, "facts": facts}, default=str)[:12000]
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._url}/api/chat",
                    json={
                        "model": self._model,
                        "stream": False,
                        "format": "json",
                        "messages": [
                            {"role": "system", "content": self._SYSTEM},
                            {"role": "user",
                             "content": f"CONTEXT:\n{context}\n\nREQUEST: {goal}"},
                        ],
                    },
                )
                response.raise_for_status()
                content = response.json()["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("model backend unavailable: %s", exc)
            return Proposal(kind="answer",
                            answer=f"The local model is unavailable ({exc}).",
                            rationale="backend error")
        return parse_model_output(content)


def parse_model_output(content: str) -> Proposal:
    """Model output is DATA. Parse defensively; on any deviation, degrade
    to an informational answer — malformed output can never become action."""
    try:
        data = json.loads(content)
        assert isinstance(data, dict)
    except (json.JSONDecodeError, AssertionError):
        return Proposal(kind="answer", answer=content[:2000],
                        rationale="model output was not valid JSON; treated as text")
    kind = data.get("kind")
    actions = data.get("actions") or []
    valid_actions = [
        a for a in actions
        if isinstance(a, dict) and isinstance(a.get("capability"), str)
        and isinstance(a.get("params"), dict)
    ]
    if kind == "plan" and valid_actions:
        return Proposal(kind="plan", actions=valid_actions,
                        rationale=str(data.get("rationale", ""))[:1000])
    return Proposal(
        kind="answer",
        answer=str(data.get("answer") or content)[:2000],
        rationale=str(data.get("rationale", ""))[:1000],
    )
