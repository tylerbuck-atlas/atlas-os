# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Persistence for Atlas AI: the assist audit log + outbox.

Prompts are personal (Class 2 by nature): the log is readable only by
the requester and the operator, and prompt content never crosses the bus.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from pydantic import BaseModel

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assists (
    id          TEXT PRIMARY KEY,
    requester   TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    backend     TEXT NOT NULL,
    answer      TEXT NOT NULL,
    sources     TEXT NOT NULL,
    plan_id     TEXT,
    plan_status TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assists_requester ON assists (requester, created_at DESC);

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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AssistRecord(BaseModel):
    id: str
    requester: str
    prompt: str
    backend: str
    answer: str
    sources: list[str]
    plan_id: str | None = None
    plan_status: str | None = None
    created_at: datetime


def _row(r: aiosqlite.Row) -> AssistRecord:
    return AssistRecord(
        id=r["id"], requester=r["requester"], prompt=r["prompt"], backend=r["backend"],
        answer=r["answer"], sources=json.loads(r["sources"]),
        plan_id=r["plan_id"], plan_status=r["plan_status"],
        created_at=datetime.fromisoformat(r["created_at"]),
    )


class AIStore:
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
            raise RuntimeError("AIStore is not open")
        return self._db

    async def record_assist(
        self, *, requester: str, prompt: str, backend: str, answer: str,
        sources: list[str], plan_id: str | None, plan_status: str | None,
    ) -> AssistRecord:
        record = AssistRecord(
            id=uuid.uuid4().hex, requester=requester, prompt=prompt,
            backend=backend, answer=answer, sources=sources,
            plan_id=plan_id, plan_status=plan_status, created_at=utcnow(),
        )
        await self.db.execute(
            """INSERT INTO assists (id, requester, prompt, backend, answer, sources,
                                    plan_id, plan_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.id, record.requester, record.prompt, record.backend,
                record.answer, json.dumps(record.sources),
                record.plan_id, record.plan_status, record.created_at.isoformat(),
            ),
        )
        await self.db.commit()
        return record

    async def list_assists(
        self, *, requester: str | None = None, limit: int = 100
    ) -> list[AssistRecord]:
        if requester is None:
            query, params = ("SELECT * FROM assists ORDER BY created_at DESC LIMIT ?", (limit,))
        else:
            query, params = (
                "SELECT * FROM assists WHERE requester = ? ORDER BY created_at DESC LIMIT ?",
                (requester, limit),
            )
        async with self.db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [_row(r) for r in rows]

    # -- outbox -------------------------------------------------------------------

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
