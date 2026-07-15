# ATLAS OS

**An AI-native distributed operating system.**

Atlas is not a chatbot. Atlas is not a home-automation platform. Atlas is an
operating system for distributed, AI-augmented environments in which the AI
model is just one service among many — and never the source of truth.

> Truth comes from sensors, databases, APIs, manuals, files, measurements,
> and user input. The LLM reasons over trusted data. It does not invent it.

## Status

**Milestone 2 — Atlas Event Bus.** Implemented so far:

- **Atlas Core** — service discovery, registration, health monitoring,
  configuration, token authentication + introspection, plugin loading,
  and a coordinated boot sequence that ends with a single log line:
  `Atlas Ready.`
- **Atlas Event Bus** — durable at-least-once pub/sub with per-service
  subscriptions, wildcard topics, long-polling pull/ack, a versioned
  schema registry, and Core's registry/health events flowing through it
  via a durable outbox.
- **atlas-sdk** — the client library services use to register,
  heartbeat, and consume the bus.

Nothing more is implemented yet — deliberately. See [docs/roadmap.md](docs/roadmap.md).

## Architecture at a glance

Atlas consists of independent services that communicate **only** through the
Event Bus or versioned APIs. No hidden dependencies. No direct imports
between services.

| Service              | Responsibility                                    | Status      |
|----------------------|---------------------------------------------------|-------------|
| **Atlas Core**       | Discovery, registration, health, config, auth     | ✅ Milestone 1 |
| Atlas Planner        | Validates and plans every action                  | planned     |
| Atlas Memory         | Durable, queryable state                          | planned     |
| **Atlas Event Bus**  | Inter-service messaging                           | ✅ Milestone 2 |
| Atlas Skill Manager  | Capability packages                               | planned     |
| Atlas Device Manager | Physical/virtual device abstraction               | planned     |
| Atlas Asset Manager  | Files, manuals, documents                         | planned     |
| Atlas Sentinel       | Security monitoring and enforcement               | planned     |

Full details in [docs/architecture.md](docs/architecture.md).

## Quick start

Requirements: Docker with the Compose plugin.

```bash
cp .env.example .env          # set ATLAS_BOOTSTRAP_TOKEN to a strong secret
docker compose up --build
```

Watch the logs. When Core has booted, verified its registry, and the health
monitor is live, you will see:

```
atlas-core | Atlas Ready.
```

The example echo service then registers itself and begins heartbeating.
Inspect the system:

```bash
# Core's own health
curl http://localhost:8000/v1/system/health

# Everything Core knows about (requires the bootstrap token)
curl -H "Authorization: Bearer $ATLAS_BOOTSTRAP_TOKEN" \
     http://localhost:8000/v1/registry/services
```

Interactive API docs: http://localhost:8000/docs

## Running the tests

```bash
cd services/core
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Engineering rules

Every Atlas service, forever:

- **Event-driven** — state changes are events, not side effects.
- **Identified** — every service has a stable identity (`atlas.<name>`), an instance ID, and a version.
- **Discoverable** — capabilities are published to Core at registration.
- **Replaceable** — services speak versioned APIs; any implementation may be swapped.
- **Versioned** — APIs are versioned (`/v1/...`); services declare their own versions.
- **Testable** — every service ships its own test suite.
- **Documented** — every service ships its own docs.
- **Independent** — every service runs (and fails) on its own.
- **Health-checked** — every service exposes `GET /healthz`.
- **Containerized** — every service ships a Dockerfile.

## Security

Zero Trust. Every API call is authenticated; there are no anonymous
services. Milestone 1 uses bootstrap-token authentication with per-service
issued tokens; the certificate/mTLS design it evolves into is specified in
[docs/security.md](docs/security.md). LLM outputs are **never** executed
directly — the Planner validates every action (future milestone).

## Repository layout

```
atlas-os/
├── docs/                      # Architecture, security, contracts, eventbus, roadmap
├── libs/
│   └── atlas-sdk/             # Client library (registration + bus clients)
├── services/
│   ├── core/                  # Atlas Core (FastAPI)
│   ├── eventbus/              # Atlas Event Bus
│   └── examples/echo/         # Minimal service demonstrating the contract
├── docker-compose.yml
└── .env.example
```

## License

TBD.
