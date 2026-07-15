# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Persistence for Sentinel: alerts + outbox."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from pydantic import BaseModel

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    subject      TEXT NOT NULL,
    severity     TEXT NOT NULL,
    detail       TEXT NOT NULL,
    raised_at    TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acked_by     TEXT,
    acked_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts (acknowledged, raised_at DESC);

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


class Alert(BaseModel):
    id: str
    kind: str
    subject: str
    severity: str
    detail: str
    raised_at: datetime
    acknowledged: bool = False
    acked_by: str | None = None
    acked_at: datetime | None = None


def _row(r: aiosqlite.Row) -> Alert:
    return Alert(
        id=r["id"], kind=r["kind"], subject=r["subject"], severity=r["severity"],
        detail=r["detail"], raised_at=datetime.fromisoformat(r["raised_at"]),
        acknowledged=bool(r["acknowledged"]), acked_by=r["acked_by"],
        acked_at=datetime.fromisoformat(r["acked_at"]) if r["acked_at"] else None,
    )


class SentinelStore:
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
            raise RuntimeError("SentinelStore is not open")
        return self._db

    # -- alerts ---------------------------------------------------------------

    async def raise_alert(
        self, *, kind: str, subject: str, severity: str, detail: str
    ) -> Alert:
        alert = Alert(
            id=uuid.uuid4().hex, kind=kind, subject=subject,
            severity=severity, detail=detail, raised_at=utcnow(),
        )
        await self.db.execute(
            """INSERT INTO alerts (id, kind, subject, severity, detail, raised_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (alert.id, alert.kind, alert.subject, alert.severity,
             alert.detail, alert.raised_at.isoformat()),
        )
        await self.db.commit()
        return alert

    async def list_alerts(
        self, *, include_acknowledged: bool = False, severity: str | None = None,
        limit: int = 200,
    ) -> list[Alert]:
        clauses, params = ["1=1"], []
        if not include_acknowledged:
            clauses.append("acknowledged = 0")
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        params.append(limit)
        async with self.db.execute(
            f"SELECT * FROM alerts WHERE {' AND '.join(clauses)} "
            "ORDER BY raised_at DESC LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def ack_alert(self, alert_id: str, *, acked_by: str) -> Alert | None:
        await self.db.execute(
            """UPDATE alerts SET acknowledged = 1, acked_by = ?, acked_at = ?
               WHERE id = ? AND acknowledged = 0""",
            (acked_by, utcnow().isoformat(), alert_id),
        )
        await self.db.commit()
        async with self.db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

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
