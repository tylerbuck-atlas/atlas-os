"""atlas.echo — the reference Atlas service.

The smallest possible service that fully satisfies the Atlas service
contract (docs/service-contract.md): identity, registration, heartbeats,
health endpoint, published capabilities, clean deregistration.
"""

__version__ = "0.2.0"

SERVICE_NAME = "atlas.echo"
