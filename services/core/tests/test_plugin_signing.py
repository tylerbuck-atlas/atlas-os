# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""No unsigned plugins: distribution signature verification."""

from __future__ import annotations

import base64

import pytest

from atlas_core.ca import CertificateAuthority
from atlas_core.plugins import PluginManager, verify_distribution_signature


class FakeDist:
    """Stands in for importlib.metadata.Distribution."""

    def __init__(self, record: str | None, signature: str | None) -> None:
        self._files = {"RECORD": record, "ATLAS-SIGNATURE": signature}
        self.metadata = {"Name": "fake-plugin"}

    def read_text(self, name: str):
        return self._files.get(name)


@pytest.fixture
def ca(tmp_path):
    authority = CertificateAuthority(tmp_path / "ca")
    authority.ensure()
    return authority


def _signed_dist(ca, record="file.py,sha256=abc,123\n") -> FakeDist:
    sig = base64.b64encode(ca.sign_blob(record.encode())).decode()
    return FakeDist(record, sig)


class TestSignatureVerification:
    def test_valid_signature_accepted(self, ca):
        assert verify_distribution_signature(_signed_dist(ca), ca.verify_blob) is True

    def test_missing_signature_rejected(self, ca):
        dist = FakeDist("record", None)
        assert verify_distribution_signature(dist, ca.verify_blob) is False

    def test_tampered_record_rejected(self, ca):
        dist = _signed_dist(ca)
        dist._files["RECORD"] = "malicious.py,sha256=evil,666\n"
        assert verify_distribution_signature(dist, ca.verify_blob) is False

    def test_signature_from_wrong_key_rejected(self, ca, tmp_path):
        other = CertificateAuthority(tmp_path / "other-ca")
        other.ensure()
        record = "file.py,sha256=abc,123\n"
        sig = base64.b64encode(other.sign_blob(record.encode())).decode()
        assert verify_distribution_signature(FakeDist(record, sig), ca.verify_blob) is False

    def test_garbage_signature_rejected(self, ca):
        assert verify_distribution_signature(FakeDist("r", "!!!"), ca.verify_blob) is False


class TestPluginManagerPolicy:
    class FakeEntryPoint:
        name = "fake"

        def __init__(self, dist):
            self.dist = dist

    def test_unsigned_refused_when_required(self, ca):
        manager = PluginManager(require_signed=True, verifier=ca.verify_blob)
        assert manager._verify(self.FakeEntryPoint(FakeDist("r", None))) is False

    def test_signed_accepted_when_required(self, ca):
        manager = PluginManager(require_signed=True, verifier=ca.verify_blob)
        assert manager._verify(self.FakeEntryPoint(_signed_dist(ca))) is True

    def test_anything_accepted_when_not_required(self, ca):
        manager = PluginManager(require_signed=False)
        assert manager._verify(self.FakeEntryPoint(FakeDist("r", None))) is True

    def test_required_without_verifier_refuses(self):
        manager = PluginManager(require_signed=True, verifier=None)
        assert manager._verify(self.FakeEntryPoint(FakeDist("r", "sig"))) is False
