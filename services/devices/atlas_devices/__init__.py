# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Atlas Device Manager — the home's devices behind one abstraction.

Protocols (MQTT, Zigbee, Z-Wave, …) live in *adapters* — separate Atlas
services that speak their protocol and sync devices here. Adapters are
plug-ins to the home, never foundations of it.
"""

__version__ = "0.1.0"

SERVICE_NAME = "atlas.devices"
