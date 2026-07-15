# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Persistence for Atlas Memory (SQLite via aiosqlite, replaceable)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite

from .models import FactRecord, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace   TEXT NOT NULL,
    key         TEXT NOT NULL,
    version     INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    data_class  INTEGER NOT NULL,
    provenance  TEXT NOT NULL,
    source      TEXT NOT NULL,
    owner       TEXT,
    created_at  TEXT NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (namespace, key, version)
);
CREATE INDEX IF NOT EXISTS idx_facts_lookup ON facts (namespace, key, version DESC);

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


def _row_to_record(row: aiosqlite.Row) -> FactRecord:
    return FactRecord(
        namespace=row["namespace"],
        key=row["key"],
        version=row["version"],
        payload=json.loads(row["payload"]),
        data_class=row["data_class"],
        provenance=row["provenance"],
        source=row["source"],
        owner=row["owner"],
        created_at=datetime.fromisoformat(row["created_at"]),
        deleted=bool(row["deleted"]),
    )


class MemoryStore:
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
            raise RuntimeError("MemoryStore is not open")
        return self._db

    # -- facts --------------------------------------------------------------

    async def append_version(
        self,
        *,
        namespace: str,
        key: str,
        payload: dict,
        data_class: int,
        provenance: str,
        source: str,
        owner: str | None,
        deleted: bool = False,
    ) -> FactRecord:
        async with self.db.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM facts WHERE namespace = ? AND key = ?",
            (namespace, key),
        ) as cur:
            row = await cur.fetchone()
        version = int(row["v"]) + 1
        now = utcnow()
        await self.db.execute(
            """INSERT INTO facts (namespace, key, version, payload, data_class,
                                  provenance, source, owner, created_at, deleted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                namespace, key, version, json.dumps(payload), data_class,
                provenance, source, owner, now.isoformat(), int(deleted),
            ),
        )
        await self.db.commit()
        return FactRecord(
            namespace=namespace, key=key, version=version, payload=payload,
            data_class=data_class, provenance=provenance, source=source,
            owner=owner, created_at=now, deleted=deleted,
        )

    async def latest(self, namespace: str, key: str) -> FactRecord | None:
        async with self.db.execute(
            """SELECT * FROM facts WHERE namespace = ? AND key = ?
               ORDER BY version DESC LIMIT 1""",
            (namespace, key),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def history(self, namespace: str, key: str, *, limit: int = 100) -> list[FactRecord]:
        async with self.db.execute(
            """SELECT * FROM facts WHERE namespace = ? AND key = ?
               ORDER BY version DESC LIMIT ?""",
            (namespace, key, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def list_latest(
        self,
        namespace: str,
        *,
        key_prefix: str | None = None,
        max_class: int | None = None,
        include_deleted: bool = False,
    ) -> list[FactRecord]:
        """Latest version of every key in a namespace, filtered."""
        async with self.db.execute(
            """SELECT f.* FROM facts f
               JOIN (SELECT namespace, key, MAX(version) AS v FROM facts
                     WHERE namespace = ? GROUP BY namespace, key) latest
                 ON latest.namespace = f.namespace AND latest.key = f.key
                    AND latest.v = f.version
               ORDER BY f.key""",
            (namespace,),
        ) as cur:
            rows = await cur.fetchall()
        records = [_row_to_record(r) for r in rows]
        if not include_deleted:
            records = [r for r in records if not r.deleted]
        if key_prefix is not None:
            records = [r for r in records if r.key.startswith(key_prefix)]
        if max_class is not None:
            records = [r for r in records if r.data_class <= max_class]
        return records

    # -- outbox ----------------------------------------------------------------

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
            (row["id"], row["topic"], json.loads(row["payload"]), row["occurred_at"])
            for row in rows
        ]

    async def get_cursor(self) -> int:
        async with self.db.execute(
            "SELECT value FROM meta WHERE key = 'outbox.cursor'"
        ) as cur:
            row = await cur.fetchone()
        return int(row["value"]) if row else 0

    async def set_cursor(self, value: int) -> None:
        await self.db.execute(
            "INSERT INTO meta (key, value) VALUES ('outbox.cursor', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(value),),
        )
        await self.db.commit()
