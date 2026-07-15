# Atlas Event Bus — Design & Semantics (v1)

The Event Bus (`atlas.eventbus`) is Atlas OS's asynchronous nervous
system: services publish facts about the world onto topics; other
services subscribe and react. It is a standalone Atlas service — it
registers with Core, heartbeats, and follows the same contract as
everything else.

## Delivery model

**Pull-based, durable, at-least-once.**

1. A producer `POST /v1/events` with a topic and payload. The bus stores
   the event durably, then fans it out: one *delivery* row per matching
   subscription.
2. A consumer `POST /v1/subscriptions/{id}/pull` to claim deliveries.
   Claimed deliveries become invisible for the **visibility timeout**
   (default 30 s).
3. The consumer processes the messages and `POST .../ack` with the
   delivery ids.
4. Unacked deliveries reappear after the visibility timeout, with an
   incremented `attempt` counter. Crash-stopped consumers lose nothing.

Consequences consumers must design for:

- **Duplicates are possible.** At-least-once means a delivery may arrive
  more than once (and Core's outbox may republish an event after a
  crash). Consumers deduplicate on `event_id` where it matters.
- **Ordering is per-subscription best effort.** Deliveries are handed
  out in insertion order, but redeliveries re-enter later. Strictly
  ordered processing requires consumer-side sequencing.
- **No replay (yet).** A subscription only receives events published
  after it was created. Historical replay is a candidate for the Memory
  milestone.

`pull` supports long-polling (`wait_seconds` up to 30): the request
parks until a message arrives or the window closes — event-driven
consumption without busy polling.

## Subscriptions

Named per service: `(service, name)` is unique and creation is
idempotent — re-subscribing on boot is safe and updates topic patterns.
Topic patterns: exact (`registry.service.registered`), prefix wildcard
(`registry.*`), or firehose (`*`).

## Topics

Dot-separated, lowercase, owned by the producing service:

| Topic | Producer | Meaning |
|---|---|---|
| `registry.service.registered` | atlas.core | A service instance registered |
| `registry.service.status_changed` | atlas.core | Lifecycle transition (healthy/unhealthy/unreachable/deregistered) |

## Schema registry

`PUT /v1/schemas/{topic}` registers a JSON Schema for a topic; versions
increment automatically and old versions are retained. When a topic has
a schema, publishes are validated against the **latest** version and
rejected (422) on mismatch; accepted events are stamped with
`schema_version`. Topics without schemas publish freely — register
schemas as contracts harden.

## Authentication

Zero Trust, no shared secrets with the bus:

- Callers present the service token Core issued them at registration.
- The bus resolves unknown tokens via Core's `POST /v1/auth/introspect`
  (authenticated with the bus's own service token) and caches verified
  identities for a short TTL.
- `source` on every event is set server-side from the verified identity —
  producers cannot impersonate each other.
- Subscriptions are scoped: only the owning service can pull, ack, or
  delete them.

Topic-level publish ACLs (e.g. only `atlas.core` may publish
`registry.*`) arrive with Sentinel; today identity is verified and
recorded, not restricted.

## Core's outbox

Core writes events to its own durable log first, then a publisher loop
forwards them to the bus in order, tracking a persisted cursor
(at-least-once across restarts). The bus is discovered through Core's own
registry — if no bus is registered, events wait in the outbox. See
`services/core/atlas_core/publisher.py`.

## Replaceability

The bus's contract is this document plus the API — not SQLite. The
storage layer (`store.py`) is swappable (Postgres, NATS JetStream, etc.)
without any consumer noticing, per the Atlas engineering rules.
