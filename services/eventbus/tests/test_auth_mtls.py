# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Bus auth in mtls mode: identity from the verified peer certificate.

Chain verification happens at the TLS handshake (covered by Core's
mTLS integration test); here we verify the bus resolves identity from
the peer certificate the transport recorded, and refuses its absence.
"""

from __future__ import annotations

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from atlas_eventbus.api.routes import router
from atlas_eventbus.bus import EventBus
from atlas_eventbus.config import BusConfig
from atlas_eventbus.store import BusStore
from atlas_sdk.tls import MTLSProtocol, identity_uri

#: httpx's ASGITransport reports this as the connecting client.
ASGI_CLIENT = ("127.0.0.1", 123)


def _cert_der(name: str, instance_id: str) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(identity_uri(name, instance_id))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


@pytest.fixture
async def mtls_app():
    config = BusConfig(
        bootstrap_token="test-bootstrap-token-1234",
        database_path=":memory:",
        security_mode="mtls",
        log_level="WARNING",
    )
    app = FastAPI()
    app.include_router(router)
    app.state.config = config
    store = BusStore(config.database_path)
    await store.open()
    app.state.store = store
    app.state.bus = EventBus(store, config)
    app.state.introspector = None  # mtls mode: nothing to introspect
    yield app
    MTLSProtocol.peer_certs.pop(ASGI_CLIENT, None)
    await store.close()


@pytest.fixture
async def client(mtls_app):
    transport = ASGITransport(app=mtls_app)
    async with AsyncClient(transport=transport, base_url="https://atlas-eventbus") as c:
        yield c


def _present_cert(name: str, instance_id: str) -> None:
    MTLSProtocol.peer_certs[ASGI_CLIENT] = _cert_der(name, instance_id)


class TestMTLSIdentity:
    async def test_no_certificate_is_401(self, client):
        MTLSProtocol.peer_certs.pop(ASGI_CLIENT, None)
        response = await client.post("/v1/events", json={"topic": "a.b", "payload": {}})
        assert response.status_code == 401
        assert "certificate" in response.json()["detail"]

    async def test_bearer_token_alone_is_refused_in_mtls_mode(self, client):
        MTLSProtocol.peer_certs.pop(ASGI_CLIENT, None)
        response = await client.post(
            "/v1/events",
            json={"topic": "a.b", "payload": {}},
            headers={"Authorization": "Bearer some-old-service-token"},
        )
        assert response.status_code == 401  # tokens are retired

    async def test_source_comes_from_certificate(self, client):
        _present_cert("atlas.core", "core-1")
        response = await client.post(
            "/v1/events", json={"topic": "registry.service.registered", "payload": {}}
        )
        assert response.status_code == 201
        assert response.json()["source"] == "atlas.core"

    async def test_subscription_scoped_to_certificate_identity(self, client):
        _present_cert("atlas.echo", "echo-1")
        sub = await client.post(
            "/v1/subscriptions", json={"name": "main", "topics": ["registry.*"]}
        )
        assert sub.status_code == 201
        sub_id = sub.json()["id"]
        assert sub.json()["service_name"] == "atlas.echo"

        # A different service's certificate cannot pull it.
        _present_cert("atlas.intruder", "bad-1")
        response = await client.post(
            f"/v1/subscriptions/{sub_id}/pull", json={"max_messages": 1}
        )
        assert response.status_code == 403

    async def test_cert_without_identity_san_refused(self, client):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nobody")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(hours=1))
            .sign(key, hashes.SHA256())
        )
        MTLSProtocol.peer_certs[ASGI_CLIENT] = cert.public_bytes(serialization.Encoding.DER)
        response = await client.post("/v1/events", json={"topic": "a.b", "payload": {}})
        assert response.status_code == 401
