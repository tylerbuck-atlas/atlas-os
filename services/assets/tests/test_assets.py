# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Assets: ingestion, content addressing, integrity, policy, tombstones."""

from __future__ import annotations

import hashlib

from .conftest import OPERATOR_TOKEN, OTHER_TOKEN, UPLOADER_TOKEN, auth, upload


class TestIngestion:
    async def test_upload_returns_record_with_provenance(self, client):
        record = await upload(client)
        assert record["name"] == "dishwasher-manual.pdf"
        assert record["kind"] == "manual"
        assert record["uploaded_by"] == "atlas.ingestor"
        assert record["sha256"] == hashlib.sha256(b"MANUAL PDF BYTES").hexdigest()
        assert record["size"] == len(b"MANUAL PDF BYTES")
        assert record["tags"] == ["kitchen", "appliance"]
        assert record["metadata"] == {"brand": "Bosch"}

    async def test_identical_content_deduplicates(self, app, client):
        a = await upload(client, name="copy-one.pdf")
        b = await upload(client, name="copy-two.pdf")
        assert a["sha256"] == b["sha256"]
        blob_dir = app.state.store._blob_dir
        assert len(list(blob_dir.iterdir())) == 1  # one blob, two records

    async def test_invalid_kind_rejected(self, client):
        response = await client.post(
            "/v1/assets",
            files={"file": ("x.bin", b"data", "application/octet-stream")},
            data={"kind": "malware"},
            headers=auth(UPLOADER_TOKEN),
        )
        assert response.status_code == 422

    async def test_empty_upload_rejected(self, client):
        response = await client.post(
            "/v1/assets",
            files={"file": ("x.bin", b"", "application/octet-stream")},
            data={"kind": "document"},
            headers=auth(UPLOADER_TOKEN),
        )
        assert response.status_code == 422

    async def test_oversize_upload_rejected(self, client):
        response = await client.post(
            "/v1/assets",
            files={"file": ("big.bin", b"x" * (1024 * 1024 + 1), "application/octet-stream")},
            data={"kind": "document"},
            headers=auth(UPLOADER_TOKEN),
        )
        assert response.status_code == 413

    async def test_requires_auth(self, client):
        response = await client.post(
            "/v1/assets",
            files={"file": ("x.bin", b"data", "application/octet-stream")},
        )
        assert response.status_code == 401


class TestContent:
    async def test_content_roundtrip_with_integrity_header(self, client):
        record = await upload(client, content=b"round trip bytes")
        response = await client.get(
            f"/v1/assets/{record['id']}/content", headers=auth(OTHER_TOKEN)
        )
        assert response.status_code == 200
        assert response.content == b"round trip bytes"
        assert response.headers["X-Atlas-SHA256"] == record["sha256"]

    async def test_tampered_blob_refused(self, app, client):
        record = await upload(client, content=b"original content")
        # Corrupt the blob on disk behind the store's back.
        (app.state.store._blob_dir / record["sha256"]).write_bytes(b"tampered!")
        response = await client.get(
            f"/v1/assets/{record['id']}/content", headers=auth(OTHER_TOKEN)
        )
        assert response.status_code == 502
        assert "integrity" in response.json()["detail"]


class TestPolicy:
    async def test_class3_content_steward_only(self, client):
        record = await upload(client, data_class=3, name="camera-still.jpg", kind="photo")
        for token, expected in ((UPLOADER_TOKEN, 200), (OPERATOR_TOKEN, 200), (OTHER_TOKEN, 403)):
            response = await client.get(
                f"/v1/assets/{record['id']}/content", headers=auth(token)
            )
            assert response.status_code == expected

    async def test_class3_filtered_from_listing(self, client):
        await upload(client, data_class=0, name="manual.pdf")
        await upload(client, data_class=3, name="camera.jpg", kind="photo",
                     content=b"different bytes")
        listing = await client.get("/v1/assets", headers=auth(OTHER_TOKEN))
        names = [a["name"] for a in listing.json()]
        assert names == ["manual.pdf"]

    async def test_kind_and_tag_filters(self, client):
        await upload(client, name="a.pdf", kind="manual", tags="kitchen")
        await upload(client, name="b.pdf", kind="document", tags="garage",
                     content=b"other content")
        response = await client.get(
            "/v1/assets", params={"kind": "manual", "tag": "kitchen"},
            headers=auth(OTHER_TOKEN),
        )
        assert [a["name"] for a in response.json()] == ["a.pdf"]


class TestDeletion:
    async def test_tombstone_and_blob_gc(self, app, client):
        record = await upload(client, content=b"to be deleted")
        response = await client.delete(
            f"/v1/assets/{record['id']}", headers=auth(UPLOADER_TOKEN)
        )
        assert response.status_code == 204
        assert (await client.get(
            f"/v1/assets/{record['id']}", headers=auth(UPLOADER_TOKEN)
        )).status_code == 404
        assert not app.state.store.blob_exists(record["sha256"])

    async def test_shared_blob_survives_partial_delete(self, app, client):
        a = await upload(client, name="one.pdf", content=b"shared bytes")
        await upload(client, name="two.pdf", content=b"shared bytes")
        await client.delete(f"/v1/assets/{a['id']}", headers=auth(UPLOADER_TOKEN))
        assert app.state.store.blob_exists(a["sha256"])  # still referenced


class TestEvents:
    async def test_ingest_event_metadata_only_and_redacted_for_personal(self, app, client):
        await upload(client, data_class=1, name="public-manual.pdf")
        await upload(client, data_class=2, name="tax-return.pdf",
                     content=b"private bytes")
        events = await app.state.store.list_events_after(0, 10)
        assert len(events) == 2
        _, topic, first, _ = events[0]
        _, _, second, _ = events[1]
        assert topic == "assets.asset.ingested"
        assert first["name"] == "public-manual.pdf"
        assert "tax-return" not in str(second)
        assert second["redacted"] is True
        # Never any content in events.
        assert "private bytes" not in str(events)
