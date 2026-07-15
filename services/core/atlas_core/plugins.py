"""Plugin loading for Atlas Core.

Plugins extend Core without forking it. A plugin is a Python package that
declares an entry point in the ``atlas_core.plugins`` group pointing at a
subclass of :class:`AtlasPlugin`. Core discovers, instantiates, and starts
plugins during boot (stage 5) and stops them on shutdown.

Milestone 1 ships the mechanism; the first-party plugin set arrives with
later milestones. Plugin signature verification lands in Milestone 3
(docs/security.md) — until then, installing a plugin into Core's
environment is an operator-level trust decision.
"""

from __future__ import annotations

import logging
from importlib import metadata

log = logging.getLogger("atlas.core.plugins")

ENTRY_POINT_GROUP = "atlas_core.plugins"


class AtlasPlugin:
    """Base class for Atlas Core plugins."""

    #: Stable plugin name, e.g. "atlas.plugin.metrics"
    name: str = "atlas.plugin.unnamed"
    version: str = "0.0.0"

    async def start(self, app) -> None:  # pragma: no cover - interface
        """Called during boot. `app` is the FastAPI application."""

    async def stop(self, app) -> None:  # pragma: no cover - interface
        """Called during shutdown."""


class PluginManager:
    """Discovers and manages the lifecycle of installed plugins."""

    def __init__(self) -> None:
        self.plugins: list[AtlasPlugin] = []

    def discover(self) -> list[AtlasPlugin]:
        discovered: list[AtlasPlugin] = []
        for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
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
