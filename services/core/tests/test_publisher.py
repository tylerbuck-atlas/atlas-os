"""Outbox publisher: Core's event log → Event Bus, at-least-once, in order."""

from __future__ import annotations

import httpx

from atlas_core.publisher import CURSOR_KEY, EventPublisher

from .conftest import REGISTRATION, register


class BusStub:
    """Collects published events; can be told to fail."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(503, text="bus down")
        import json

        self.received.append(json.loads(request.content))
        return httpx.Response(201, json={"ok": True})


async def make_publisher(app, stub: BusStub) -> EventPublisher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(stub.handler))
    return EventPublisher(
        app.state.store, app.state.registry, app.state.config, client=client
    )


async def register_bus(client) -> dict:
    return await register(client, {
        "name": "atlas.eventbus",
        "version": "0.1.0",
        "address": "http://atlas-eventbus:8200",
        "health_url": "http://atlas-eventbus:8200/healthz",
        "capabilities": ["eventbus.publish"],
        "metadata": {},
    })


class TestEventPublisher:
    async def test_no_bus_registered_publishes_nothing(self, app, client):
        await register(client)  # creates events, but no bus exists
        stub = BusStub()
        publisher = await make_publisher(app, stub)
        assert await publisher.publish_once() == 0
        assert stub.received == []

    async def test_publishes_backlog_in_order_once_bus_appears(self, app, client):
        await register(client)          # echo events land in the outbox
        await register_bus(client)      # now a bus is registered
        stub = BusStub()
        publisher = await make_publisher(app, stub)

        published = await publisher.publish_once()
        assert published >= 2  # echo registration + bus registration events
        topics = [e["topic"] for e in stub.received]
        assert topics[0] == "registry.service.registered"
        # Order preserved: registered events for echo before bus.
        names = [
            e["payload"].get("name")
            for e in stub.received
            if e["topic"] == "registry.service.registered"
        ]
        assert names == ["atlas.echo", "atlas.eventbus"]

    async def test_cursor_survives_and_prevents_duplicates(self, app, client):
        await register_bus(client)
        stub = BusStub()
        publisher = await make_publisher(app, stub)
        first = await publisher.publish_once()
        assert first > 0
        assert await publisher.publish_once() == 0  # nothing new
        assert len(stub.received) == first
        assert int(await app.state.store.get_meta(CURSOR_KEY)) > 0

    async def test_bus_failure_halts_batch_and_retries(self, app, client):
        await register_bus(client)
        stub = BusStub()
        stub.fail = True
        publisher = await make_publisher(app, stub)
        assert await publisher.publish_once() == 0

        stub.fail = False
        published = await publisher.publish_once()
        assert published > 0  # same events, retried — at-least-once

    async def test_new_events_flow_after_cursor(self, app, client):
        await register_bus(client)
        stub = BusStub()
        publisher = await make_publisher(app, stub)
        await publisher.publish_once()
        before = len(stub.received)

        await register(client, dict(REGISTRATION, name="atlas.later"))
        published = await publisher.publish_once()
        assert published >= 1
        assert len(stub.received) > before
        assert any(
            e["payload"].get("name") == "atlas.later" for e in stub.received[before:]
        )
