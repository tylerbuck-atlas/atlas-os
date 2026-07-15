#!/usr/bin/env python3
# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Sign an installed plugin distribution with the Atlas CA key.

Writes ATLAS-SIGNATURE (base64 ECDSA-SHA256 over the dist's RECORD) into
the distribution's .dist-info directory. Run wherever the plugin is
installed, with access to the CA directory.

    python scripts/sign_plugin.py --ca-dir data/ca --dist my-atlas-plugin
"""

from __future__ import annotations

import argparse
import base64
import sys
from importlib import metadata
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

SIGNATURE_FILE = "ATLAS-SIGNATURE"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca-dir", required=True, help="Atlas CA directory (holds ca.key)")
    parser.add_argument("--dist", required=True, help="Installed distribution name to sign")
    args = parser.parse_args()

    key_path = Path(args.ca_dir) / "ca.key"
    if not key_path.exists():
        print(f"error: {key_path} not found", file=sys.stderr)
        return 1
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)

    try:
        dist = metadata.distribution(args.dist)
    except metadata.PackageNotFoundError:
        print(f"error: distribution {args.dist!r} is not installed", file=sys.stderr)
        return 1

    record = dist.read_text("RECORD")
    if record is None:
        print("error: distribution has no RECORD file", file=sys.stderr)
        return 1

    signature = key.sign(record.encode(), ec.ECDSA(hashes.SHA256()))
    dist_info = Path(str(dist.locate_file(""))) / f"{dist.metadata['Name'].replace('-', '_')}-{dist.version}.dist-info"
    if not dist_info.exists():
        # Fall back to searching for the dist-info that holds RECORD
        candidates = [p.parent for p in Path(str(dist.locate_file(""))).glob("*.dist-info/RECORD")]
        if not candidates:
            print("error: cannot locate .dist-info directory", file=sys.stderr)
            return 1
        dist_info = candidates[0]

    out = dist_info / SIGNATURE_FILE
    out.write_text(base64.b64encode(signature).decode() + "\n")
    print(f"signed: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
