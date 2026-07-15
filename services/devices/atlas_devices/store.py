# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Persistence for the Device Manager (SQLite, replaceable)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite

from .models import DeviceRecord, DeviceSync, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id         TEXT PRIMARY KEY,
    adapter    TEXT NOT NULL,
    native_id  TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    room       TEXT,
    data_class INTEGER NOT NULL,
    commands   TEXT NOT NULL,
    state      TEXT NOT NULL,
    online     INTEGER NOT NULL,
    metadata   TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (adapter, native_id)
);
CREATE INDEX IF NOT EXISTS idx_devices_kind ON devices (kind);
CREATE INDEX IF NOT EXISTS idx_devices_room ON devices (room);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _row(r: aiosqlite.Row) -> DeviceRecord:
    return DeviceRecord(
        id=r["id"], adapter=r["adapter"], native_id=r["native_id"], name=r["name"],
        kind=r["kind"], room=r["room"], data_class=r["data_class"],
        commands=json.loads(r["commands"]), state=json.loads(r["state"]),
        online=bool(r["online"]), metadata=json.loads(r["metadata"]),
        first_seen=datetime.fromisoformat(r["first_seen"]),
        updated_at=datetime.fromisoformat(r["updated_at"]),
    )


class DeviceStore:
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
            raise RuntimeError("DeviceStore is not open")
        return self._db

    async def upsert(
        self, *, adapter: str, native_id: str, sync: DeviceSync
    ) -> tuple[DeviceRecord, bool]:
        """Insert or update; returns (record, created)."""
        now = utcnow().isoformat()
        async with self.db.execute(
            "SELECT id FROM devices WHERE adapter = ? AND native_id = ?",
            (adapter, native_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            device_id = uuid.uuid4().hex
            await self.db.execute(
                """INSERT INTO devices (id, adapter, native_id, name, kind, room,
                       data_class, commands, state, online, metadata, first_seen, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    device_id, adapter, native_id, sync.name, sync.kind, sync.room,
                    sync.data_class, json.dumps(sync.commands), json.dumps(sync.state),
                    int(sync.online), json.dumps(sync.metadata), now, now,
                ),
            )
            created = True
        else:
            device_id = row["id"]
            await self.db.execute(
                """UPDATE devices SET name = ?, kind = ?, room = ?, data_class = ?,
                       commands = ?, state = ?, online = ?, metadata = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    sync.name, sync.kind, sync.room, sync.data_class,
                    json.dumps(sync.commands), json.dumps(sync.state),
                    int(sync.online), json.dumps(sync.metadata), now, device_id,
                ),
            )
            created = False
        await self.db.commit()
        record = await self.get(device_id)
        assert record is not None
        return record, created

    async def get(self, device_id: str) -> DeviceRecord | None:
        async with self.db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def list(
        self, *, kind: str | None = None, room: str | None = None,
        adapter: str | None = None, online: bool | None = None,
    ) -> list[DeviceRecord]:
        clauses, params = ["1=1"], []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if room is not None:
            clauses.append("room = ?")
            params.append(room)
        if adapter is not None:
            clauses.append("adapter = ?")
            params.append(adapter)
        if online is not None:
            clauses.append("online = ?")
            params.append(int(online))
        async with self.db.execute(
            f"SELECT * FROM devices WHERE {' AND '.join(clauses)} ORDER BY name", params
        ) as cur:
            rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def set_offline(self, device_id: str) -> None:
        await self.db.execute(
            "UPDATE devices SET online = 0, updated_at = ? WHERE id = ?",
            (utcnow().isoformat(), device_id),
        )
        await self.db.commit()

    # -- outbox -----------------------------------------------------------------

    async def append_event(self, topic: str, payload: dict) -> None:
        await self.db.execute(
            "INSERT INTO events (topic, occurred_at, payload) VALUES (?, ?, ?)",
            (topic, utcnow().isoformat(), json.dumps(payload)),
        )
        await self.db.commit()

    async def list_events_after(self, after_id: int, limit: int) -> list[tuple]:
        async with self.db.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?", (after_id, limit)
        ) as cur:
            rows = await cur.fetchall()
        return [
            (r["id"], r["topic"], json.loads(r["payload"]), r["occurred_at"]) for r in rows
        ]

    async def get_cursor(self) -> int:
        async with self.db.execute("SELECT value FROM meta WHERE key = 'outbox.cursor'") as cur:
            row = await cur.fetchone()
        return int(row["value"]) if row else 0

    async def set_cursor(self, value: int) -> None:
        await self.db.execute(
            "INSERT INTO meta (key, value) VALUES ('outbox.cursor', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(value),),
        )
        await self.db.commit()
