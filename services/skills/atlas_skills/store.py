# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Persistence for the Skill Manager."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from pydantic import BaseModel, Field, field_validator

SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)+$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    name         TEXT NOT NULL,
    version      TEXT NOT NULL,
    manifest     TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 0,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (name, version)
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


class SkillManifest(BaseModel):
    """The signed unit of publication."""

    name: str = Field(examples=["skill.greeter"])
    version: str = Field(examples=["1.0.0"])
    description: str = Field(default="", max_length=2000)
    #: Capabilities this skill provides when loaded by a consuming service.
    provides: list[str] = Field(default_factory=list, max_length=50)
    #: The packaged artifact, stored in the Atlas Asset Manager.
    artifact_asset_id: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publisher: str = Field(max_length=200)
    signature: str = Field(description="base64 ECDSA-SHA256 by the Atlas CA key")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not SKILL_NAME_PATTERN.match(v):
            raise ValueError("skill name must be dot-separated lowercase, e.g. 'skill.greeter'")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not VERSION_PATTERN.match(v):
            raise ValueError("version must be semantic (X.Y.Z)")
        return v


class SkillRecord(BaseModel):
    manifest: SkillManifest
    enabled: bool
    published_by: str
    published_at: datetime


def _row(r: aiosqlite.Row) -> SkillRecord:
    return SkillRecord(
        manifest=SkillManifest(**json.loads(r["manifest"])),
        enabled=bool(r["enabled"]),
        published_by=r["published_by"],
        published_at=datetime.fromisoformat(r["published_at"]),
    )


class SkillStore:
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
            raise RuntimeError("SkillStore is not open")
        return self._db

    async def publish(self, manifest: SkillManifest, *, published_by: str) -> SkillRecord:
        now = utcnow()
        await self.db.execute(
            """INSERT INTO skills (name, version, manifest, enabled, published_by, published_at)
               VALUES (?, ?, ?, 0, ?, ?)""",
            (
                manifest.name, manifest.version,
                json.dumps(manifest.model_dump()), published_by, now.isoformat(),
            ),
        )
        await self.db.commit()
        return SkillRecord(
            manifest=manifest, enabled=False,
            published_by=published_by, published_at=now,
        )

    async def exists(self, name: str, version: str) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM skills WHERE name = ? AND version = ?", (name, version)
        ) as cur:
            return await cur.fetchone() is not None

    async def get(self, name: str, version: str) -> SkillRecord | None:
        async with self.db.execute(
            "SELECT * FROM skills WHERE name = ? AND version = ?", (name, version)
        ) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def list(
        self, *, name: str | None = None, enabled_only: bool = False
    ) -> list[SkillRecord]:
        clauses, params = ["1=1"], []
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        if enabled_only:
            clauses.append("enabled = 1")
        async with self.db.execute(
            f"SELECT * FROM skills WHERE {' AND '.join(clauses)} ORDER BY name, version",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def set_enabled(self, name: str, version: str, enabled: bool) -> bool:
        cur = await self.db.execute(
            "UPDATE skills SET enabled = ? WHERE name = ? AND version = ?",
            (int(enabled), name, version),
        )
        await self.db.commit()
        return cur.rowcount > 0

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
