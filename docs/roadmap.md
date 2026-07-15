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

## Milestone 4 — Atlas Memory + Asset Manager

- Durable, queryable system state
- File/document/manual ingestion with provenance metadata
- "Truth sources" become first-class, auditable objects

## Milestone 5 — Atlas Planner + Sentinel

- Goal → validated plan → auditable execution pipeline
- Policy engine: what may act on what, when
- Sentinel anomaly monitoring

## Milestone 6 — Device & Skill Managers

- Device abstraction layer (integration adapters live here — this is
  where MQTT/Zigbee/etc. finally appear, as adapters, not foundations)
- Skill packaging, signing, discovery

## Milestone 7 — The AI service

- The LLM joins as a registered service like any other
- Reads only from governed truth sources
- All proposed actions flow through the Planner. No exceptions.
