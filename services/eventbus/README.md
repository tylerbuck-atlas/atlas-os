# Atlas Event Bus

Durable, at-least-once inter-service messaging for Atlas OS
(`atlas.eventbus`). Design and semantics: [docs/eventbus.md](../../docs/eventbus.md).

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e "../../libs/atlas-sdk" && pip install -e ".[dev]"
ATLAS_BOOTSTRAP_TOKEN=<same secret Core uses> \
ATLAS_EVENTBUS_CORE_URL=http://localhost:8000 \
ATLAS_EVENTBUS_SELF_URL=http://localhost:8200 \
  python -m atlas_eventbus.main
```

## Test

```bash
pytest
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | liveness (unauthenticated) |
| POST | `/v1/events` | publish an event |
| POST | `/v1/subscriptions` | create/fetch a named subscription |
| GET | `/v1/subscriptions` | list own subscriptions |
| POST | `/v1/subscriptions/{id}/pull` | pull deliveries (long-poll) |
| POST | `/v1/subscriptions/{id}/ack` | acknowledge deliveries |
| DELETE | `/v1/subscriptions/{id}` | delete subscription |
| PUT | `/v1/schemas/{topic}` | register a schema version |
| GET | `/v1/schemas[/{topic}]` | inspect schemas |

All endpoints except `/healthz` require an authenticated identity. In
mtls mode (default) that is the caller's verified peer certificate; in
token (development) mode, a bearer token resolved through Core's
`/v1/auth/introspect`.
