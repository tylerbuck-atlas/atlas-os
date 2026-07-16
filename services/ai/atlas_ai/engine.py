# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""The assist pipeline: gather truth → reason → propose → record.

The founding rules, as executed code paths:

1. **Truth first.** The model sees only what TruthGatherer returns —
   governed, class-filtered, provenance-labeled data from Memory and
   the Device Manager. Nothing else enters the context.
2. **Proposals, not actions.** If the backend proposes actions, they are
   submitted to the Planner as a plan in atlas.ai's name. Default-deny
   policy, operator approval, and the audit trail all apply. This
   service holds no other client — it *cannot* invoke a capability
   directly, because no such code path exists.
3. **Everything on the record.** Every assist is stored (requester,
   prompt, answer, plan) and announced on the bus without prompt
   content.
"""

from __future__ import annotations

import logging
import ssl

import httpx

from atlas_sdk import AtlasService, discover_service

from .backends import InferenceResult
from .store import AIStore, AssistRecord
from .truth import TruthGatherer

log = logging.getLogger("atlas.ai")


class AssistEngine:
    def __init__(
        self,
        store: AIStore,
        atlas: AtlasService,
        gatherer: TruthGatherer,
        backend,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._atlas = atlas
        self._gatherer = gatherer
        self._backend = backend
        self._client = client

    async def close(self) -> None:
        await self._gatherer.close()
        if hasattr(self._backend, "close"):
            await self._backend.close()
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

    async def assist(self, prompt: str, *, requester: str) -> AssistRecord:
        truth = await self._gatherer.gather()
        result: InferenceResult = await self._backend.infer(prompt, truth)

        plan_id: str | None = None
        plan_status: str | None = None
        if result.proposed_actions:
            plan_id, plan_status = await self._submit_plan(prompt, result)

        record = await self._store.record_assist(
            requester=requester,
            prompt=prompt,
            backend=self._backend.name,
            answer=result.answer,
            sources=truth.get("sources", []),
            plan_id=plan_id,
            plan_status=plan_status,
        )
        # On the bus: the fact of an assist, never its content.
        await self._store.append_event(
            "ai.assist.completed",
            {
                "assist_id": record.id,
                "requester": requester,
                "backend": self._backend.name,
                "proposed_actions": len(result.proposed_actions),
                "plan_id": plan_id,
                "plan_status": plan_status,
            },
        )
        return record

    async def _submit_plan(
        self, prompt: str, result: InferenceResult
    ) -> tuple[str | None, str | None]:
        """The ONLY route from model output toward the world."""
        planner = await self._find("atlas.planner")
        if planner is None:
            log.warning("planner unavailable; proposal dropped")
            return None, "planner_unavailable"
        try:
            response = await self._http().post(
                f"{planner}/v1/plans",
                json={
                    "goal": f"[atlas.ai] {prompt[:900]}",
                    "actions": [a.model_dump() for a in result.proposed_actions],
                },
            )
        except (httpx.HTTPError, ssl.SSLError) as exc:
            log.warning("plan submission failed: %s", exc)
            return None, "submission_failed"
        if response.status_code != 201:
            log.warning("planner refused submission: %s", response.status_code)
            return None, f"refused_{response.status_code}"
        plan = response.json()
        log.info(
            "proposed plan %s -> %s (%d action(s))",
            plan["id"], plan["status"], len(result.proposed_actions),
        )
        return plan["id"], plan["status"]

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
