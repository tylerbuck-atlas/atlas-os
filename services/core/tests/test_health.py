"""Health monitoring: heartbeat watchdog and active probes."""

from __future__ import annotations

from datetime import timedelta

import httpx

from atlas_core.models import ServiceStatus, utcnow

from .conftest import bootstrap_headers, register, token_headers


async def _force_stale_heartbeat(app, instance_id: str, seconds: int) -> None:
    await app.state.store.record_heartbeat(
        instance_id, utcnow() - timedelta(seconds=seconds)
    )


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeHttpClient:
    """Stands in for httpx.AsyncClient in probe tests."""

    def __init__(self, ok: bool) -> None:
        self.ok = ok

    async def get(self, url: str) -> _FakeResponse:
        if not self.ok:
            raise httpx.ConnectError("connection refused")
        return _FakeResponse(200)

    async def aclose(self) -> None:
        pass


class TestHeartbeatWatchdog:
    async def test_missed_heartbeats_mark_unreachable(self, app, client):
        body = await register(client)
        instance_id = body["service"]["instance_id"]

        # Window = interval(1s) * misses(2) = 2s. Go 10s stale.
        await _force_stale_heartbeat(app, instance_id, seconds=10)
        await app.state.health_monitor.check_heartbeats_once()

        record = await app.state.registry.get(instance_id)
        assert record.status == ServiceStatus.UNREACHABLE

    async def test_fresh_heartbeat_is_not_flagged(self, app, client):
        body = await register(client)
        await app.state.health_monitor.check_heartbeats_once()
        record = await app.state.registry.get(body["service"]["instance_id"])
        assert record.status == ServiceStatus.STARTING

    async def test_heartbeat_recovers_unreachable_service(self, app, client):
        body = await register(client)
        instance_id = body["service"]["instance_id"]

        await _force_stale_heartbeat(app, instance_id, seconds=10)
        await app.state.health_monitor.check_heartbeats_once()
        assert (await app.state.registry.get(instance_id)).status == ServiceStatus.UNREACHABLE

        response = await client.post(
            f"/v1/registry/services/{instance_id}/heartbeat",
            headers=token_headers(body["service_token"]),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
        assert (await app.state.registry.get(instance_id)).status == ServiceStatus.HEALTHY


class TestActiveProbes:
    async def test_successful_probe_marks_healthy(self, app, client):
        body = await register(client)
        instance_id = body["service"]["instance_id"]

        app.state.health_monitor._client = _FakeHttpClient(ok=True)
        await app.state.health_monitor.probe_all_once()

        assert (await app.state.registry.get(instance_id)).status == ServiceStatus.HEALTHY

    async def test_failing_probe_marks_unhealthy(self, app, client):
        body = await register(client)
        instance_id = body["service"]["instance_id"]

        app.state.health_monitor._client = _FakeHttpClient(ok=False)
        await app.state.health_monitor.probe_all_once()

        assert (await app.state.registry.get(instance_id)).status == ServiceStatus.UNHEALTHY

    async def test_probe_recovers_unhealthy_service(self, app, client):
        body = await register(client)
        instance_id = body["service"]["instance_id"]
        monitor = app.state.health_monitor

        monitor._client = _FakeHttpClient(ok=False)
        await monitor.probe_all_once()
        assert (await app.state.registry.get(instance_id)).status == ServiceStatus.UNHEALTHY

        monitor._client = _FakeHttpClient(ok=True)
        await monitor.probe_all_once()
        assert (await app.state.registry.get(instance_id)).status == ServiceStatus.HEALTHY

    async def test_probe_does_not_override_unreachable(self, app, client):
        """Unreachable means 'not heartbeating' — only a heartbeat clears it."""
        body = await register(client)
        instance_id = body["service"]["instance_id"]
        monitor = app.state.health_monitor

        await _force_stale_heartbeat(app, instance_id, seconds=10)
        await monitor.check_heartbeats_once()

        monitor._client = _FakeHttpClient(ok=True)
        await monitor.probe_all_once()
        assert (await app.state.registry.get(instance_id)).status == ServiceStatus.UNREACHABLE

    async def test_status_visible_in_discovery(self, app, client):
        body = await register(client)
        app.state.health_monitor._client = _FakeHttpClient(ok=True)
        await app.state.health_monitor.probe_all_once()

        response = await client.get(
            "/v1/registry/services", headers=bootstrap_headers()
        )
        assert response.json()[0]["status"] == "healthy"
        assert body["service"]["instance_id"] == response.json()[0]["instance_id"]
