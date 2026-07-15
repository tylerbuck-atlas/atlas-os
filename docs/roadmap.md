# Atlas OS — Roadmap

Build the operating system first. No AI, no Home Assistant, no MQTT until
the foundation is real.

## Milestone 1 — Atlas Core boot ✅ (this repository)

- Repository, documentation, Docker Compose
- Atlas Core: configuration, identity, registry, discovery
- Service registration with bootstrap-token auth + per-service tokens
- Health monitoring: heartbeats (push) + probes (pull)
- Plugin loading mechanism (entry points)
- Deterministic boot sequence ending in `Atlas Ready.`
- Example service (`atlas.echo`) proving the service contract
- Test suite

## Milestone 2 — Atlas Event Bus ✅

- Standalone Event Bus service (`atlas.eventbus`)
- Core publishes its existing registry/health events onto the bus
  (durable outbox + persisted cursor, discovered via Core's own registry)
- Event schema registry + versioning, validated at publish time
- At-least-once delivery, per-service subscriptions, wildcard topics,
  long-polling pull, visibility-timeout redelivery
- Token introspection API in Core (`/v1/auth/introspect`)
- `atlas-sdk` shared library (registration client + bus client)

## Milestone 3 — Zero Trust hardening ✅

- Atlas CA in Core: root at first boot, CSR enrollment at registration,
  24-hour service certificates carrying `atlas://service/{name}/{instance}`
- mTLS for all service↔service and service↔Core traffic; identity from
  verified peer certificates; auto re-enrollment at 2/3 cert lifetime
  with hot TLS reload; revocation via registry state
- Token auth retired (bootstrap token = enrollment only; `token` mode
  kept for development)
- Signed plugin verification (CA-signed dist RECORDs) + operator certs

## Milestone 4 — Atlas Memory + Asset Manager ✅

- `atlas.memory`: durable, versioned, queryable facts with required
  provenance and enforced data classes; registry events materialized
  into `system.services` state; class-redacted change events
- `atlas.assets`: content-addressed (sha256) file store with integrity
  verification on every read, dedup, tombstones + blob GC
- Truth sources are first-class, auditable objects; the 4-class privacy
  model is live in both schemas
- SDK grows shared service auth + the durable BusOutbox

## Milestone 5 — Atlas Planner + Sentinel ✅

- `atlas.planner`: goal → policy validation → (approval) → auditable
  step-by-step execution against the uniform capability-invocation
  contract; **default deny**; operator-only policies and approvals;
  immutable plan/step audit trail; full lifecycle on the bus
- `atlas.sentinel`: consumes registry.*/planner.* events; service-down,
  flapping, policy-rejection and probing rules with alert dedup; alert
  store + ack API; sentinel.alert.raised events

## Milestone 6 — Device & Skill Managers

- Device abstraction layer (integration adapters live here — this is
  where MQTT/Zigbee/etc. finally appear, as adapters, not foundations)
- Skill packaging, signing, discovery

## Milestone 7 — The AI service

- The LLM joins as a registered service like any other
- Reads only from governed truth sources
- All proposed actions flow through the Planner. No exceptions.
