# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Persistence for the Asset Manager.

Metadata in SQLite; content in a **content-addressed blob store** —
blobs live at ``blob_dir/<sha256>``, so identical files are stored once
and every read can (and does) verify integrity against the address.
A truth source that cannot prove it is unmodified is not a truth source.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from pydantic import BaseModel, Field

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    data_class   INTEGER NOT NULL,
    tags         TEXT NOT NULL,          -- JSON array
    metadata     TEXT NOT NULL,          -- JSON object
    sha256       TEXT NOT NULL,
    size         INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    uploaded_by  TEXT NOT NULL,
    owner        TEXT,
    uploaded_at  TEXT NOT NULL,
    deleted      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets (kind);
CREATE INDEX IF NOT EXISTS idx_assets_sha  ON assets (sha256);

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


class AssetRecord(BaseModel):
    id: str
    name: str
    kind: str
    data_class: int = Field(serialization_alias="class")
    tags: list[str]
    metadata: dict
    sha256: str
    size: int
    content_type: str
    uploaded_by: str
    owner: str | None
    uploaded_at: datetime
    deleted: bool = False

    model_config = {"populate_by_name": True}


class IntegrityError(Exception):
    """Blob content does not match its content address."""


def _row(r: aiosqlite.Row) -> AssetRecord:
    return AssetRecord(
        id=r["id"], name=r["name"], kind=r["kind"], data_class=r["data_class"],
        tags=json.loads(r["tags"]), metadata=json.loads(r["metadata"]),
        sha256=r["sha256"], size=r["size"], content_type=r["content_type"],
        uploaded_by=r["uploaded_by"], owner=r["owner"],
        uploaded_at=datetime.fromisoformat(r["uploaded_at"]),
        deleted=bool(r["deleted"]),
    )


class AssetStore:
    def __init__(self, database_path: str, blob_dir: str) -> None:
        self._path = database_path
        self._blob_dir = Path(blob_dir)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._blob_dir.mkdir(parents=True, exist_ok=True)
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
            raise RuntimeError("AssetStore is not open")
        return self._db

    # -- blobs (content-addressed) -------------------------------------------

    def write_blob(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        path = self._blob_dir / digest
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(content)
            tmp.rename(path)
        return digest

    def read_blob(self, sha256: str) -> bytes:
        content = (self._blob_dir / sha256).read_bytes()
        if hashlib.sha256(content).hexdigest() != sha256:
            raise IntegrityError(sha256)
        return content

    def blob_exists(self, sha256: str) -> bool:
        return (self._blob_dir / sha256).exists()

    async def delete_blob_if_unreferenced(self, sha256: str) -> bool:
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM assets WHERE sha256 = ? AND deleted = 0", (sha256,)
        ) as cur:
            row = await cur.fetchone()
        if int(row["n"]) == 0 and self.blob_exists(sha256):
            (self._blob_dir / sha256).unlink()
            return True
        return False

    # -- records -------------------------------------------------------------------

    async def insert(
        self,
        *,
        name: str,
        kind: str,
        data_class: int,
        tags: list[str],
        metadata: dict,
        sha256: str,
        size: int,
        content_type: str,
        uploaded_by: str,
        owner: str | None,
    ) -> AssetRecord:
        record = AssetRecord(
            id=uuid.uuid4().hex, name=name, kind=kind, data_class=data_class,
            tags=tags, metadata=metadata, sha256=sha256, size=size,
            content_type=content_type, uploaded_by=uploaded_by, owner=owner,
            uploaded_at=utcnow(),
        )
        await self.db.execute(
            """INSERT INTO assets (id, name, kind, data_class, tags, metadata, sha256,
                                   size, content_type, uploaded_by, owner, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.id, record.name, record.kind, record.data_class,
                json.dumps(record.tags), json.dumps(record.metadata), record.sha256,
                record.size, record.content_type, record.uploaded_by, record.owner,
                record.uploaded_at.isoformat(),
            ),
        )
        await self.db.commit()
        return record

    async def get(self, asset_id: str) -> AssetRecord | None:
        async with self.db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)) as cur:
            row = await cur.fetchone()
        return _row(row) if row else None

    async def list(
        self,
        *,
        kind: str | None = None,
        tag: str | None = None,
        max_class: int | None = None,
    ) -> list[AssetRecord]:
        clauses, params = ["deleted = 0"], []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if max_class is not None:
            clauses.append("data_class <= ?")
            params.append(max_class)
        async with self.db.execute(
            f"SELECT * FROM assets WHERE {' AND '.join(clauses)} ORDER BY uploaded_at DESC",
            params,
        ) as cur:
            rows = await cur.fetchall()
        records = [_row(r) for r in rows]
        if tag is not None:
            records = [r for r in records if tag in r.tags]
        return records

    async def tombstone(self, asset_id: str) -> None:
        await self.db.execute("UPDATE assets SET deleted = 1 WHERE id = ?", (asset_id,))
        await self.db.commit()

    # -- outbox ------------------------------------------------------------------------

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
