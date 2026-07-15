# Atlas OS — Service Contract v1

Every service that participates in Atlas — first-party or third-party —
must satisfy this contract. Atlas Core enforces the registration schema;
the rest is enforced by review and, later, by Sentinel.

## 1. Identity

- A stable service **name**: `atlas.<name>` for first-party,
  `<vendor>.<name>` for third-party. Lowercase, dot-separated,
  `[a-z0-9_.-]`, max 64 chars.
- A unique **instance ID** per running process (Core assigns one at
  registration).
- A semantic **version** (`MAJOR.MINOR.PATCH`).

## 2. Registration

On startup, after the service is able to serve `/healthz`:

```
POST {CORE_URL}/v1/registry/services
Authorization: Bearer {ATLAS_BOOTSTRAP_TOKEN}
Content-Type: application/json

{
  "name": "atlas.echo",
  "version": "0.1.0",
  "address": "http://atlas-echo:8100",
  "health_url": "http://atlas-echo:8100/healthz",
  "capabilities": ["echo.reply"],
  "metadata": {"description": "Example echo service"}
}
```

Response `201`:

```json
{
  "service": { "...": "registration record, including instance_id" },
  "service_token": "<returned exactly once — store it in memory>",
  "heartbeat_interval_seconds": 10
}
```

Registering an already-registered `name` again replaces the previous
instance (the old instance's token is revoked and its record superseded).
This makes restarts and redeploys safe by default.

## 3. Heartbeats

Every `heartbeat_interval_seconds` (from the registration response):

```
POST {CORE_URL}/v1/registry/services/{instance_id}/heartbeat
Authorization: Bearer {service_token}
```

Missing 3 consecutive intervals ⇒ Core marks the service `unreachable`.
Heartbeats from a superseded or deregistered instance return `401/410` —
on receiving either, a service must re-register.

## 4. Health endpoint

`GET /healthz` — unauthenticated, returns `200` with:

```json
{"status": "ok", "service": "atlas.echo", "version": "0.1.0"}
```

Return `200` only when actually able to serve. Core probes this URL
(default every 15 s); failures mark the service `unhealthy`.

## 5. Capabilities

Capabilities are dot-separated strings (`echo.reply`,
`devices.zwave.control`). Services publish them at registration and may
update them by re-registering. Discovery by capability:

```
GET {CORE_URL}/v1/registry/services?capability=echo.reply
Authorization: Bearer {service_token}
```

## 5a. Invocable capabilities (Milestone 5)

A capability that can be *acted on* (not just discovered) must be
invocable at the uniform endpoint:

```
POST {address}/v1/invoke/{capability}
Content-Type: application/json

{ ...capability-specific parameters... }
```

Return `2xx` + a JSON object on success; any other status is a failed
step. **This endpoint is how the Atlas Planner executes plan steps** —
services should treat calls to it as actions with consequences and may
verify that the caller is `atlas.planner` for capabilities with side
effects. Read-only capabilities (discovery, queries) need not implement
invocation.

## 6. Shutdown

On clean shutdown:

```
DELETE {CORE_URL}/v1/registry/services/{instance_id}
Authorization: Bearer {service_token}
```

Crash-stops are tolerated — the health monitor will notice.

## 7. Packaging

- Ships a `Dockerfile`; runs as a non-root user.
- Configured entirely via environment variables.
- Ships its own tests and its own `README`.
- Logs to stdout/stderr, one event per line.

## 8. Versioned APIs

All service APIs live under a version prefix (`/v1/...`). Breaking a
published contract requires a new prefix.
