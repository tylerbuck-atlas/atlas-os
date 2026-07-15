# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""atlas.adapter.virtual — the reference device adapter.

Simulates a small household (a light, a temperature sensor, a Class-3
presence sensor) and demonstrates the full adapter contract without any
hardware: device sync, command execution, and state reporting.
"""

__version__ = "0.1.0"

SERVICE_NAME = "atlas.adapter.virtual"
