# Atlas OS — Architecture

## 1. What Atlas is

Atlas OS is an AI-native **distributed operating system**. It manages
services, devices, assets, and knowledge across one or more nodes. An AI
model is one service inside the system — a reasoning engine over trusted
data — never the kernel and never the source of truth.

### Core philosophy

The LLM is never the source of truth. Truth comes from sensors, databases,
APIs, manuals, files, measurements, and user input. Every Atlas design
decision follows from this: data flows from trusted sources into governed
stores; the model consumes and reasons; the Planner validates anything the
model proposes before it can touch the world.

Atlas is **local-first and offline-capable**: it must boot, run, and
serve its home with zero internet connectivity. Anything that requires
the internet is an adapter — explicitly installed, explicitly granted
egress, individually removable. Enforcement is at the network layer, not
in documentation. The binding commitment is [privacy.md](privacy.md).

## 2. Services

Atlas is composed of independent services. Each runs in its own container,
owns its own state, and communicates only through the Event Bus or defined,
versioned APIs.

| Service | Identity | Responsibility |
|---|---|---|
| Atlas Core | `atlas.core` | Service registry, discovery, health monitoring, configuration, authentication, plugin loading, boot coordination |
| Atlas Planner | `atlas.planner` | Turns goals into validated, auditable action plans; the only path from intent to execution |
| Atlas Memory | `atlas.memory` | Durable, queryable system state and knowledge (implemented — see [memory-assets.md](memory-assets.md)) |
| Atlas Event Bus | `atlas.eventbus` | Asynchronous inter-service messaging (implemented — see [eventbus.md](eventbus.md)) |
| Atlas Skill Manager | `atlas.skills` | Discoverable, versioned capability packages |
| Atlas Device Manager | `atlas.devices` | Abstraction over physical and virtual devices |
| Atlas Asset Manager | `atlas.assets` | Files, manuals, documents, and their metadata (implemented — see [memory-assets.md](memory-assets.md)) |
| Atlas Sentinel | `atlas.sentinel` | Security monitoring, anomaly detection, enforcement |

### Communication rules

1. Services communicate **only** via the Event Bus or versioned HTTP APIs.
2. No direct imports between services. Ever. Each service is its own
   Python package with its own dependencies and its own container.
3. Every API is versioned under a `/v1/`-style prefix. Breaking changes
   require a new version prefix, and old versions are deprecated on a
   published schedule.
4. Services discover each other through Atlas Core — never through
   hard-coded addresses (Compose/DNS hostnames are acceptable only for
   reaching Core itself, which is the bootstrap point).

## 3. Atlas Core (Milestone 1 — implemented)

Atlas Core is the bootstrap point of the operating system. It is
intentionally boring, dependency-light, and durable.

### Responsibilities

- **Registration** — services announce identity, version, address,
  capabilities, and health endpoint.
- **Discovery** — authenticated services query the registry to find each
  other by name or capability.
- **Health monitoring** — dual model:
  - *Heartbeats (push):* services `POST /v1/registry/services/{id}/heartbeat`
    on an agreed interval (default 10 s). Missing 3 consecutive intervals
    marks the service `unreachable`.
  - *Probes (pull):* Core polls each service's declared `health_url`
    (default every 15 s). A failing probe marks the service `unhealthy`.
- **Configuration** — Core's own configuration comes from environment
  variables (12-factor). Distributed config for other services is a later
  milestone.
- **Authentication** — see [security.md](security.md). Milestone 1:
  bootstrap token to register, per-service issued tokens thereafter.
- **Boot coordination** — deterministic boot sequence (below).
- **Plugin loading** — Core loads plugins declared via entry points at
  boot (the mechanism exists in Milestone 1; the first-party plugin set is
  a later milestone).

### Boot sequence

Boot is explicit, ordered, and observable. Each stage is logged and the
current stage is queryable at `GET /v1/system/status`.

```
1. CONFIG      load + validate configuration; refuse to boot on invalid config
2. IDENTITY    establish atlas.core identity and instance ID
3. REGISTRY    initialize the service registry store
4. AUTH        initialize the token service (verify bootstrap secret present)
5. PLUGINS     discover and load registered plugins
6. HEALTH      start heartbeat watchdog + active probe loop
7. API         mount versioned API routers; begin serving
8. READY       log exactly: "Atlas Ready."
```

If any stage fails, Core logs the failing stage and exits non-zero. There
is no degraded half-booted mode.

### Service lifecycle

```
            register            heartbeat ok / probe ok
  (unknown) ────────► STARTING ─────────────────────────► HEALTHY
                                                           │  ▲
                                        missed heartbeats  │  │ recovery
                                                           ▼  │
                                                        UNREACHABLE
                                                           │
                                          failing probes   ▼
                                                        UNHEALTHY
                          deregister / eviction
  any state ────────────────────────────────────────► DEREGISTERED
```

States: `starting`, `healthy`, `unhealthy`, `unreachable`, `deregistered`.
Transitions are recorded as events in Core's durable event log (the
outbox) and forwarded, in order, to the Atlas Event Bus by Core's
publisher loop (see [eventbus.md](eventbus.md)). The bus is discovered
through Core's own registry — no configured address, no hidden
dependency; without a bus, events wait in the outbox.

## 4. The service contract

Every Atlas service — first-party or third-party — must satisfy
[service-contract.md](service-contract.md). Summary:

- has a stable identity `atlas.<name>` (or `vendor.<name>` for third party)
- registers with Core at startup and deregisters on clean shutdown
- heartbeats on the negotiated interval
- exposes `GET /healthz` returning `200` + JSON when able to serve
- publishes its capabilities at registration
- authenticates every inbound API call
- is containerized, versioned, tested, and documented

## 5. Design decisions & rationale (Milestone 1)

**Core-hosted registry (push + probe), no external registry.** Consul/etcd
would add a hard infrastructure dependency to every one of the thousands of
future installs. Core's registry is a few hundred lines, has zero external
dependencies, and the API is what matters — if scale ever demands it, the
storage behind `/v1/registry` can be swapped without any service noticing
(replaceability rule).

**SQLite for registry persistence.** Registry state survives Core restarts
without requiring a database server. The storage layer is behind a small
interface; Postgres can replace it later with no API change.

**No Event Bus in Milestone 1.** The milestone was Core booting and
monitoring. Registry state changes were written to an internal event log
with the same shape they now have on the bus — Milestone 2 published
existing events rather than inventing new ones.

**No AI in Milestones 1–2.** By design. The operating system comes first.

## 6. Atlas Event Bus (Milestone 2 — implemented)

Durable, pull-based, at-least-once pub/sub with named per-service
subscriptions, wildcard topic patterns, long-polling, and a versioned
schema registry that validates payloads at publish time. Callers
authenticate with their Core-issued service tokens; the bus verifies
them through Core's `/v1/auth/introspect` and never holds secrets of its
own. Full design: [eventbus.md](eventbus.md).
