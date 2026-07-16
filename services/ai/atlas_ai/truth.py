# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Truth gathering: the ONLY data the model ever sees.

The AI reads from governed truth sources — Atlas Memory and the Device
Manager — using **its own identity**. That sentence is the privacy
model: the OS enforces what the model can see, not the prompt. As a
non-steward, atlas.ai receives redacted state for Class-3 devices, its
Memory queries are capped at Class 2 explicitly *and* filtered
server-side, and nothing here can override that from the client side.

Everything gathered is labeled with its provenance so answers can cite
where knowledge came from.
"""

from __future__ import annotations

import logging
import ssl

import httpx

from atlas_sdk import AtlasService, discover_service

log = logging.getLogger("atlas.ai.truth")

MAX_MODEL_CLASS = 2  # Class 3 (intimate) never reaches any model. Ever.


class TruthGatherer:
    def __init__(
        self,
        atlas: AtlasService,
        *,
        fact_namespaces: list[str],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._atlas = atlas
        self._namespaces = fact_namespaces
        self._client = client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            ctx = self._atlas.client_ssl_context()
            self._client = httpx.AsyncClient(
                timeout=10.0,
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

    async def gather(self) -> dict:
        """Assemble the grounded context: facts + devices + provenance."""
        truth: dict = {"facts": {}, "devices": [], "sources": []}

        memory = await self._find("atlas.memory")
        if memory:
            for namespace in self._namespaces:
                try:
                    response = await self._http().get(
                        f"{memory}/v1/facts/{namespace}",
                        params={"max_class": MAX_MODEL_CLASS},
                    )
                    if response.status_code == 200:
                        facts = response.json()
                        truth["facts"][namespace] = {
                            f["key"]: {
                                "payload": f["payload"],
                                "provenance": f["provenance"],
                                "version": f["version"],
                            }
                            for f in facts
                        }
                        truth["sources"].append(
                            f"atlas.memory:{namespace} ({len(facts)} facts, class<={MAX_MODEL_CLASS})"
                        )
                except (httpx.HTTPError, ssl.SSLError) as exc:
                    log.warning("memory gather failed for %s: %s", namespace, exc)

        devices = await self._find("atlas.devices")
        if devices:
            try:
                response = await self._http().get(f"{devices}/v1/devices")
                if response.status_code == 200:
                    records = response.json()
                    # Server-side redaction already applied for our identity;
                    # belt-and-braces: drop Class 3 entries entirely.
                    records = [d for d in records if d.get("class", 1) <= MAX_MODEL_CLASS]
                    truth["devices"] = records
                    truth["sources"].append(
                        f"atlas.devices ({len(records)} devices, class<={MAX_MODEL_CLASS})"
                    )
            except (httpx.HTTPError, ssl.SSLError) as exc:
                log.warning("device gather failed: %s", exc)

        return truth
