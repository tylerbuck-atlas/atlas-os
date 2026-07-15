# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Plugin loading for Atlas Core.

Plugins extend Core without forking it: a Python package declaring an
entry point in the ``atlas_core.plugins`` group pointing at a subclass of
:class:`AtlasPlugin`. Core discovers, verifies, and starts plugins during
boot (stage 5) and stops them on shutdown.

**No unsigned plugins.** In mtls mode (default) every plugin's installed
distribution must carry an ``ATLAS-SIGNATURE`` file: an ECDSA signature
by the Atlas CA key over the distribution's ``RECORD`` (which itself
hashes every installed file). Sign with ``scripts/sign_plugin.py``.
Unsigned or tampered plugins are refused. Token (development) mode skips
verification unless ATLAS_REQUIRE_SIGNED_PLUGINS=true.
"""

from __future__ import annotations

import base64
import logging
from importlib import metadata

log = logging.getLogger("atlas.core.plugins")

ENTRY_POINT_GROUP = "atlas_core.plugins"
SIGNATURE_FILE = "ATLAS-SIGNATURE"


class AtlasPlugin:
    """Base class for Atlas Core plugins."""

    #: Stable plugin name, e.g. "atlas.plugin.metrics"
    name: str = "atlas.plugin.unnamed"
    version: str = "0.0.0"

    async def start(self, app) -> None:  # pragma: no cover - interface
        """Called during boot. `app` is the FastAPI application."""

    async def stop(self, app) -> None:  # pragma: no cover - interface
        """Called during shutdown."""


def verify_distribution_signature(dist: metadata.Distribution, verifier) -> bool:
    """True if the dist's RECORD carries a valid Atlas signature.

    `verifier(data: bytes, signature: bytes) -> bool` — normally the
    Atlas CA's verify_blob.
    """
    record = dist.read_text("RECORD")
    signature_b64 = dist.read_text(SIGNATURE_FILE)
    if record is None or signature_b64 is None:
        return False
    try:
        signature = base64.b64decode(signature_b64.strip())
    except Exception:
        return False
    return verifier(record.encode(), signature)


class PluginManager:
    """Discovers, verifies, and manages the lifecycle of installed plugins."""

    def __init__(self, *, require_signed: bool = False, verifier=None) -> None:
        self.require_signed = require_signed
        self._verifier = verifier
        self.plugins: list[AtlasPlugin] = []
        self.rejected: list[str] = []

    def _verify(self, entry_point) -> bool:
        if not self.require_signed:
            return True
        if self._verifier is None:
            log.error("plugin signing required but no verifier available")
            return False
        dist = entry_point.dist
        if dist is None:
            log.error("plugin %r: cannot locate distribution; refused", entry_point.name)
            return False
        if not verify_distribution_signature(dist, self._verifier):
            log.error(
                "plugin %r (%s): missing or invalid ATLAS-SIGNATURE; refused. "
                "Sign it with scripts/sign_plugin.py",
                entry_point.name, dist.metadata.get("Name", "?"),
            )
            return False
        return True

    def discover(self) -> list[AtlasPlugin]:
        discovered: list[AtlasPlugin] = []
        self.rejected = []
        for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
            if not self._verify(entry_point):
                self.rejected.append(entry_point.name)
                continue
            try:
                plugin_cls = entry_point.load()
                plugin = plugin_cls()
                if not isinstance(plugin, AtlasPlugin):
                    log.error("plugin %r does not subclass AtlasPlugin; skipped", entry_point.name)
                    continue
                discovered.append(plugin)
            except Exception:
                log.exception("failed to load plugin %r; skipped", entry_point.name)
        self.plugins = discovered
        return discovered

    async def start_all(self, app) -> None:
        for plugin in self.plugins:
            await plugin.start(app)
            log.info("plugin started: %s v%s", plugin.name, plugin.version)

    async def stop_all(self, app) -> None:
        for plugin in reversed(self.plugins):
            try:
                await plugin.stop(app)
            except Exception:
                log.exception("plugin %s failed to stop cleanly", plugin.name)
