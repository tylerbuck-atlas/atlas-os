# atlas-sdk

The client library Atlas services use to satisfy the
[service contract](../../docs/service-contract.md) and talk to the
[Event Bus](../../docs/eventbus.md).

- `AtlasService` — register with Core, heartbeat, re-register on token
  invalidation, deregister on shutdown.
- `EventBusClient` — publish events; create subscriptions; pull and ack
  deliveries.
- `discover_service` — find services by name or capability via Core.

A library, not a service: it keeps services decoupled — all communication
still flows through Core's APIs and the Bus.

```python
from atlas_sdk import AtlasService, EventBusClient, discover_service

atlas = AtlasService(
    name="atlas.example", version="0.1.0",
    address="http://atlas-example:9000",
    health_url="http://atlas-example:9000/healthz",
    capabilities=["example.demo"],
    core_url="http://atlas-core:8000",
    bootstrap_token=BOOTSTRAP_TOKEN,
)
await atlas.start()

buses = await discover_service(
    core_url=atlas.core_url, token=atlas.service_token, name="atlas.eventbus"
)
bus = EventBusClient(buses[0]["address"], atlas.service_token)
sub_id = await bus.ensure_subscription("example-main", ["registry.*"])
for message in await bus.pull(sub_id, wait_seconds=20):
    handle(message["event"])
    await bus.ack(sub_id, [message["delivery_id"]])
```
