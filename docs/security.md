# Atlas OS — Security Model

Atlas is Zero Trust: **no anonymous services, no unauthenticated APIs, no
unsigned plugins, no direct execution of LLM output.** As of Milestone 3
the certificate model is implemented and is the default.

## Principles

1. Every API call is authenticated. The only exceptions are liveness
   (`/healthz`, `/v1/system/health`) and the CA certificate itself
   (`/v1/ca/certificate`) — material that is public by definition.
2. Every service has a cryptographic identity, attributable to a service
   *instance*, not just "someone with a key".
3. Secrets are injected via environment/secret stores — never committed,
   never baked into images.
4. LLM outputs are data, not instructions. Only the Planner can turn a
   proposal into an action (future milestone).
5. No unsigned plugins (enforced); no unsigned firmware (arrives with the
   Device Manager).

## Security modes

`ATLAS_SECURITY_MODE` on every service:

- **`mtls` (default)** — certificate identity everywhere, described below.
- **`token`** — the Milestone-2 bearer-token model, kept for development
  and tests. Do not run it in production.

## The mtls model (Milestone 3 — implemented)

### The Atlas CA

Atlas Core operates a private CA (`atlas_core/ca.py`). The root key and
certificate are generated at Core's first boot and persisted in
`ATLAS_CA_DIR` (default `data/ca`). **Back up `ca.key`; treat it like the
keys to the house.** The CA certificate is public and served at
`GET /v1/ca/certificate`.

### Enrollment

Registration *is* enrollment. A new service:

1. generates a private key locally (the key never leaves the service);
2. obtains the CA certificate — preferably pre-provisioned out-of-band
   (`ATLAS_CA_CERT`), otherwise fetched from Core at first contact
   (trust-on-first-use, acceptable on the private container network);
3. `POST /v1/registry/services` over TLS with the **bootstrap token** and
   a `csr` field;
4. receives a certificate valid `ATLAS_CERT_TTL_HOURS` (default 24 h).

Identity is bound by the CA, never copied from the CSR: the certificate's
SAN carries `atlas://service/{name}/{instance_id}` (the instance ID Core
just assigned) plus the DNS names from the service's registered address.
The bootstrap token authorizes *enrollment only* — it grants access to no
other API in mtls mode.

### Mutual TLS everywhere

Every service serves HTTPS with its issued certificate and verifies
peer certificates against the Atlas CA (uvicorn `CERT_OPTIONAL` at the
handshake — so liveness stays reachable — with identity *enforced per
route*). Every authenticated route resolves the caller from the verified
peer certificate's identity SAN. Bearer service tokens no longer exist.

Scoping is unchanged from Milestone 2, now cryptographic: service A's
certificate cannot heartbeat for, deregister, or pull the subscriptions
of service B.

### Rotation & revocation

- The SDK re-enrolls automatically at ~2/3 of certificate lifetime and
  hot-reloads the service's TLS context (no restart).
- Re-enrollment supersedes the previous instance in the registry.
  **Revocation is a registry state change:** Core refuses a certificate
  whose instance is superseded/deregistered immediately, before expiry.
- Non-Core services (e.g. the bus) trust any unexpired Atlas certificate
  without a registry round-trip; their revocation propagation bound is
  the cert TTL. Tighten `ATLAS_CERT_TTL_HOURS` to tighten that bound.

### The operator identity

Humans and tooling authenticate with an operator certificate
(`atlas://service/atlas.operator/...`), mintable only by whoever holds
the CA key:

```bash
python scripts/operator_cert.py --ca-dir data/ca --out ./operator
curl --cert operator/operator.crt --key operator/operator.key \
     --cacert operator/ca.crt https://localhost:8000/v1/registry/services
```

### Signed plugins

In mtls mode Core refuses to load any plugin whose installed distribution
lacks a valid `ATLAS-SIGNATURE` (an ECDSA-SHA256 signature by the CA key
over the distribution's `RECORD`, which hashes every installed file):

```bash
python scripts/sign_plugin.py --ca-dir data/ca --dist my-atlas-plugin
```

### Node identity (groundwork)

Certificates currently identify service instances. Binding certificates
to authenticated *nodes* (machines) — so a service certificate is only
issued to a known node — is designed to reuse this same CA and enrollment
flow, and lands alongside the Device Manager milestones.

## token mode (development only)

The Milestone-2 model: bootstrap token to register; per-service bearer
tokens (SHA-256 hashed at rest) for everything else; the Event Bus
verifies tokens via Core's `POST /v1/auth/introspect`. Useful for local
hacking and fast tests; carries no transport security.

## Threat notes for operators

- `ATLAS_BOOTSTRAP_TOKEN` is the enrollment secret. Rotate by restarting
  Core with a new value; issued certificates are unaffected.
- `data/ca/ca.key` is the root of all trust: operator certs, service
  certs, and plugin signatures. Restrict and back it up.
- Do not expose Atlas ports outside the private network without a
  reverse proxy; the mTLS surface is designed for the service mesh, not
  the public internet.
- Registry contents (service names, addresses, capabilities) are
  discoverable by any enrolled service, by design.
