# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""atlas.echo — the reference Atlas service.

The smallest possible service that fully satisfies the Atlas service
contract (docs/service-contract.md): identity, registration, heartbeats,
health endpoint, published capabilities, clean deregistration.
"""

__version__ = "0.3.0"

SERVICE_NAME = "atlas.echo"
