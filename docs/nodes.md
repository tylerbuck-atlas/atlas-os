# Atlas OS — Nodes & the Field Tier (design)

*Status: design document. The hub tier is implemented (Milestones 1–7);
the field tier and explicit node identity are the post-1.0 roadmap.*

Diagram: [diagrams/node-architecture.html](diagrams/node-architecture.html)

## The three tiers

| Tier | Hardware | Runs | Identity |
|---|---|---|---|
| **Hub / central node** | Proxmox VM, mini-PC (N100-class), any Docker host | All Atlas services (the mTLS mesh) | CA-issued service certificates (implemented) |
| **Full nodes** (future) | Additional Docker-capable machines — a Pi near a radio, a GPU box for inference | A subset of services/adapters | `atlas://node/{name}/{id}` certificates via the same CA/CSR flow (groundwork in docs/security.md) |
| **Field nodes** | ESP32-class microcontrollers, configuration varying by location | Firmware only — sensors, actuators, radio bridges | Per-device keys today; hardware-anchored device certs (ESP32 secure boot + flash encryption) as the end state |

Field nodes cannot run the service contract (no Docker/Python), so they
participate **through a steward adapter** — a real Atlas service that
owns their protocol and syncs them into the Device Manager. The adapter
is the only component holding a LAN grant; everything else stays
egress-denied per docs/privacy.md.

## Data flow

**Truth (up):** ESP32 reading → adapter (IoT VLAN, per-device key) →
Device Manager sync (mTLS) → versioned fact in Memory (class +
`adapter:` provenance) **and** class-redacted event on the bus →
Sentinel watches. Field nodes only ever *report in* — a compromised
node can lie about a temperature but can command nothing.

**Action (down):** intent (human or `atlas.ai`) → plan → Planner
(default-deny policy, approval) → Device Manager (refuses all callers
except Planner/operator) → steward adapter → ESP32 actuator → new state
re-enters the truth flow.

## Field-node configurations (vary by location, same contract)

- **Sensor puck** — ESP32-S3/C3 + temp/humidity, mmWave presence
  (LD2410-class). Presence is **Class 3** and is redacted/withheld by
  the existing machinery automatically.
- **Sense-and-act** — ESP32 + PoE Ethernet (Olimex ESP32-POE /
  WT32-ETH01) driving relays/contacts where one cable should provide
  power and network.
- **Radio bridge** — ESP32-C6 carrying Zigbee/Thread battery devices
  into Atlas.

Every configuration enters through the same three verbs: sync, report,
actuate.

## Security requirements for the field tier

1. **Isolated IoT VLAN**; per-device encryption keys at minimum.
2. **No unsigned firmware** (founding rule): firmware images are stored
   content-addressed in the Asset Manager and released via CA-signed
   Skill manifests — the OTA pipeline reuses Milestone 6's machinery
   unchanged. A tampered image fails its content address.
3. Field devices hold no Atlas credentials beyond their own identity;
   the adapter is the trust boundary.

## Build order (post-1.0)

1. `atlas.adapter.esphome` — adapter speaking ESPHome's native API;
   per-sensor data classes in its device sync. (The pragmatic path:
   ESPHome's per-device YAML is exactly "configuration varies by
   location.")
2. Signed-firmware OTA through Assets + Skills.
3. Explicit node identity (`atlas://node/...`) and, eventually,
   Atlas-native ESP-IDF firmware with hardware-anchored device
   certificates — making every field node a full cryptographic citizen.
