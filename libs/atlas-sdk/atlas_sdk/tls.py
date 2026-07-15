# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""TLS primitives shared by every Atlas service.

- key + CSR generation (enrollment happens at registration; see
  docs/security.md)
- the ``atlas://service/{name}/{instance_id}`` identity URI carried in
  certificate SANs
- :class:`MTLSProtocol`, a uvicorn HTTP protocol that records each
  connection's *verified* peer certificate so request handlers can
  resolve the caller's identity (uvicorn does not expose peer certs to
  ASGI apps natively)
- runtime cert-file management with owner-only permissions
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from uvicorn.protocols.http.h11_impl import H11Protocol

IDENTITY_SCHEME = "atlas"
IDENTITY_PREFIX = "atlas://service/"


# -- keys & CSRs -------------------------------------------------------------

def generate_private_key_pem() -> bytes:
    """A fresh EC P-256 private key (PEM, unencrypted — protect the file)."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def create_csr_pem(key_pem: bytes, common_name: str) -> bytes:
    """A CSR proving possession of the key. Identity in the final
    certificate is set by the CA, never taken from the CSR."""
    key = serialization.load_pem_private_key(key_pem, password=None)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM)


# -- identity ------------------------------------------------------------------

def identity_uri(name: str, instance_id: str) -> str:
    return f"{IDENTITY_PREFIX}{name}/{instance_id}"


def parse_identity_uri(uri: str) -> tuple[str, str] | None:
    """atlas://service/{name}/{instance_id} → (name, instance_id)."""
    if not uri.startswith(IDENTITY_PREFIX):
        return None
    rest = uri[len(IDENTITY_PREFIX):]
    parts = rest.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def peer_identity_from_der(der: bytes) -> tuple[str, str] | None:
    """Extract (service_name, instance_id) from a verified peer cert."""
    cert = x509.load_der_x509_certificate(der)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    for uri in san.get_values_for_type(x509.UniformResourceIdentifier):
        parsed = parse_identity_uri(uri)
        if parsed:
            return parsed
    return None


def cert_not_valid_after(cert_pem: bytes) -> datetime:
    cert = x509.load_pem_x509_certificate(cert_pem)
    return cert.not_valid_after_utc


def cert_seconds_remaining(cert_pem: bytes) -> float:
    return (cert_not_valid_after(cert_pem) - datetime.now(timezone.utc)).total_seconds()


# -- uvicorn peer-cert capture ---------------------------------------------------

class MTLSProtocol(H11Protocol):
    """uvicorn H11 protocol that records each connection's verified peer
    certificate, keyed by the client (ip, port).

    The TLS handshake (ssl_cert_reqs) is what *verifies* the cert against
    the Atlas CA; this class only makes the already-verified cert visible
    to the application so it can read the identity SAN.
    """

    peer_certs: dict[tuple, bytes] = {}

    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        ssl_obj = transport.get_extra_info("ssl_object")
        if ssl_obj is not None and self.client:
            der = ssl_obj.getpeercert(binary_form=True)
            if der:
                MTLSProtocol.peer_certs[tuple(self.client)] = der

    def connection_lost(self, exc) -> None:
        if self.client:
            MTLSProtocol.peer_certs.pop(tuple(self.client), None)
        super().connection_lost(exc)


def peer_cert_der_for_scope(scope) -> bytes | None:
    client = scope.get("client")
    if not client:
        return None
    return MTLSProtocol.peer_certs.get(tuple(client))


# -- runtime credential files -------------------------------------------------------

@dataclass
class TLSRuntime:
    """A service's live TLS material on disk (owner-only permissions)."""

    directory: Path
    key_path: Path
    cert_path: Path
    ca_path: Path

    @classmethod
    def prepare(cls, directory: str | Path) -> "TLSRuntime":
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        return cls(d, d / "service.key", d / "service.crt", d / "ca.crt")

    def write(self, *, key_pem: bytes | None, cert_pem: bytes, ca_pem: bytes) -> None:
        if key_pem is not None:
            self.key_path.touch(mode=0o600, exist_ok=True)
            self.key_path.write_bytes(key_pem)
            os.chmod(self.key_path, 0o600)
        self.cert_path.write_bytes(cert_pem)
        self.ca_path.write_bytes(ca_pem)

    def client_ssl_context(self) -> ssl.SSLContext:
        """Context for *outbound* mTLS: trust the Atlas CA, present our cert."""
        ctx = ssl.create_default_context(cafile=str(self.ca_path))
        ctx.load_cert_chain(str(self.cert_path), str(self.key_path))
        return ctx

    def uvicorn_kwargs(self) -> dict:
        """Server-side settings: serve our cert, request (and verify when
        presented) peer certs against the Atlas CA. OPTIONAL — not
        REQUIRED — so `/healthz` stays reachable by credential-less
        infrastructure; protected routes enforce identity per request."""
        return {
            "ssl_certfile": str(self.cert_path),
            "ssl_keyfile": str(self.key_path),
            "ssl_ca_certs": str(self.ca_path),
            "ssl_cert_reqs": ssl.CERT_OPTIONAL,
            "http": MTLSProtocol,
        }
