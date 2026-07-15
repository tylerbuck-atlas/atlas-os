# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Event Bus authentication.

Identity resolution is shared across Atlas services and lives in the SDK
(:mod:`atlas_sdk.service_auth`); this module re-exports it so bus code
and tests keep a stable import path.
"""

from atlas_sdk.service_auth import CoreIntrospector, Identity, require_identity

__all__ = ["CoreIntrospector", "Identity", "require_identity"]
