# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""The Atlas CA: creation, persistence, issuance, identity SANs, signatures."""

from __future__ import annotations

import pytest
from cryptography import x509

from atlas_core.ca import CertificateAuthority, hostnames_from_urls
from atlas_sdk.tls import (
    create_csr_pem,
    generate_private_key_pem,
    peer_identity_from_der,
)


@pytest.fixture
def ca(tmp_path):
    authority = CertificateAuthority(tmp_path / "ca")
    authority.ensure()
    return authority


class TestCALifecycle:
    def test_created_once_then_loaded(self, tmp_path):
        first = CertificateAuthority(tmp_path / "ca")
        assert first.ensure() is True
        second = CertificateAuthority(tmp_path / "ca")
        assert second.ensure() is False
        assert first.cert_pem == second.cert_pem

    def test_root_is_a_ca_cert(self, ca):
        cert = x509.load_pem_x509_certificate(ca.cert_pem)
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        assert bc.ca is True


class TestIssuance:
    def test_issues_cert_with_identity_and_dns_sans(self, ca):
        key_pem = generate_private_key_pem()
        csr = create_csr_pem(key_pem, "atlas.echo")
        cert_pem = ca.issue_from_csr(
            csr,
            service_name="atlas.echo",
            instance_id="abc123",
            dns_names=["atlas-echo", "localhost"],
            ttl_hours=24,
        )
        cert = x509.load_pem_x509_certificate(cert_pem)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert set(san.get_values_for_type(x509.DNSName)) == {"atlas-echo", "localhost"}

        identity = peer_identity_from_der(
            cert.public_bytes(__import__("cryptography").hazmat.primitives.serialization.Encoding.DER)
        )
        assert identity == ("atlas.echo", "abc123")

    def test_identity_comes_from_ca_not_csr(self, ca):
        """A CSR claiming to be someone else does not matter — identity is
        bound by the CA from the registration."""
        key_pem = generate_private_key_pem()
        csr = create_csr_pem(key_pem, "atlas.imposter")  # lies in the CSR subject
        cert_pem = ca.issue_from_csr(
            csr,
            service_name="atlas.echo",
            instance_id="real1",
            dns_names=["atlas-echo"],
            ttl_hours=1,
        )
        cert = x509.load_pem_x509_certificate(cert_pem)
        from cryptography.hazmat.primitives import serialization

        identity = peer_identity_from_der(cert.public_bytes(serialization.Encoding.DER))
        assert identity == ("atlas.echo", "real1")

    def test_garbage_csr_rejected(self, ca):
        with pytest.raises(ValueError):
            ca.issue_from_csr(
                b"not a csr", service_name="x.y", instance_id="i",
                dns_names=[], ttl_hours=1,
            )


class TestBlobSignatures:
    def test_sign_and_verify(self, ca):
        data = b"RECORD contents"
        sig = ca.sign_blob(data)
        assert ca.verify_blob(data, sig) is True

    def test_tampered_data_fails(self, ca):
        sig = ca.sign_blob(b"original")
        assert ca.verify_blob(b"tampered", sig) is False


class TestHostnames:
    def test_extracts_distinct_hosts(self):
        assert hostnames_from_urls(
            "https://atlas-echo:8100", "https://atlas-echo:8100/healthz",
        ) == ["atlas-echo"]

    def test_multiple_hosts(self):
        assert hostnames_from_urls(
            "https://atlas-echo:8100", "https://localhost:8100/healthz",
        ) == ["atlas-echo", "localhost"]
