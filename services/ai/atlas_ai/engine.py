# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""The AI engine: gather governed truth → propose → (maybe) plan.

The founding rules, enforced by structure:

- **Truth**: context comes only from Atlas Memory and the Device
  Manager, whose class policies already withhold what this service may
  not see (atlas.ai stewards nothing, so Class 3 is structurally
  unreadable). A defensive filter drops anything intimate that could
  ever slip through.
- **Action**: the ONLY consequence this service can have is submitting
  a plan to the Planner as ``atlas.ai`` — where default-deny policy,
  operator approval, and the audit trail apply. The Device Manager
  would refuse it directly anyway (M6); this engine doesn't even try.
- **Audit**: every interaction is recorded; bus events carry no prompt
  text (prompts are personal — Class 2 thinking applies to telemetry
  about thinking).
"""

from __future__ import annotations

import logging
import ssl

import httpx

from atlas_sdk import AtlasService, discover_service
from atlas_sdk.service_auth import Identity

from .backends import Proposal
from .store import AIStore, Interaction

log = logging.getLogger("atlas.ai")

CLASS_INTIMATE = 3


class AIEngine:
    def __init__(
        self,
        store: AIStore,
        atlas: AtlasService,
        backend,
        *,
        max_context_items: int = 100,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._atlas = atlas
        self._backend = backend
        self._max_items = max_context_items
        self._client = client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            ctx = self._atlas.client_ssl_context()
            self._client = httpx.AsyncClient(
                timeout=15.0,
                verify=ctx if ctx is not None else True,
                headers=(
                    {}
                    if self._atlas.security_mode == "mtls"
                    else {"Authorization": f"Bearer {self._atlas.service_token or ''}"}
                ),
            )
        return self._client

    async def _find(self, name: str) -> str | None:
        token, ssl_ctx = self._atlas.bus_credentials()
        try:
            services = await discover_service(
                core_url=self._atlas.core_url, token=token, ssl_context=ssl_ctx, name=name
            )
        except (httpx.HTTPError, ssl.SSLError):
            return None
        live = [s for s in services if s.get("status") in ("starting", "healthy")]
        return live[0]["address"].rstrip("/") if live else None

    # -- context: governed truth only -----------------------------------------

    async def gather_context(self) -> tuple[list[dict], list[dict]]:
        """(devices, facts) — everything the policies allow us to see,
        and nothing intimate even if something slipped."""
        devices: list[dict] = []
        facts: list[dict] = []

        devices_url = await self._find("atlas.devices")
        if devices_url:
            try:
                response = await self._http().get(f"{devices_url}/v1/devices")
                if response.status_code == 200:
                    devices = response.json()
            except (httpx.HTTPError, ssl.SSLError) as exc:
                log.warning("device context unavailable: %s", exc)

        memory_url = await self._find("atlas.memory")
        if memory_url:
            for namespace in ("system.services", "home.rooms"):
                try:
                    response = await self._http().get(
                        f"{memory_url}/v1/facts/{namespace}", params={"max_class": 2}
                    )
                    if response.status_code == 200:
                        facts.extend(
                            {"key": f"{namespace}/{f['key']}",
                             "payload": f["payload"], "class": f["class"]}
                            for f in response.json()
                        )
                except (httpx.HTTPError, ssl.SSLError) as exc:
                    log.warning("memory context unavailable: %s", exc)

        # Defense in depth: the upstream policies already redact/withhold
        # Class 3; enforce it locally too so a bug elsewhere cannot put
        # intimate data in front of a model.
        for device in devices:
            if device.get("class", 1) >= CLASS_INTIMATE:
                device["state"] = {"redacted": True}
        facts = [f for f in facts if f.get("class", 1) < CLASS_INTIMATE]

        return devices[: self._max_items], facts[: self._max_items]

    # -- ask -------------------------------------------------------------------

    async def ask(self, prompt: str, identity: Identity) -> Interaction:
        devices, facts = await self.gather_context()
        proposal: Proposal = await self._backend.propose(prompt, devices, facts)

        plan_id: str | None = None
        plan_status: str | None = None
        if proposal.kind == "plan" and proposal.actions:
            plan_id, plan_status = await self._submit_plan(prompt, proposal)
            if plan_id is None:
                proposal = Proposal(
                    kind="answer",
                    answer="I proposed a plan but the Planner is unavailable; "
                           "nothing was done.",
                    rationale=proposal.rationale,
                )

        interaction = await self._store.record(
            requester=identity.name,
            prompt=prompt,
            kind=proposal.kind,
            answer=proposal.answer,
            rationale=proposal.rationale,
            plan_id=plan_id,
            plan_status=plan_status,
            model=self._backend.name,
            context_size=len(devices) + len(facts),
        )
        # Bus event: metadata only — never the prompt, never the answer.
        await self._store.append_event(
            "ai.interaction",
            {"interaction_id": interaction.id, "requester": identity.name,
             "kind": proposal.kind, "model": self._backend.name,
             **({"plan_id": plan_id, "plan_status": plan_status} if plan_id else {})},
        )
        return interaction

    async def _submit_plan(
        self, prompt: str, proposal: Proposal
    ) -> tuple[str | None, str | None]:
        planner_url = await self._find("atlas.planner")
        if planner_url is None:
            return None, None
        try:
            response = await self._http().post(
                f"{planner_url}/v1/plans",
                json={"goal": f"[atlas.ai] {prompt[:900]}", "actions": proposal.actions},
            )
        except (httpx.HTTPError, ssl.SSLError) as exc:
            log.warning("planner unreachable: %s", exc)
            return None, None
        if response.status_code != 201:
            log.warning("planner refused submission: %s %s",
                        response.status_code, response.text[:200])
            return None, None
        plan = response.json()
        log.info("proposal submitted as plan %s (%s)", plan["id"], plan["status"])
        return plan["id"], plan["status"]
