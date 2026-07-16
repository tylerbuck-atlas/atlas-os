# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Persistence for the AI service: the interaction audit + outbox."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from pydantic import BaseModel

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interactions (
    id           TEXT PRIMARY KEY,
    requester    TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    answer       TEXT,
    rationale    TEXT NOT NULL DEFAULT '',
    plan_id      TEXT,
    plan_status  TEXT,
    model        TEXT NOT NULL,
    context_size INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);

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


class Interaction(BaseModel):
    id: str
    requester: str
    prompt: str
    kind: str
    answer: str | None
    rationale: str
    plan_id: str | None
    plan_status: str | None
    model: str
    context_size: int
    created_at: datetime


def _row(r: aiosqlite.Row) -> Interaction:
    return Interaction(
        id=r["id"], requester=r["requester"], prompt=r["prompt"], kind=r["kind"],
        answer=r["answer"], rationale=r["rationale"], plan_id=r["plan_id"],
        plan_status=r["plan_status"], model=r["model"], context_size=r["context_size"],
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

    async def record(
        self, *, requester: str, prompt: str, kind: str, answer: str | None,
        rationale: str, plan_id: str | None, plan_status: str | None,
        model: str, context_size: int,
    ) -> Interaction:
        interaction = Interaction(
            id=uuid.uuid4().hex, requester=requester, prompt=prompt, kind=kind,
            answer=answer, rationale=rationale, plan_id=plan_id,
            plan_status=plan_status, model=model, context_size=context_size,
            created_at=utcnow(),
        )
        await self.db.execute(
            """INSERT INTO interactions (id, requester, prompt, kind, answer, rationale,
                   plan_id, plan_status, model, context_size, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                interaction.id, requester, prompt, kind, answer, rationale,
                plan_id, plan_status, model, context_size,
                interaction.created_at.isoformat(),
            ),
        )
        await self.db.commit()
        return interaction

    async def list(
        self, *, requester: str | None = None, limit: int = 100
    ) -> list[Interaction]:
        if requester is None:
            query, params = (
                "SELECT * FROM interactions ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        else:
            query, params = (
                "SELECT * FROM interactions WHERE requester = ? "
                "ORDER BY created_at DESC LIMIT ?",
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
