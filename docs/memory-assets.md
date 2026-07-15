# Atlas Memory & Asset Manager — Design (v1)

Milestone 4 makes truth first-class. Two services:

- **atlas.memory** — durable, queryable, versioned *facts* about the
  household.
- **atlas.assets** — the home's *files* (manuals, documents, photos,
  firmware) as content-addressed, integrity-verified objects.

Both are born class-aware: every object carries a data class
(docs/privacy.md) and policy attaches to the class.

## Memory: facts

A fact is `(namespace, key)` → versioned payloads. Writes **append**;
nothing is silently overwritten; history is queryable. Every version
carries:

| Field | Meaning |
|---|---|
| `payload` | the fact itself (JSON) |
| `class` | 0 public · 1 household · 2 personal · 3 intimate |
| `provenance` | *how this is known* — required; Atlas stores no facts of unknown origin |
| `source` | the verified identity that wrote it (never client-claimed) |
| `owner` | the person a Class 2/3 fact belongs to (schema-ready; person-level enforcement arrives with the Planner) |

API: `PUT/GET/DELETE /v1/facts/{ns}/{key}`, `GET .../history`,
`GET /v1/facts/{ns}?key_prefix=&max_class=`. Tombstones preserve history.

### The Milestone-4 access rule

- **Class 0–2:** readable by any authenticated service — the household
  boundary is the mTLS mesh itself.
- **Class 3 (intimate):** readable only by the *steward* (the service
  that wrote it) and the operator. No grants exist yet, so intimate data
  simply does not travel. Bulk queries *filter* unreadable facts rather
  than erroring, so existence patterns don't leak to scans.

### Events become memory

Memory subscribes to `registry.*` on the bus and materializes events
into `system.services/*` facts — the event stream becomes durable,
versioned, queryable state with provenance (`event:<topic>`). This is
the roadmap's "event replay lands in Memory," and the same pattern will
absorb device and sensor events in Milestone 6.

### Change events, class-redacted

Every fact change is published (via Memory's durable outbox) as
`memory.fact.changed`. Class 0–1 events carry the payload; Class 2–3
events announce only `{namespace, key, version, class, redacted: true}` —
the bus is never a side channel around the read policy.

## Assets: files

Content-addressed storage: a blob lives at `blobs/<sha256>`, identical
files deduplicate, and **every read re-verifies the hash** — a truth
source that cannot prove it is unmodified is refused (HTTP 502) rather
than served. Metadata (kind, tags, class, provenance, owner) lives in
SQLite; deletion tombstones the record and garbage-collects the blob
once unreferenced.

API: `POST /v1/assets` (multipart), `GET /v1/assets[?kind=&tag=&max_class=]`,
`GET /v1/assets/{id}`, `GET /v1/assets/{id}/content`, `DELETE /v1/assets/{id}`.

Kinds: `manual`, `document`, `photo`, `firmware`, `other`. The same
Class-3 steward rule applies to both metadata and content. Ingest events
(`assets.asset.ingested`) carry metadata only — never content — and
redact names for Class 2+.

## Shared infrastructure (SDK)

Milestone 4 extracted two patterns every service now reuses:

- `atlas_sdk.service_auth` — caller identity resolution (peer
  certificate in mtls mode, Core introspection in token mode).
- `atlas_sdk.BusOutbox` — the durable outbox → bus publisher Core
  pioneered in Milestone 2, storage-agnostic.

## Deliberate limits (v1)

- No queries across namespaces; no full-text search (Milestone 6+ needs
  will drive it).
- Person-level policy (`owner`) is recorded but not yet enforced — the
  Planner's grant system (M5) is the enforcement point.
- Asset uploads are read fully into memory (bounded by
  `ATLAS_ASSETS_MAX_UPLOAD_BYTES`, default 512 MB); streaming ingestion
  can replace the internals without an API change.
