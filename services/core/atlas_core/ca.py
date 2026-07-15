# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""The Atlas Certificate Authority.

Core operates a private CA (docs/security.md). The root is generated at
first boot and persisted in the CA directory; every service receives a
short-lived certificate at registration, binding its identity —
``atlas://service/{name}/{instance_id}`` — into the SAN. Identity in a
certificate is always set by the CA from the registration it is binding;
nothing is copied from CSR attributes except the public key.

The CA key also signs plugin manifests (see plugins.py).
"""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from atlas_sdk.tls import identity_uri

log = logging.getLogger("atlas.core.ca")

ROOT_TTL_DAYS = 3650  # ~10 years for the root; leaves are short-lived


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def hostnames_from_urls(*urls: str) -> list[str]:
    """Distinct hostnames used to reach a service — its cert's DNS SANs."""
    names: list[str] = []
    for url in urls:
        host = urlparse(url).hostname
        if host and host not in names:
            names.append(host)
    return names


class CertificateAuthority:
    """File-backed private CA."""

    def __init__(self, ca_dir: str | Path) -> None:
        self._dir = Path(ca_dir)
        self._key: ec.EllipticCurvePrivateKey | None = None
        self._cert: x509.Certificate | None = None

    # -- lifecycle -----------------------------------------------------------

    def ensure(self) -> bool:
        """Load the CA, creating it on first boot. Returns True if created."""
        self._dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)
        key_path, cert_path = self._dir / "ca.key", self._dir / "ca.crt"
        if key_path.exists() and cert_path.exists():
            self._key = serialization.load_pem_private_key(
                key_path.read_bytes(), password=None
            )
            self._cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            log.info("Atlas CA loaded (serial %x)", self._cert.serial_number)
            return False

        self._key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Atlas OS"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Atlas Root CA"),
        ])
        now = _utcnow()
        self._cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=ROOT_TTL_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_cert_sign=True, crl_sign=True,
                    content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .sign(self._key, hashes.SHA256())
        )
        key_path.touch(mode=0o600, exist_ok=True)
        key_path.write_bytes(self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        os.chmod(key_path, 0o600)
        cert_path.write_bytes(self._cert.public_bytes(serialization.Encoding.PEM))
        log.warning("Atlas CA CREATED at %s — back up ca.key securely", self._dir)
        return True

    @property
    def cert_pem(self) -> bytes:
        assert self._cert is not None, "CA not initialized"
        return self._cert.public_bytes(serialization.Encoding.PEM)

    # -- issuance --------------------------------------------------------------

    def issue_from_csr(
        self,
        csr_pem: bytes,
        *,
        service_name: str,
        instance_id: str,
        dns_names: list[str],
        ttl_hours: int,
    ) -> bytes:
        """Sign a service certificate from a CSR's public key.

        SAN = the service's reachable hostnames + its identity URI.
        EKU allows both server and client auth (services are both).
        """
        assert self._key is not None and self._cert is not None
        csr = x509.load_pem_x509_csr(csr_pem)
        if not csr.is_signature_valid:
            raise ValueError("CSR signature invalid (no proof of key possession)")

        san: list[x509.GeneralName] = [x509.DNSName(h) for h in dns_names]
        san.append(x509.UniformResourceIdentifier(identity_uri(service_name, instance_id)))
        now = _utcnow()
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, service_name)]))
            .issuer_name(self._cert.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(hours=ttl_hours))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
                ),
                critical=False,
            )
            .sign(self._key, hashes.SHA256())
        )
        log.info(
            "issued certificate: %s (%s), ttl=%dh, dns=%s",
            service_name, instance_id, ttl_hours, dns_names,
        )
        return cert.public_bytes(serialization.Encoding.PEM)

    def issue_self(
        self, *, common_name: str, instance_id: str, dns_names: list[str], ttl_hours: int
    ) -> tuple[bytes, bytes]:
        """Key + certificate for Core itself (and for operator tooling)."""
        key_pem = _new_key_pem()
        csr_pem = _quick_csr(key_pem, common_name)
        cert_pem = self.issue_from_csr(
            csr_pem,
            service_name=common_name,
            instance_id=instance_id,
            dns_names=dns_names,
            ttl_hours=ttl_hours,
        )
        return key_pem, cert_pem

    # -- detached signatures (plugin manifests) ------------------------------------

    def sign_blob(self, data: bytes) -> bytes:
        assert self._key is not None
        return self._key.sign(data, ec.ECDSA(hashes.SHA256()))

    def verify_blob(self, data: bytes, signature: bytes) -> bool:
        assert self._cert is not None
        try:
            self._cert.public_key().verify(signature, data, ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False


def _new_key_pem() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _quick_csr(key_pem: bytes, common_name: str) -> bytes:
    key = serialization.load_pem_private_key(key_pem, password=None)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM)
