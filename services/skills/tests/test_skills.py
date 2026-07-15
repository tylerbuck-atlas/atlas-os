# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Skills: signature verification, artifact cross-check, lifecycle, authz."""

from __future__ import annotations

import datetime
import hashlib

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from atlas_sdk.service_auth import Identity
from atlas_skills.api.routes import router
from atlas_skills.config import SkillsConfig
from atlas_skills.store import SkillStore
from atlas_skills.verify import sign_manifest, verify_manifest

OPERATOR_TOKEN = "tok-operator"
SERVICE_TOKEN = "tok-service"

IDENTITIES = {
    OPERATOR_TOKEN: Identity("atlas.operator", "manual-1", "cert"),
    SERVICE_TOKEN: Identity("atlas.other", "o-1", "0.1.0"),
}

ARTIFACT_BYTES = b"skill artifact bytes"
ARTIFACT_SHA = hashlib.sha256(ARTIFACT_BYTES).hexdigest()


class FakeIntrospector:
    async def introspect(self, token):
        return IDENTITIES.get(token)

    async def close(self):
        pass


@pytest.fixture(scope="module")
def ca():
    """A throwaway CA keypair + self-signed cert (the trust anchor)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Atlas CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM)


class FakeAtlas:
    core_url = "https://core.test"
    security_mode = "token"
    service_token = "tok-skills"
    tls = None

    def bus_credentials(self):
        return (self.service_token, None)


@pytest.fixture
async def app(ca, monkeypatch):
    _, ca_pem = ca
    config = SkillsConfig(
        bootstrap_token="test-bootstrap-token-1234",
        database_path=":memory:",
        security_mode="token",
        log_level="WARNING",
    )
    application = FastAPI()
    application.include_router(router)
    application.state.config = config
    application.state.atlas = FakeAtlas()
    application.state.ca_cert_pem = ca_pem

    store = SkillStore(config.database_path)
    await store.open()
    application.state.store = store
    application.state.introspector = FakeIntrospector()

    # Fake Assets: one known artifact.
    import atlas_skills.api.routes as routes_module

    async def fake_discover(*, core_url, token=None, ssl_context=None,
                            name=None, capability=None, timeout=5.0):
        return [{"name": "atlas.assets", "status": "healthy",
                 "address": "https://assets.test"}]

    monkeypatch.setattr(routes_module, "discover_service", fake_discover)

    def assets_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/assets/asset-1":
            return httpx.Response(200, json={"id": "asset-1", "sha256": ARTIFACT_SHA})
        return httpx.Response(404, json={"detail": "unknown asset"})

    real_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(assets_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(routes_module.httpx, "AsyncClient", patched_client)

    yield application
    await store.close()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://atlas-skills") as c:
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make_manifest(ca_key, *, name="skill.greeter", version="1.0.0",
                  asset_id="asset-1", sha=ARTIFACT_SHA, sign=True, tamper=False):
    manifest = {
        "name": name, "version": version,
        "description": "Says hello",
        "provides": ["greeter.hello"],
        "artifact_asset_id": asset_id,
        "artifact_sha256": sha,
        "publisher": "Tyler Buck",
    }
    if sign:
        manifest["signature"] = sign_manifest(manifest, ca_key)
    else:
        manifest["signature"] = "bm90IGEgc2ln"  # base64 "not a sig"
    if tamper:
        manifest["provides"] = ["greeter.hello", "sneaky.extra"]
    return manifest


class TestVerification:
    def test_roundtrip(self, ca):
        key, cert_pem = ca
        manifest = make_manifest(key)
        assert verify_manifest(manifest, cert_pem) is True

    def test_tampered_manifest_fails(self, ca):
        key, cert_pem = ca
        manifest = make_manifest(key, tamper=True)
        assert verify_manifest(manifest, cert_pem) is False

    def test_missing_signature_fails(self, ca):
        _, cert_pem = ca
        assert verify_manifest({"name": "x"}, cert_pem) is False


class TestPublication:
    async def test_valid_skill_published(self, ca, client):
        key, _ = ca
        response = await client.post(
            "/v1/skills", json=make_manifest(key), headers=auth(OPERATOR_TOKEN)
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["enabled"] is False
        assert body["manifest"]["name"] == "skill.greeter"

    async def test_bad_signature_refused(self, ca, client):
        key, _ = ca
        response = await client.post(
            "/v1/skills", json=make_manifest(key, sign=False),
            headers=auth(OPERATOR_TOKEN),
        )
        assert response.status_code == 422
        assert "signature" in response.json()["detail"]

    async def test_tampered_after_signing_refused(self, ca, client):
        key, _ = ca
        response = await client.post(
            "/v1/skills", json=make_manifest(key, tamper=True),
            headers=auth(OPERATOR_TOKEN),
        )
        assert response.status_code == 422

    async def test_artifact_hash_mismatch_refused(self, ca, client):
        key, _ = ca
        manifest = make_manifest(key, sha="0" * 64)
        response = await client.post(
            "/v1/skills", json=manifest, headers=auth(OPERATOR_TOKEN)
        )
        assert response.status_code == 422
        assert "mismatch" in response.json()["detail"]

    async def test_unknown_artifact_refused(self, ca, client):
        key, _ = ca
        response = await client.post(
            "/v1/skills", json=make_manifest(key, asset_id="nope"),
            headers=auth(OPERATOR_TOKEN),
        )
        assert response.status_code == 422

    async def test_versions_immutable(self, ca, client):
        key, _ = ca
        manifest = make_manifest(key)
        await client.post("/v1/skills", json=manifest, headers=auth(OPERATOR_TOKEN))
        response = await client.post(
            "/v1/skills", json=manifest, headers=auth(OPERATOR_TOKEN)
        )
        assert response.status_code == 409

    async def test_non_operator_cannot_publish(self, ca, client):
        key, _ = ca
        response = await client.post(
            "/v1/skills", json=make_manifest(key), headers=auth(SERVICE_TOKEN)
        )
        assert response.status_code == 403


class TestLifecycle:
    async def test_enable_disable_and_discovery(self, ca, client):
        key, _ = ca
        await client.post("/v1/skills", json=make_manifest(key), headers=auth(OPERATOR_TOKEN))

        enabled = await client.post(
            "/v1/skills/skill.greeter/1.0.0/enable", headers=auth(OPERATOR_TOKEN)
        )
        assert enabled.json()["enabled"] is True

        listing = await client.get(
            "/v1/skills", params={"enabled_only": True}, headers=auth(SERVICE_TOKEN)
        )
        assert len(listing.json()) == 1

        await client.post(
            "/v1/skills/skill.greeter/1.0.0/disable", headers=auth(OPERATOR_TOKEN)
        )
        listing = await client.get(
            "/v1/skills", params={"enabled_only": True}, headers=auth(SERVICE_TOKEN)
        )
        assert listing.json() == []

    async def test_non_operator_cannot_enable(self, ca, client):
        key, _ = ca
        await client.post("/v1/skills", json=make_manifest(key), headers=auth(OPERATOR_TOKEN))
        response = await client.post(
            "/v1/skills/skill.greeter/1.0.0/enable", headers=auth(SERVICE_TOKEN)
        )
        assert response.status_code == 403
