# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Skill manifest signing and verification.

A skill manifest is signed by the Atlas CA key (the same root of trust
as service certificates and plugin signatures): ECDSA-SHA256 over the
canonical JSON of the manifest *without* its ``signature`` field —
sorted keys, compact separators. **No unsigned skills**: the Skill
Manager refuses manifests whose signature does not verify against the
Atlas CA certificate.
"""

from __future__ import annotations

import base64
import json

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def canonical_manifest_bytes(manifest: dict) -> bytes:
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()


def sign_manifest(manifest: dict, ca_key) -> str:
    """Returns the base64 signature (used by scripts/sign_skill.py)."""
    signature = ca_key.sign(canonical_manifest_bytes(manifest), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode()


def verify_manifest(manifest: dict, ca_cert_pem: bytes) -> bool:
    signature_b64 = manifest.get("signature")
    if not signature_b64:
        return False
    try:
        signature = base64.b64decode(signature_b64)
    except Exception:
        return False
    cert = x509.load_pem_x509_certificate(ca_cert_pem)
    try:
        cert.public_key().verify(
            signature, canonical_manifest_bytes(manifest), ec.ECDSA(hashes.SHA256())
        )
        return True
    except InvalidSignature:
        return False
