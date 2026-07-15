"""Publish, subscribe, pull, ack, redelivery, and auth scoping."""

from __future__ import annotations

import asyncio

from .conftest import CORE_TOKEN, ECHO_TOKEN, OTHER_TOKEN, auth, make_subscription, publish


class TestAuth:
    async def test_publish_requires_token(self, client):
        response = await client.post(
            "/v1/events", json={"topic": "a.b", "payload": {}}
        )
        assert response.status_code == 401

    async def test_unknown_token_rejected(self, client):
        response = await client.post(
            "/v1/events",
            json={"topic": "a.b", "payload": {}},
            headers=auth("garbage"),
        )
        assert response.status_code == 401

    async def test_source_is_set_from_identity_not_client(self, client):
        envelope = await publish(client, token=CORE_TOKEN)
        assert envelope["source"] == "atlas.core"


class TestPublishSubscribe:
    async def test_end_to_end_delivery(self, client):
        sub_id = await make_subscription(client, token=ECHO_TOKEN, topics=["registry.*"])
        envelope = await publish(client, topic="registry.service.registered")

        response = await client.post(
            f"/v1/subscriptions/{sub_id}/pull",
            json={"max_messages": 10},
            headers=auth(ECHO_TOKEN),
        )
        messages = response.json()["messages"]
        assert len(messages) == 1
        assert messages[0]["event"]["event_id"] == envelope["event_id"]
        assert messages[0]["event"]["topic"] == "registry.service.registered"
        assert messages[0]["attempt"] == 1

        ack = await client.post(
            f"/v1/subscriptions/{sub_id}/ack",
            json={"delivery_ids": [messages[0]["delivery_id"]]},
            headers=auth(ECHO_TOKEN),
        )
        assert ack.json() == {"acked": 1}

        # Acked -> gone.
        response = await client.post(
            f"/v1/subscriptions/{sub_id}/pull",
            json={"max_messages": 10},
            headers=auth(ECHO_TOKEN),
        )
        assert response.json()["messages"] == []

    async def test_non_matching_topic_not_delivered(self, client):
        sub_id = await make_subscription(client, topics=["devices.*"])
        await publish(client, topic="registry.service.registered")
        response = await client.post(
            f"/v1/subscriptions/{sub_id}/pull",
            json={"max_messages": 10},
            headers=auth(ECHO_TOKEN),
        )
        assert response.json()["messages"] == []

    async def test_fanout_to_multiple_subscribers(self, client):
        echo_sub = await make_subscription(client, token=ECHO_TOKEN, topics=["*"])
        other_sub = await make_subscription(client, token=OTHER_TOKEN, topics=["registry.*"])
        await publish(client)

        for sub_id, token in ((echo_sub, ECHO_TOKEN), (other_sub, OTHER_TOKEN)):
            response = await client.post(
                f"/v1/subscriptions/{sub_id}/pull",
                json={"max_messages": 10},
                headers=auth(token),
            )
            assert len(response.json()["messages"]) == 1

    async def test_subscription_upsert_is_idempotent(self, client):
        first = await make_subscription(client, name="main", topics=["registry.*"])
        second = await make_subscription(client, name="main", topics=["registry.*", "system.*"])
        assert first == second  # same id, topics updated

        response = await client.get("/v1/subscriptions", headers=auth(ECHO_TOKEN))
        subs = response.json()
        assert len(subs) == 1
        assert "system.*" in subs[0]["topics"]

    async def test_events_before_subscription_are_not_delivered(self, client):
        await publish(client)
        sub_id = await make_subscription(client)
        response = await client.post(
            f"/v1/subscriptions/{sub_id}/pull",
            json={"max_messages": 10},
            headers=auth(ECHO_TOKEN),
        )
        assert response.json()["messages"] == []


class TestAtLeastOnce:
    async def test_unacked_delivery_comes_back(self, client):
        """visibility_timeout_seconds=1 in test config."""
        sub_id = await make_subscription(client)
        await publish(client)

        first = (await client.post(
            f"/v1/subscriptions/{sub_id}/pull",
            json={"max_messages": 1},
            headers=auth(ECHO_TOKEN),
        )).json()["messages"]
        assert first[0]["attempt"] == 1

        # Not acked: invisible now, redelivered after the timeout.
        empty = (await client.post(
            f"/v1/subscriptions/{sub_id}/pull",
            json={"max_messages": 1},
            headers=auth(ECHO_TOKEN),
        )).json()["messages"]
        assert empty == []

        await asyncio.sleep(1.1)
        second = (await client.post(
            f"/v1/subscriptions/{sub_id}/pull",
            json={"max_messages": 1},
            headers=auth(ECHO_TOKEN),
        )).json()["messages"]
        assert len(second) == 1
        assert second[0]["delivery_id"] == first[0]["delivery_id"]
        assert second[0]["attempt"] == 2

    async def test_long_poll_returns_on_publish(self, client):
        sub_id = await make_subscription(client)

        async def poll():
            response = await client.post(
                f"/v1/subscriptions/{sub_id}/pull",
                json={"max_messages": 1, "wait_seconds": 5},
                headers=auth(ECHO_TOKEN),
            )
            return response.json()["messages"]

        task = asyncio.create_task(poll())
        await asyncio.sleep(0.2)
        await publish(client)
        messages = await asyncio.wait_for(task, timeout=4)
        assert len(messages) == 1


class TestScoping:
    async def test_cannot_pull_anothers_subscription(self, client):
        sub_id = await make_subscription(client, token=ECHO_TOKEN)
        response = await client.post(
            f"/v1/subscriptions/{sub_id}/pull",
            json={"max_messages": 1},
            headers=auth(OTHER_TOKEN),
        )
        assert response.status_code == 403

    async def test_cannot_ack_anothers_subscription(self, client):
        sub_id = await make_subscription(client, token=ECHO_TOKEN)
        response = await client.post(
            f"/v1/subscriptions/{sub_id}/ack",
            json={"delivery_ids": [1]},
            headers=auth(OTHER_TOKEN),
        )
        assert response.status_code == 403

    async def test_delete_subscription(self, client):
        sub_id = await make_subscription(client)
        response = await client.delete(
            f"/v1/subscriptions/{sub_id}", headers=auth(ECHO_TOKEN)
        )
        assert response.status_code == 204
        response = await client.get("/v1/subscriptions", headers=auth(ECHO_TOKEN))
        assert response.json() == []
