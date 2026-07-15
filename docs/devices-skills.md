# Atlas Device & Skill Managers — Design (v1)

Milestone 6 plugs the house in — through adapters, never as foundations.

## Device Manager (`atlas.devices`)

### Devices and adapters

A **device** is Atlas's abstraction: id, name, kind (`light`, `sensor`,
`lock`, …), room, data class, accepted commands, state. An **adapter**
is a separate Atlas service that owns a protocol (MQTT, Zigbee, Z-Wave,
vendor APIs) and syncs its devices here:

```
PUT /v1/adapters/{adapter}/devices/{native_id}   ← adapter self-syncs
POST /v1/invoke/adapter.command                  ← Device Manager → adapter
```

The syncing adapter is the device's **steward**: only it may update or
offline the device (identity-checked — adapter A cannot sync devices as
adapter B), and Class-3 device state (cameras, presence, microphones) is
visible only to the steward, the Planner, and the operator; everyone
else sees `{"redacted": true}`.

### Truth flow

Adapter observes → syncs → the Device Manager (1) emits class-redacted
`devices.device.*` events on the bus and (2) writes the state into Atlas
Memory as a fact (`home.devices/{id}`, provenance `adapter:{name}`,
carrying the device's class). "Truth from sensors" is literal: sensor
readings become versioned, auditable, class-protected facts.

### Action flow — the Planner's door

`POST /v1/invoke/devices.command` accepts **only** `atlas.planner` and
the operator. Everything else — including adapters themselves — gets
403. Acting on the house means submitting a plan with capability
`devices.command`; policy decides, approval gates, the audit trail
records, and the Device Manager routes the command to the steward
adapter. There is no side door.

### Real protocols and the privacy contract

Protocol adapters that need LAN access are the privacy contract's
"explicit egress adapters": each joins a dedicated network declared in
one visible place in Compose (see the annotated example), while the rest
of Atlas stays egress-denied. USB coordinators (Zigbee/Z-Wave sticks)
need only a device mapping — no network grant at all. The reference
`atlas.adapter.virtual` simulates a light, a temperature sensor, and a
Class-3 presence sensor with zero hardware and zero LAN.

## Skill Manager (`atlas.skills`)

A **skill** is a signed, versioned capability package:

```json
{
  "name": "skill.greeter", "version": "1.0.0",
  "description": "Says hello",
  "provides": ["greeter.hello"],
  "artifact_asset_id": "…",             ← the package, stored in Atlas Assets
  "artifact_sha256": "…",
  "publisher": "…",
  "signature": "…"                       ← ECDSA by the Atlas CA key
}
```

**No unsigned skills.** Publication verifies (1) the manifest signature
against the Atlas CA — the same root of trust as service certificates
and plugin signing — and (2) that the artifact exists in the Asset
Manager with exactly the content address the signed manifest claims.
Content addressing + signature = a skill cannot be swapped or tampered
after signing. Versions are immutable; enable/disable is operator-only;
discovery is open to any authenticated service.

Sign with `scripts/sign_skill.py --ca-dir data/ca --manifest skill.json`.

### Scope honesty (v1)

The Skill Manager is the registry and trust layer. *Loading* a skill is
the consuming service's job (the M7 AI service loads prompt/behavior
skills; adapters can load driver skills). Runtime sandboxing of skill
code is future work and will land before third-party skills are
encouraged.
