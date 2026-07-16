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

## The IoT SSID and VLAN

Atlas field nodes join a **dedicated Wi-Fi network** (an "IoT SSID") that
is separate from the household's main Wi-Fi and mapped to its own VLAN —
a network segment the household's computers and the internet cannot
reach into, and which cannot reach out.

- An **SSID** is just a Wi-Fi network name. Your existing router/APs
  broadcast a second one (e.g. `<Home>-IoT`) alongside the family
  network — same hardware, separate network. Most consumer routers call
  this a "guest network"; prosumer gear (UniFi, Omada, OpenWrt, pfSense)
  maps each SSID to a VLAN.
- The **VLAN** is the isolated hallway that doorway opens into. A device
  on the IoT SSID lands in the IoT VLAN and, network-wise, lives in a
  different building from the family's laptops.

This enforces a slice of the privacy contract *before Atlas sees a
packet*: one firewall rule (`IoT VLAN → the adapter, nothing else`)
contains the entire fleet. A compromised node lands on an empty,
isolated segment where it can reach only the adapter — and even then,
the worst it can do is lie about a sensor reading, because commands only
ever flow *down* through the Planner.

### All-Wi-Fi fleet notes

The reference fleet is **all Wi-Fi** (no PoE/Ethernet nodes), which has
consequences worth designing for:

- **2.4 GHz only** — ESP32s don't do 5 GHz. Put the IoT SSID on a clean
  20 MHz channel (1, 6, or 11). Fleet size is capped by your AP's
  client limit (often ~30–50), not by bandwidth (nodes sip kilobits).
- **Client isolation ON** — nodes never talk to each other; one
  compromised node can't probe its neighbors.
- **Shared PSK is a collective transport trust** — which is exactly why
  per-device ESPHome encryption keys and unique OTA passwords sit
  *above* the Wi-Fi layer. A cracked PSK reaches an isolated VLAN where
  every node still speaks its own key.
- **All-Wi-Fi ⇒ all-wall-powered.** Wi-Fi's overhead makes battery
  nodes impractical; key sensors (presence) are always-on anyway. Use an
  ESP32-C6 as a Zigbee/Thread bridge for any future battery devices.
- **Discovery across VLANs:** mDNS doesn't cross VLAN boundaries, so the
  adapter either takes a leg on the IoT VLAN (macvlan — fits the
  "adapter holds the LAN grant" pattern), uses an mDNS reflector, or —
  simplest and most diagnosable — DHCP reservations + static adoption.
- **Future:** native-firmware nodes with `atlas://node/...` certificates
  can do **EAP-TLS** Wi-Fi auth — per-device Wi-Fi credentials from your
  own CA, retiring the shared PSK entirely.

## Camera nodes (worked example)

A garage "help me work" camera is the case that exercises the privacy
contract's *grant* clause. A camera is **Class 3** — so by default no
model and no other service can see it. Enabling a workshop assistant is
a deliberate, narrow, audited grant, not a default:

- **On-demand capture, not streaming.** You ask ("look at this wiring");
  the AI proposes a plan with a `camera.capture` action; policy (your
  grant, scoped to that camera + requester) allows it; the adapter tells
  the node to take **one frame**; it goes over the isolated VLAN then
  mTLS to a **local vision model** (never cloud — Class 3); the answer
  returns; the frame is discarded unless you store it (as Class 3, with
  retention) in Assets. Every capture is a Planner audit row.
- **Dual-class node.** The garage node's door contact/relay are Class 1
  (free-flowing) while its camera is Class 3 (steward+operator only) —
  on one board. Atlas handles this natively; class is per-device.
- **Trust through physics:** a hardwired capture LED; Sentinel watching
  capture frequency for anomalies.
- **Hardware:** ESP32-S3 + OV5640 (5MP stills) for workbench detail;
  the $8 ESP32-CAM is fine only for coarse "is the door open" checks.
  Genuine live video assist is the one job to promote off the ESP32 tier
  to a Pi-class node.

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
