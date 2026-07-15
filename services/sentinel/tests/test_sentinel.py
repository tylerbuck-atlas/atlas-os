# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Sentinel: anomaly rules, dedup, alert store, API authz."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from atlas_sentinel.api.routes import router
from atlas_sentinel.config import SentinelConfig
from atlas_sentinel.rules import RuleEngine
from atlas_sentinel.store import SentinelStore
from atlas_sdk.service_auth import Identity

SERVICE_TOKEN = "tok-service"
OPERATOR_TOKEN = "tok-operator"

IDENTITIES = {
    SERVICE_TOKEN: Identity("atlas.other", "o-1", "0.1.0"),
    OPERATOR_TOKEN: Identity("atlas.operator", "manual-1", "cert"),
}


class FakeIntrospector:
    async def introspect(self, token):
        return IDENTITIES.get(token)

    async def close(self):
        pass


def down_event(name="atlas.echo", to="unreachable", reason="no heartbeat"):
    return {
        "topic": "registry.service.status_changed",
        "payload": {"name": name, "to": to, "reason": reason},
    }


def rejection_event(requester="atlas.ai"):
    return {
        "topic": "planner.plan.rejected",
        "payload": {"requester": requester, "reason": "default-deny"},
    }


class TestRules:
    def test_service_down_raises(self):
        rules = RuleEngine()
        alerts = rules.evaluate(down_event(to="unreachable"), now=0.0)
        assert len(alerts) == 1
        assert alerts[0].kind == "service.down"
        assert alerts[0].severity == "critical"

    def test_unhealthy_is_warning(self):
        rules = RuleEngine()
        alerts = rules.evaluate(down_event(to="unhealthy"), now=0.0)
        assert alerts[0].severity == "warning"

    def test_healthy_transition_is_quiet(self):
        rules = RuleEngine()
        assert rules.evaluate(down_event(to="healthy"), now=0.0) == []

    def test_dedup_within_cooldown(self):
        rules = RuleEngine(cooldown_seconds=60)
        assert len(rules.evaluate(down_event(), now=0.0)) == 1
        assert rules.evaluate(down_event(), now=10.0) == []      # suppressed
        assert len(rules.evaluate(down_event(), now=70.0)) == 1  # cooldown over

    def test_flapping_detection(self):
        rules = RuleEngine(flap_threshold=4, flap_window_seconds=60, cooldown_seconds=0)
        alerts = []
        for i in range(4):
            alerts += rules.evaluate(down_event(to="healthy" if i % 2 else "unhealthy"),
                                     now=float(i))
        kinds = [a.kind for a in alerts]
        assert "service.flapping" in kinds

    def test_flapping_window_expires(self):
        rules = RuleEngine(flap_threshold=4, flap_window_seconds=60)
        for i in range(3):
            rules.evaluate(down_event(to="healthy"), now=float(i))
        # 4th transition far outside the window: no flap alert
        alerts = rules.evaluate(down_event(to="healthy"), now=1000.0)
        assert all(a.kind != "service.flapping" for a in alerts)

    def test_rejection_and_probing(self):
        rules = RuleEngine(rejection_threshold=3, rejection_window_seconds=300,
                           cooldown_seconds=0)
        alerts = []
        for i in range(3):
            alerts += rules.evaluate(rejection_event(), now=float(i))
        kinds = [a.kind for a in alerts]
        assert kinds.count("policy.rejection") == 3
        assert "policy.probing" in kinds
        probing = next(a for a in alerts if a.kind == "policy.probing")
        assert probing.severity == "critical"

    def test_unrelated_topics_ignored(self):
        rules = RuleEngine()
        assert rules.evaluate({"topic": "memory.fact.changed", "payload": {}}, now=0.0) == []


@pytest.fixture
async def app():
    config = SentinelConfig(
        bootstrap_token="test-bootstrap-token-1234",
        database_path=":memory:",
        security_mode="token",
        log_level="WARNING",
    )
    application = FastAPI()
    application.include_router(router)
    application.state.config = config
    store = SentinelStore(config.database_path)
    await store.open()
    application.state.store = store
    application.state.introspector = FakeIntrospector()
    yield application
    await store.close()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://atlas-sentinel") as c:
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAlertAPI:
    async def test_alerts_visible_and_ackable(self, app, client):
        alert = await app.state.store.raise_alert(
            kind="service.down", subject="atlas.echo",
            severity="critical", detail="unreachable",
        )
        listing = await client.get("/v1/alerts", headers=auth(SERVICE_TOKEN))
        assert [a["id"] for a in listing.json()] == [alert.id]

        acked = await client.post(
            f"/v1/alerts/{alert.id}/ack", headers=auth(OPERATOR_TOKEN)
        )
        assert acked.json()["acknowledged"] is True
        assert acked.json()["acked_by"] == "atlas.operator"

        # Acked alerts leave the default view.
        assert (await client.get("/v1/alerts", headers=auth(SERVICE_TOKEN))).json() == []

    async def test_only_operator_acks(self, app, client):
        alert = await app.state.store.raise_alert(
            kind="x", subject="y", severity="info", detail="z"
        )
        response = await client.post(
            f"/v1/alerts/{alert.id}/ack", headers=auth(SERVICE_TOKEN)
        )
        assert response.status_code == 403

    async def test_requires_auth(self, client):
        assert (await client.get("/v1/alerts")).status_code == 401


class TestWatcherIntegration:
    async def test_handle_event_persists_and_publishes(self, app):
        from atlas_sentinel.rules import RuleEngine
        from atlas_sentinel.watcher import Watcher

        watcher = Watcher(app.state.store, RuleEngine(), atlas=None)
        raised = await watcher.handle_event(down_event())
        assert raised == 1
        alerts = await app.state.store.list_alerts()
        assert alerts[0].kind == "service.down"
        events = await app.state.store.list_events_after(0, 10)
        assert events[0][1] == "sentinel.alert.raised"
