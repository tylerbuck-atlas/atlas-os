# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Persistence for the Planner: policies, plans, steps, outbox.

Plans and steps are the audit trail — rows are inserted and their status
fields updated, but never deleted. The home can always answer "what
acted, when, on whose authority, and what happened."
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite

from .models import (
    ActionRequest,
    PlanRecord,
    PlanStatus,
    PolicyEffect,
    PolicyRecord,
    StepRecord,
    StepStatus,
    utcnow,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS policies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    priority   INTEGER NOT NULL,
    requester  TEXT NOT NULL,
    capability TEXT NOT NULL,
    effect     TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id          TEXT PRIMARY KEY,
    goal        TEXT NOT NULL,
    requester   TEXT NOT NULL,
    status      TEXT NOT NULL,
    reason      TEXT,
    approved_by TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans (status);

CREATE TABLE IF NOT EXISTS steps (
    plan_id          TEXT NOT NULL REFERENCES plans(id),
    idx              INTEGER NOT NULL,
    capability       TEXT NOT NULL,
    params           TEXT NOT NULL,
    target_service   TEXT,
    status           TEXT NOT NULL,
    resolved_service TEXT,
    result           TEXT,
    error            TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    PRIMARY KEY (plan_id, idx)
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


def _dt(v) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def _step_row(r: aiosqlite.Row) -> StepRecord:
    return StepRecord(
        index=r["idx"], capability=r["capability"], params=json.loads(r["params"]),
        target_service=r["target_service"], status=StepStatus(r["status"]),
        resolved_service=r["resolved_service"],
        result=json.loads(r["result"]) if r["result"] else None,
        error=r["error"], started_at=_dt(r["started_at"]), finished_at=_dt(r["finished_at"]),
    )


class PlannerStore:
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
            raise RuntimeError("PlannerStore is not open")
        return self._db

    # -- policies ------------------------------------------------------------

    async def add_policy(
        self, *, priority: int, requester: str, capability: str,
        effect: PolicyEffect, note: str, created_by: str,
    ) -> PolicyRecord:
        now = utcnow()
        cur = await self.db.execute(
            """INSERT INTO policies (priority, requester, capability, effect, note,
                                     created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (priority, requester, capability, effect.value, note, created_by, now.isoformat()),
        )
        await self.db.commit()
        return PolicyRecord(
            id=cur.lastrowid, priority=priority, requester=requester,
            capability=capability, effect=effect, note=note,
            created_by=created_by, created_at=now,
        )

    async def list_policies(self) -> list[PolicyRecord]:
        async with self.db.execute(
            "SELECT * FROM policies ORDER BY priority, id"
        ) as cur:
            rows = await cur.fetchall()
        return [
            PolicyRecord(
                id=r["id"], priority=r["priority"], requester=r["requester"],
                capability=r["capability"], effect=PolicyEffect(r["effect"]),
                note=r["note"], created_by=r["created_by"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    async def delete_policy(self, policy_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM policies WHERE id = ?", (policy_id,))
        await self.db.commit()
        return cur.rowcount > 0

    # -- plans -----------------------------------------------------------------

    async def create_plan(
        self, *, goal: str, requester: str, status: PlanStatus,
        reason: str | None, actions: list[ActionRequest],
    ) -> PlanRecord:
        plan_id = uuid.uuid4().hex
        now = utcnow()
        await self.db.execute(
            """INSERT INTO plans (id, goal, requester, status, reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (plan_id, goal, requester, status.value, reason, now.isoformat(), now.isoformat()),
        )
        for index, action in enumerate(actions):
            await self.db.execute(
                """INSERT INTO steps (plan_id, idx, capability, params, target_service, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    plan_id, index, action.capability, json.dumps(action.params),
                    action.target_service, StepStatus.PENDING.value,
                ),
            )
        await self.db.commit()
        plan = await self.get_plan(plan_id)
        assert plan is not None
        return plan

    async def get_plan(self, plan_id: str) -> PlanRecord | None:
        async with self.db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        async with self.db.execute(
            "SELECT * FROM steps WHERE plan_id = ? ORDER BY idx", (plan_id,)
        ) as cur:
            steps = [_step_row(r) for r in await cur.fetchall()]
        return PlanRecord(
            id=row["id"], goal=row["goal"], requester=row["requester"],
            status=PlanStatus(row["status"]), reason=row["reason"],
            approved_by=row["approved_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            steps=steps,
        )

    async def list_plans(
        self, *, status: PlanStatus | None = None, requester: str | None = None,
        limit: int = 100,
    ) -> list[PlanRecord]:
        clauses, params = ["1=1"], []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if requester is not None:
            clauses.append("requester = ?")
            params.append(requester)
        params.append(limit)
        async with self.db.execute(
            f"SELECT id FROM plans WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC LIMIT ?",
            params,
        ) as cur:
            ids = [r["id"] for r in await cur.fetchall()]
        return [p for pid in ids if (p := await self.get_plan(pid))]

    async def set_plan_status(
        self, plan_id: str, status: PlanStatus, *,
        reason: str | None = None, approved_by: str | None = None,
    ) -> None:
        await self.db.execute(
            """UPDATE plans SET status = ?, updated_at = ?,
                   reason = COALESCE(?, reason),
                   approved_by = COALESCE(?, approved_by)
               WHERE id = ?""",
            (status.value, utcnow().isoformat(), reason, approved_by, plan_id),
        )
        await self.db.commit()

    async def update_step(
        self, plan_id: str, index: int, *, status: StepStatus,
        resolved_service: str | None = None, result: dict | None = None,
        error: str | None = None, started: bool = False, finished: bool = False,
    ) -> None:
        now = utcnow().isoformat()
        await self.db.execute(
            """UPDATE steps SET status = ?,
                   resolved_service = COALESCE(?, resolved_service),
                   result = COALESCE(?, result),
                   error = COALESCE(?, error),
                   started_at = CASE WHEN ? THEN ? ELSE started_at END,
                   finished_at = CASE WHEN ? THEN ? ELSE finished_at END
               WHERE plan_id = ? AND idx = ?""",
            (
                status.value, resolved_service,
                json.dumps(result) if result is not None else None,
                error, int(started), now, int(finished), now, plan_id, index,
            ),
        )
        await self.db.commit()

    # -- outbox ------------------------------------------------------------------

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
