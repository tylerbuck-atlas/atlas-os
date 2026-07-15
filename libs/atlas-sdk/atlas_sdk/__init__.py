# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.

"""Atlas OS SDK.

The client library every Atlas service uses to satisfy the service
contract (docs/service-contract.md) and to talk to the Event Bus.
This is a library, not a service: importing it does not couple services
to each other — all communication still flows through Core and the Bus.
"""

from .client import AtlasService, EventBusClient, discover_service
from .outbox import BusOutbox
from .service_auth import CoreIntrospector, Identity, require_identity
from .tls import (
    MTLSProtocol,
    TLSRuntime,
    create_csr_pem,
    generate_private_key_pem,
    identity_uri,
    parse_identity_uri,
    peer_cert_der_for_scope,
    peer_identity_from_der,
)

__version__ = "0.3.0"

__all__ = [
    "AtlasService", "EventBusClient", "discover_service",
    "BusOutbox", "CoreIntrospector", "Identity", "require_identity",
    "MTLSProtocol", "TLSRuntime", "create_csr_pem", "generate_private_key_pem",
    "identity_uri", "parse_identity_uri", "peer_cert_der_for_scope",
    "peer_identity_from_der", "__version__",
]
