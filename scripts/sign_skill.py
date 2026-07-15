#!/usr/bin/env python3
# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Sign a skill manifest with the Atlas CA key.

Reads a manifest JSON file, computes the canonical signature, and writes
the manifest back with its ``signature`` field set — ready to publish to
the Skill Manager.

    python scripts/sign_skill.py --ca-dir data/ca --manifest skill.json
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca-dir", required=True)
    parser.add_argument("--manifest", required=True, help="Manifest JSON file (updated in place)")
    args = parser.parse_args()

    key_path = Path(args.ca_dir) / "ca.key"
    if not key_path.exists():
        print(f"error: {key_path} not found", file=sys.stderr)
        return 1
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text())
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    signature = key.sign(canonical, ec.ECDSA(hashes.SHA256()))
    manifest["signature"] = base64.b64encode(signature).decode()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"signed: {manifest_path} ({manifest['name']} {manifest['version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
