"""Atlas OS SDK.

The client library every Atlas service uses to satisfy the service
contract (docs/service-contract.md) and to talk to the Event Bus.
This is a library, not a service: importing it does not couple services
to each other — all communication still flows through Core and the Bus.
"""

from .client import AtlasService, EventBusClient, discover_service

__version__ = "0.2.0"

__all__ = ["AtlasService", "EventBusClient", "discover_service", "__version__"]
