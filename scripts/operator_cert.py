#!/usr/bin/env python3
# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Mint an operator certificate from the Atlas CA.

In mtls mode every authenticated API requires a client certificate. This
tool — usable only by whoever holds the CA key — issues the operator
identity (atlas://service/atlas.operator/...) for humans and tooling.

    python scripts/operator_cert.py --ca-dir data/ca --out ./operator
    curl --cert operator/operator.crt --key operator/operator.key \
         --cacert operator/ca.crt https://localhost:8000/v1/registry/services
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

from atlas_core.ca import CertificateAuthority  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca-dir", required=True)
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument("--ttl-hours", type=int, default=12)
    args = parser.parse_args()

    ca = CertificateAuthority(args.ca_dir)
    if ca.ensure():
        print("warning: CA did not exist and was created", file=sys.stderr)

    key_pem, cert_pem = ca.issue_self(
        common_name="atlas.operator",
        instance_id=f"manual-{uuid.uuid4().hex[:8]}",
        dns_names=[],
        ttl_hours=args.ttl_hours,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    (out / "operator.key").write_bytes(key_pem)
    os.chmod(out / "operator.key", 0o600)
    (out / "operator.crt").write_bytes(cert_pem)
    (out / "ca.crt").write_bytes(ca.cert_pem)
    print(f"operator credentials written to {out}/ (valid {args.ttl_hours}h)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
