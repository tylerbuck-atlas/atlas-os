"""Persistence for the Event Bus (SQLite via aiosqlite).

Same replaceability rule as Core's store: the public surface is small and
backend-agnostic so Postgres (or anything else) can replace SQLite without
touching bus logic or the API.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from .models import Delivery, EventEnvelope, Subscription, TopicSchema, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL UNIQUE,
    topic        TEXT NOT NULL,
    source       TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    published_at TEXT NOT NULL,
    schema_version INTEGER,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events (topic);

CREATE TABLE IF NOT EXISTS subscriptions (
    id           TEXT PRIMARY KEY,
    service_name TEXT NOT NULL,
    name         TEXT NOT NULL,
    topics       TEXT NOT NULL,          -- JSON array of patterns
    created_at   TEXT NOT NULL,
    UNIQUE (service_name, name)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
    event_row       INTEGER NOT NULL REFERENCES events(id),
    state           TEXT NOT NULL DEFAULT 'pending',   -- pending|inflight|acked
    attempts        INTEGER NOT NULL DEFAULT 0,
    visible_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_ready
    ON deliveries (subscription_id, state, visible_at);

CREATE TABLE IF NOT EXISTS schemas (
    topic       TEXT NOT NULL,
    version     INTEGER NOT NULL,
    json_schema TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (topic, version)
);
"""


def _row_to_envelope(row: aiosqlite.Row) -> EventEnvelope:
    return EventEnvelope(
        event_id=row["event_id"],
        topic=row["topic"],
        source=row["source"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        published_at=datetime.fromisoformat(row["published_at"]),
        schema_version=row["schema_version"],
        payload=json.loads(row["payload"]),
    )


class BusStore:
    def __init__(self, database_path: str) -> None:
        self._path = database_path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("BusStore is not open")
        return self._db

    # -- events -------------------------------------------------------------

    async def insert_event(self, envelope: EventEnvelope) -> int:
        cur = await self.db.execute(
            """INSERT INTO events (event_id, topic, source, occurred_at, published_at,
                                   schema_version, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                envelope.event_id,
                envelope.topic,
                envelope.source,
                envelope.occurred_at.isoformat(),
                envelope.published_at.isoformat(),
                envelope.schema_version,
                json.dumps(envelope.payload),
            ),
        )
        await self.db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    # -- subscriptions --------------------------------------------------------

    async def upsert_subscription(self, sub: Subscription) -> Subscription:
        """Create the subscription, or return the existing one for
        (service_name, name). Topic patterns are updated on conflict."""
        await self.db.execute(
            """INSERT INTO subscriptions (id, service_name, name, topics, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (service_name, name)
               DO UPDATE SET topics = excluded.topics""",
            (
                sub.id,
                sub.service_name,
                sub.name,
                json.dumps(sub.topics),
                sub.created_at.isoformat(),
            ),
        )
        await self.db.commit()
        async with self.db.execute(
            "SELECT * FROM subscriptions WHERE service_name = ? AND name = ?",
            (sub.service_name, sub.name),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        return self._row_to_subscription(row)

    @staticmethod
    def _row_to_subscription(row: aiosqlite.Row) -> Subscription:
        return Subscription(
            id=row["id"],
            service_name=row["service_name"],
            name=row["name"],
            topics=json.loads(row["topics"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def get_subscription(self, subscription_id: str) -> Subscription | None:
        async with self.db.execute(
            "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row_to_subscription(row) if row else None

    async def list_subscriptions(
        self, *, service_name: str | None = None
    ) -> list[Subscription]:
        if service_name is None:
            query, params = "SELECT * FROM subscriptions ORDER BY service_name, name", ()
        else:
            query, params = (
                "SELECT * FROM subscriptions WHERE service_name = ? ORDER BY name",
                (service_name,),
            )
        async with self.db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [self._row_to_subscription(r) for r in rows]

    async def delete_subscription(self, subscription_id: str) -> None:
        await self.db.execute(
            "DELETE FROM deliveries WHERE subscription_id = ?", (subscription_id,)
        )
        await self.db.execute(
            "DELETE FROM subscriptions WHERE id = ?", (subscription_id,)
        )
        await self.db.commit()

    # -- deliveries -----------------------------------------------------------

    async def enqueue(self, subscription_id: str, event_row: int) -> None:
        now = utcnow().isoformat()
        await self.db.execute(
            """INSERT INTO deliveries (subscription_id, event_row, state, attempts,
                                       visible_at, created_at)
               VALUES (?, ?, 'pending', 0, ?, ?)""",
            (subscription_id, event_row, now, now),
        )
        await self.db.commit()

    async def pull(
        self, subscription_id: str, *, max_messages: int, visibility_timeout: int
    ) -> list[Delivery]:
        """Claim up to max_messages ready deliveries (at-least-once).

        Ready = pending, or inflight whose visibility timeout expired
        (a consumer pulled it and never acked — it comes back).
        """
        now = utcnow()
        async with self.db.execute(
            """SELECT d.id AS delivery_id, d.attempts, e.*
               FROM deliveries d JOIN events e ON e.id = d.event_row
               WHERE d.subscription_id = ?
                 AND d.state != 'acked'
                 AND d.visible_at <= ?
               ORDER BY d.id
               LIMIT ?""",
            (subscription_id, now.isoformat(), max_messages),
        ) as cur:
            rows = await cur.fetchall()

        deliveries: list[Delivery] = []
        redelivery_at = (now + timedelta(seconds=visibility_timeout)).isoformat()
        for row in rows:
            await self.db.execute(
                """UPDATE deliveries
                   SET state = 'inflight', attempts = attempts + 1, visible_at = ?
                   WHERE id = ?""",
                (redelivery_at, row["delivery_id"]),
            )
            deliveries.append(
                Delivery(
                    delivery_id=row["delivery_id"],
                    attempt=row["attempts"] + 1,
                    event=_row_to_envelope(row),
                )
            )
        if deliveries:
            await self.db.commit()
        return deliveries

    async def ack(self, subscription_id: str, delivery_ids: list[int]) -> int:
        """Acknowledge deliveries (scoped to the subscription). Returns count."""
        total = 0
        for delivery_id in delivery_ids:
            cur = await self.db.execute(
                """UPDATE deliveries SET state = 'acked'
                   WHERE id = ? AND subscription_id = ? AND state != 'acked'""",
                (delivery_id, subscription_id),
            )
            total += cur.rowcount
        await self.db.commit()
        return total

    async def purge_acked(self, *, older_than_hours: int = 1) -> int:
        """Housekeeping: drop acked deliveries past their usefulness."""
        threshold = (utcnow() - timedelta(hours=older_than_hours)).isoformat()
        cur = await self.db.execute(
            "DELETE FROM deliveries WHERE state = 'acked' AND created_at < ?",
            (threshold,),
        )
        await self.db.commit()
        return cur.rowcount

    async def backlog(self, subscription_id: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE subscription_id = ? AND state != 'acked'",
            (subscription_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # -- schemas ---------------------------------------------------------------

    async def register_schema(self, topic: str, json_schema: dict) -> TopicSchema:
        async with self.db.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schemas WHERE topic = ?", (topic,)
        ) as cur:
            row = await cur.fetchone()
        version = int(row["v"]) + 1
        now = utcnow()
        await self.db.execute(
            "INSERT INTO schemas (topic, version, json_schema, created_at) VALUES (?, ?, ?, ?)",
            (topic, version, json.dumps(json_schema), now.isoformat()),
        )
        await self.db.commit()
        return TopicSchema(
            topic=topic, version=version, json_schema=json_schema, created_at=now
        )

    async def latest_schema(self, topic: str) -> TopicSchema | None:
        async with self.db.execute(
            "SELECT * FROM schemas WHERE topic = ? ORDER BY version DESC LIMIT 1", (topic,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return TopicSchema(
            topic=row["topic"],
            version=row["version"],
            json_schema=json.loads(row["json_schema"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def list_schemas(self) -> list[TopicSchema]:
        async with self.db.execute(
            """SELECT s.* FROM schemas s
               JOIN (SELECT topic, MAX(version) AS v FROM schemas GROUP BY topic) latest
                 ON latest.topic = s.topic AND latest.v = s.version
               ORDER BY s.topic"""
        ) as cur:
            rows = await cur.fetchall()
        return [
            TopicSchema(
                topic=row["topic"],
                version=row["version"],
                json_schema=json.loads(row["json_schema"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]
