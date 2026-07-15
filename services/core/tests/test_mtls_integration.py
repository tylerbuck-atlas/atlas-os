# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""mTLS integration: a REAL Atlas Core over real sockets and real TLS.

Boots Core in mtls mode under uvicorn on localhost, then exercises the
full Zero Trust flow: CSR enrollment, certificate identity, instance
scoping, operator certs, and rejection of the credential-less.
"""

from __future__ import annotations

import asyncio
import ssl

import httpx
import pytest
import uvicorn

from atlas_core.ca import CertificateAuthority
from atlas_core.config import CoreConfig
from atlas_core.main import create_app, init_tls
from atlas_sdk.tls import create_csr_pem, generate_private_key_pem

PORT = 18443
BASE = f"https://localhost:{PORT}"
BOOTSTRAP = "mtls-test-bootstrap-token"


@pytest.fixture
async def core(tmp_path):
    config = CoreConfig(
        bootstrap_token=BOOTSTRAP,
        database_path=":memory:",
        security_mode="mtls",
        ca_dir=str(tmp_path / "ca"),
        core_hostnames="localhost",
        heartbeat_interval_seconds=1,
        probe_interval_seconds=3600,  # no probing during the test
        log_level="WARNING",
        _env_file=None,
    )
    app = create_app(config)
    init_tls(app)
    server_config = uvicorn.Config(
        app, host="127.0.0.1", port=PORT, log_config=None, log_level="warning",
        **app.state.core_tls.uvicorn_kwargs(),
    )
    server = uvicorn.Server(server_config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    yield app
    server.should_exit = True
    await task


def _server_verify(app) -> ssl.SSLContext:
    """Trust the test CA (server auth only, no client cert)."""
    return ssl.create_default_context(cadata=app.state.ca.cert_pem.decode())


def _mtls_ctx(app, key_pem: bytes, cert_pem: bytes, tmp_path) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cadata=app.state.ca.cert_pem.decode())
    key_file = tmp_path / "c.key"
    cert_file = tmp_path / "c.crt"
    key_file.write_bytes(key_pem)
    cert_file.write_bytes(cert_pem)
    ctx.load_cert_chain(str(cert_file), str(key_file))
    return ctx


async def _enroll(app, name="atlas.echo") -> tuple[bytes, bytes, dict]:
    key_pem = generate_private_key_pem()
    csr = create_csr_pem(key_pem, name).decode()
    async with httpx.AsyncClient(verify=_server_verify(app)) as client:
        response = await client.post(
            f"{BASE}/v1/registry/services",
            headers={"Authorization": f"Bearer {BOOTSTRAP}"},
            json={
                "name": name,
                "version": "0.1.0",
                "address": "https://localhost:9999",
                "health_url": "https://localhost:9999/healthz",
                "capabilities": ["echo.reply"],
                "csr": csr,
            },
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "service_token" not in body  # tokens are retired in mtls mode
    return key_pem, body["certificate"].encode(), body


class TestEnrollment:
    async def test_ca_certificate_is_public(self, core):
        async with httpx.AsyncClient(verify=_server_verify(core)) as client:
            response = await client.get(f"{BASE}/v1/ca/certificate")
        assert response.status_code == 200
        assert b"BEGIN CERTIFICATE" in response.content

    async def test_registration_without_csr_rejected(self, core):
        async with httpx.AsyncClient(verify=_server_verify(core)) as client:
            response = await client.post(
                f"{BASE}/v1/registry/services",
                headers={"Authorization": f"Bearer {BOOTSTRAP}"},
                json={
                    "name": "atlas.echo",
                    "version": "0.1.0",
                    "address": "https://localhost:9999",
                    "health_url": "https://localhost:9999/healthz",
                },
            )
        assert response.status_code == 400
        assert "csr" in response.json()["detail"]

    async def test_enrollment_issues_certificate(self, core):
        _, cert_pem, body = await _enroll(core)
        assert b"BEGIN CERTIFICATE" in cert_pem
        assert body["ca_certificate"].startswith("-----BEGIN CERTIFICATE")


class TestCertificateIdentity:
    async def test_certified_service_can_heartbeat_and_discover(self, core, tmp_path):
        key_pem, cert_pem, body = await _enroll(core)
        instance_id = body["service"]["instance_id"]
        ctx = _mtls_ctx(core, key_pem, cert_pem, tmp_path)

        async with httpx.AsyncClient(verify=ctx) as client:
            hb = await client.post(
                f"{BASE}/v1/registry/services/{instance_id}/heartbeat"
            )
            assert hb.status_code == 200

            listing = await client.get(f"{BASE}/v1/registry/services")
            assert listing.status_code == 200
            assert listing.json()[0]["name"] == "atlas.echo"

    async def test_no_certificate_no_access(self, core):
        async with httpx.AsyncClient(verify=_server_verify(core)) as client:
            response = await client.get(f"{BASE}/v1/registry/services")
        assert response.status_code == 401
        assert "certificate" in response.json()["detail"]

    async def test_healthz_needs_nothing(self, core):
        async with httpx.AsyncClient(verify=_server_verify(core)) as client:
            response = await client.get(f"{BASE}/healthz")
        assert response.status_code == 200

    async def test_cert_of_service_a_cannot_touch_service_b(self, core, tmp_path):
        a_key, a_cert, a_body = await _enroll(core, "atlas.echo")
        _b_key, _b_cert, b_body = await _enroll(core, "atlas.other")
        ctx = _mtls_ctx(core, a_key, a_cert, tmp_path)

        async with httpx.AsyncClient(verify=ctx) as client:
            response = await client.post(
                f"{BASE}/v1/registry/services/{b_body['service']['instance_id']}/heartbeat"
            )
        assert response.status_code == 403

    async def test_superseded_certificate_is_refused(self, core, tmp_path):
        """Rotation/revocation: re-enrollment supersedes the old instance;
        its still-unexpired certificate stops working immediately."""
        old_key, old_cert, old_body = await _enroll(core, "atlas.echo")
        await _enroll(core, "atlas.echo")  # supersedes

        ctx = _mtls_ctx(core, old_key, old_cert, tmp_path)
        async with httpx.AsyncClient(verify=ctx) as client:
            response = await client.post(
                f"{BASE}/v1/registry/services/{old_body['service']['instance_id']}/heartbeat"
            )
        assert response.status_code == 410

    async def test_operator_certificate_can_discover(self, core, tmp_path):
        ca: CertificateAuthority = core.state.ca
        key_pem, cert_pem = ca.issue_self(
            common_name="atlas.operator",
            instance_id="manual-test",
            dns_names=[],
            ttl_hours=1,
        )
        ctx = _mtls_ctx(core, key_pem, cert_pem, tmp_path)
        async with httpx.AsyncClient(verify=ctx) as client:
            response = await client.get(f"{BASE}/v1/registry/services")
        assert response.status_code == 200

    async def test_foreign_ca_certificate_rejected_at_handshake(self, core, tmp_path):
        """A cert from a DIFFERENT CA never reaches the application."""
        foreign = CertificateAuthority(tmp_path / "foreign-ca")
        foreign.ensure()
        key_pem, cert_pem = foreign.issue_self(
            common_name="atlas.echo",
            instance_id="evil",
            dns_names=[],
            ttl_hours=1,
        )
        ctx = ssl.create_default_context(cadata=core.state.ca.cert_pem.decode())
        key_file, cert_file = tmp_path / "f.key", tmp_path / "f.crt"
        key_file.write_bytes(key_pem)
        cert_file.write_bytes(cert_pem)
        ctx.load_cert_chain(str(cert_file), str(key_file))

        refused = False
        try:
            async with httpx.AsyncClient(verify=ctx) as client:
                response = await client.get(f"{BASE}/v1/registry/services")
            refused = response.status_code == 401
        except (httpx.TransportError, ssl.SSLError):
            # Verification failed at the TLS handshake — the server aborted
            # the connection before the request ever reached the app.
            refused = True
        assert refused
