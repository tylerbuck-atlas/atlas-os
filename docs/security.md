# Atlas OS — Security Model

Atlas is Zero Trust from the first commit: **no anonymous services, no
unauthenticated APIs, no direct execution of LLM output.** This document
describes what is enforced today (Milestone 1) and the certificate-based
model it evolves into.

## Principles

1. Every API call is authenticated. There are no anonymous endpoints except
   `GET /healthz` and `GET /v1/system/health` (liveness must be checkable
   by infrastructure that holds no credentials).
2. Every service has an identity. Requests are attributable to a service
   instance, not just "someone with a key".
3. Secrets are injected via environment/secret stores — never committed,
   never baked into images.
4. LLM outputs are data, not instructions. Only the Planner can turn a
   proposal into an action, and only after validation (future milestone).
5. No unsigned firmware, no unsigned plugins (enforcement lands with the
   Skill/Device managers).

## Milestone 1 — token authentication (implemented)

### Bootstrap

- Core refuses to boot unless `ATLAS_BOOTSTRAP_TOKEN` is set to a
  non-default value (minimum 16 characters).
- The bootstrap token is the *only* credential that can call
  `POST /v1/registry/services` (register).

### Per-service tokens

- On successful registration, Core issues the service a unique, random
  256-bit **service token**, returned exactly once in the registration
  response.
- Only a SHA-256 hash of the token is stored server-side; a database leak
  does not leak credentials.
- The service authenticates all subsequent calls (heartbeat, discovery,
  deregistration) with `Authorization: Bearer <service-token>`.
- A service token is scoped to its own registration: service A's token
  cannot heartbeat for, or deregister, service B. Discovery (read) is
  available to any authenticated service.
- Deregistration immediately revokes the token.

### Transport

Milestone 1 assumes services share a private container network (the
Compose network). TLS termination for any externally exposed surface is
the operator's responsibility until mTLS lands (below).

## Target model — certificates and mTLS (Milestone 3, designed)

Token auth is a bridge. The destination:

1. **Atlas CA.** Core (later, Sentinel) operates a private CA. The CA root
   is generated at first boot and stored in the operator's secret store.
2. **Enrollment.** A new service submits a CSR with its bootstrap token.
   Core validates and returns a short-lived (24 h) certificate binding
   `atlas.<name>` + instance ID into the SAN.
3. **mTLS everywhere.** All service↔service and service↔Core connections
   require mutual TLS. Identity comes from the peer certificate; bearer
   tokens are retired.
4. **Rotation.** Services re-enroll automatically at 2/3 of cert lifetime.
   Revocation is a registry state change, checked by Core on every
   registry operation.
5. **Node identity.** Physical/virtual nodes carry their own certificates;
   a service certificate is only issued to an authenticated node.
6. **Signed artifacts.** Plugins, skills, and firmware must carry
   signatures chained to the Atlas CA or an operator-trusted key.

The registration API is already shaped for this evolution: enrollment will
be `POST /v1/registry/services` with a `csr` field, and everything else
keeps its contract.

## Threat notes for operators (Milestone 1)

- Treat `ATLAS_BOOTSTRAP_TOKEN` like a root password. Rotate it by
  restarting Core with a new value; service tokens survive rotation.
- Do not expose Core's port outside the private network unless you put
  TLS + a reverse proxy in front of it.
- Registry contents (service names, addresses, capabilities) are
  discoverable by any registered service by design.
