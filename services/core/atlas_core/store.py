# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Persistence layer for Atlas Core.

SQLite via aiosqlite: registry state survives Core restarts with zero
external infrastructure. The public surface of :class:`RegistryStore` is
deliberately small so the backend can be replaced (e.g. Postgres) without
touching the registry, health monitor, or API layers.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite

from .models import Event, ServiceRecord, ServiceStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    instance_id       TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    version           TEXT NOT NULL,
    address           TEXT NOT NULL,
    health_url        TEXT NOT NULL,
    capabilities      TEXT NOT NULL,          -- JSON array
    metadata          TEXT NOT NULL,          -- JSON object
    status            TEXT NOT NULL,
    token_hash        TEXT NOT NULL UNIQUE,
    registered_at     TEXT NOT NULL,          -- ISO 8601 UTC
    status_changed_at TEXT NOT NULL,
    last_heartbeat_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_services_name   ON services (name);
CREATE INDEX IF NOT EXISTS idx_services_status ON services (status);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload     TEXT NOT NULL                 -- JSON object
);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events (topic);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _row_to_record(row: aiosqlite.Row) -> ServiceRecord:
    return ServiceRecord(
        instance_id=row["instance_id"],
        name=row["name"],
        version=row["version"],
        address=row["address"],
        health_url=row["health_url"],
        capabilities=json.loads(row["capabilities"]),
        metadata=json.loads(row["metadata"]),
        status=ServiceStatus(row["status"]),
        registered_at=datetime.fromisoformat(row["registered_at"]),
        status_changed_at=datetime.fromisoformat(row["status_changed_at"]),
        last_heartbeat_at=(
            datetime.fromisoformat(row["last_heartbeat_at"]) if row["last_heartbeat_at"] else None
        ),
    )


class RegistryStore:
    """SQLite-backed store for service records and system events."""

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
            raise RuntimeError("RegistryStore is not open")
        return self._db

    # -- services ---------------------------------------------------------

    async def insert_service(self, record: ServiceRecord, token_hash: str) -> None:
        await self.db.execute(
            """INSERT INTO services (instance_id, name, version, address, health_url,
                   capabilities, metadata, status, token_hash, registered_at,
                   status_changed_at, last_heartbeat_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.instance_id,
                record.name,
                record.version,
                record.address,
                record.health_url,
                json.dumps(record.capabilities),
                json.dumps(record.metadata),
                record.status.value,
                token_hash,
                record.registered_at.isoformat(),
                record.status_changed_at.isoformat(),
                record.last_heartbeat_at.isoformat() if record.last_heartbeat_at else None,
            ),
        )
        await self.db.commit()

    async def get_service(self, instance_id: str) -> ServiceRecord | None:
        async with self.db.execute(
            "SELECT * FROM services WHERE instance_id = ?", (instance_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def get_service_by_token_hash(self, token_hash: str) -> ServiceRecord | None:
        async with self.db.execute(
            "SELECT * FROM services WHERE token_hash = ?", (token_hash,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def list_services(
        self,
        *,
        name: str | None = None,
        capability: str | None = None,
        status: ServiceStatus | None = None,
        include_deregistered: bool = False,
    ) -> list[ServiceRecord]:
        clauses, params = [], []
        if not include_deregistered:
            clauses.append("status != ?")
            params.append(ServiceStatus.DEREGISTERED.value)
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.db.execute(
            f"SELECT * FROM services {where} ORDER BY name, registered_at", params
        ) as cur:
            rows = await cur.fetchall()
        records = [_row_to_record(r) for r in rows]
        if capability is not None:
            records = [r for r in records if capability in r.capabilities]
        return records

    async def set_status(
        self, instance_id: str, status: ServiceStatus, changed_at: datetime
    ) -> None:
        await self.db.execute(
            "UPDATE services SET status = ?, status_changed_at = ? WHERE instance_id = ?",
            (status.value, changed_at.isoformat(), instance_id),
        )
        await self.db.commit()

    async def record_heartbeat(self, instance_id: str, at: datetime) -> None:
        await self.db.execute(
            "UPDATE services SET last_heartbeat_at = ? WHERE instance_id = ?",
            (at.isoformat(), instance_id),
        )
        await self.db.commit()

    async def revoke_token(self, instance_id: str) -> None:
        """Make a superseded/deregistered instance's token unusable."""
        await self.db.execute(
            "UPDATE services SET token_hash = 'revoked:' || instance_id WHERE instance_id = ?",
            (instance_id,),
        )
        await self.db.commit()

    # -- events -----------------------------------------------------------

    async def append_event(self, event: Event) -> None:
        await self.db.execute(
            "INSERT INTO events (topic, occurred_at, payload) VALUES (?, ?, ?)",
            (event.topic, event.occurred_at.isoformat(), json.dumps(event.payload)),
        )
        await self.db.commit()

    async def list_events_after(self, after_id: int, *, limit: int = 100) -> list[Event]:
        """Events with id > after_id, oldest first (outbox publishing order)."""
        async with self.db.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?", (after_id, limit)
        ) as cur:
            rows = await cur.fetchall()
        return [
            Event(
                id=row["id"],
                topic=row["topic"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                payload=json.loads(row["payload"]),
            )
            for row in rows
        ]

    # -- meta ---------------------------------------------------------------

    async def get_meta(self, key: str) -> str | None:
        async with self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def list_events(self, *, limit: int = 100) -> list[Event]:
        async with self.db.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [
            Event(
                id=row["id"],
                topic=row["topic"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                payload=json.loads(row["payload"]),
            )
            for row in rows
        ]
