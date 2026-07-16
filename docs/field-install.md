# Atlas OS — Field Installation Playbook

Copyright © 2026 Tyler Buck · AGPL-3.0

Installing Atlas into **someone else's home**. This is the human
procedure — what to prepare, what to carry, what to do on-site, in what
order — layered on top of the software steps in
[INSTALL.md](../INSTALL.md).

> **Honest status.** The hub and all eight services are implemented and
> tested. The **field-node tier (ESP32 adapter) is design-complete but
> not yet code** — the node sections below describe the intended
> procedure and are marked *(pending `atlas.adapter.esphome`)*. Until
> that ships, a real install delivers the hub, the AI, and any
> USB/LAN-adapter devices; Wi-Fi ESP32 nodes are the next build. Do not
> promise a homeowner node automation you cannot yet commission.

---

## Phase 0 — Before you go (remote prep)

Do these at your bench, not in their living room.

**Confirm the deal.** Walk the homeowner through
[data-handling.md](data-handling.md) and get it signed *before* anything
is installed. Atlas will hold sensor data, possibly camera frames,
possibly presence — in their home. Consent and data ownership are
settled up front, in writing. This is not optional and not a formality.

**Site questionnaire.** Collect ahead of time:
- Internet router make/model and whether you'll manage the network or
  they will.
- Where the hub will live (needs power, wired Ethernet ideally, cool,
  out of the way — a closet, a shelf, a rack).
- Which rooms want which sensors; whether any cameras are in scope
  (triggers the extra consent in data-handling.md).
- Whether they have a GPU box for local AI, or the hub will run the
  stub, or you're bringing an inference node.

**Pre-stage the hub.** Build it at your bench (Phases 3–4 of this doc),
run `scripts/atlas-preflight.sh` until all green, then
`docker compose down` and transport it ready-to-run. A hub that already
booted once at your shop is a hub that will boot in their closet.

**Pre-flash nodes** *(pending adapter)*. Each ESP32 gets its
location-specific config, its Wi-Fi credentials for *their* IoT SSID,
and its per-device key — flashed and labeled at the bench. Arriving with
labeled, pre-configured nodes turns the on-site node step into "mount
and power," not "debug embedded firmware in a hallway."

## Phase 1 — The customer network (on-site, first)

Atlas assumes a segmented network. Set this up before the hub goes in.

1. **Create the IoT SSID + VLAN.** A dedicated Wi-Fi network (e.g.
   `<Home>-IoT`) mapped to its own VLAN, separate from the family's
   main network. See the explanation in
   [nodes.md](nodes.md#the-iot-ssid-and-vlan).
2. **2.4 GHz, clean channel** (1, 6, or 11 — ESP32s are 2.4 GHz only).
3. **Client isolation ON** for that SSID — nodes never talk to each
   other, only up to the hub's adapter.
4. **One firewall rule:** IoT VLAN → the hub's adapter address, and
   nothing else. No IoT-VLAN → internet, no IoT-VLAN → main-LAN.
5. **Reserve the hub a static IP / DHCP reservation** on the main LAN so
   nodes and the family's devices can always find it.
6. Note the SSID, PSK, VLAN id, and hub IP on the install record.

If the homeowner's router can't do VLANs or per-SSID isolation, that is
a prerequisite to solve (a prosumer AP / router) — flag it in Phase 0,
not on install day.

## Phase 2 — Physical placement

- **Hub:** wired Ethernet to the main LAN, on a UPS if at all possible,
  somewhere cool and undisturbed. This machine's uptime *is* the home's
  automation; treat it like the furnace, not like a laptop.
- **Nodes** *(pending adapter):* mounted where each room needs them,
  near an outlet (all-Wi-Fi means all-wall-powered). Presence sensors
  aimed per their datasheet; cameras aimed *only* where the homeowner
  agreed, with the capture LED visible.

## Phase 3 — Install the hub software

Follow [INSTALL.md](../INSTALL.md) steps 2–4 on the hub machine. In
brief:

```bash
# Docker (INSTALL.md §2), then:
git clone https://github.com/tylerbuck-atlas/atlas-os.git && cd atlas-os
cp .env.example .env
sed -i "s|change-me-to-a-long-random-secret|$(openssl rand -base64 24)|" .env
docker compose up --build -d
docker compose logs -f atlas-core        # wait for: Atlas Ready.
```

## Phase 4 — Secure the trust root (do not skip)

The moment the CA exists on first boot:

```bash
./scripts/atlas-backup.sh                 # full snapshot incl. ca.key
```

Move the backup **off the hub** — an encrypted USB key you keep, plus a
copy you leave with the homeowner in a sealed envelope (it is *their*
trust root; you are a custodian, not the owner). Losing `ca.key` means
re-enrolling every service and node from scratch. Record where both
copies went on the install sheet.

Then mint your working credentials:

```bash
./scripts/atlas-operator-cert.sh
```

## Phase 5 — Commission the core system

```bash
./scripts/atlas-preflight.sh
```

Every service green, boot stage `8/8 READY`, Sentinel quiet. Then prove
the security posture in front of yourself (and, if they're technical,
the homeowner):

- no cert → `401`
- plain HTTP → refused
- a container cannot reach the internet → blocked

(The exact commands are in [INSTALL.md](../INSTALL.md) §6.) These three
checks are your evidence that the privacy promises are real, not
marketing.

## Phase 6 — Local AI (if in scope)

If bringing/using a GPU: install Ollama, pull a model sized to the card
(Qwen3 14B for 12 GB+, 8B for 8 GB — see
[deployment.md](deployment.md)), set `ATLAS_AI_BACKEND=ollama` +
`ATLAS_AI_MODEL_NAME` in `.env`, uncomment the `extra_hosts` line on
`atlas-ai`, and `docker compose up -d atlas-ai`. Otherwise leave the
stub — the home still works; the AI is honest about being a stub.

Confirm: ask a grounded question and get an answer citing real facts.

## Phase 7 — Adopt the nodes *(pending `atlas.adapter.esphome`)*

The intended flow, per [nodes.md](nodes.md):

1. Power each pre-flashed node; it joins the IoT SSID.
2. The adapter discovers it (mDNS on the VLAN, or a DHCP-reservation
   list you loaded) and adopts it with its per-device key.
3. It appears in the Device Manager, stewarded by the adapter, with its
   per-sensor data classes; its state lands in Memory as class-aware
   facts; Sentinel begins watching its heartbeat.
4. Verify each: `curl $OP https://<hub>:8700/v1/devices?room=<room>` —
   right devices, right classes (presence/camera = Class 3), online.

Walk room by room off the site questionnaire; check each node off the
commissioning list before moving on.

## Phase 8 — Author the home's policy

Atlas does **nothing** until you write policy — default-deny is real.
With the homeowner, decide the starting posture. Conservative default:

```bash
OP="--cert operator/operator.crt --key operator/operator.key --cacert operator/ca.crt"
# The AI (and anything else) may PROPOSE device actions; a human approves each.
curl -s $OP -X POST https://<hub>:8500/v1/policies -H 'Content-Type: application/json' \
  -d '{"requester":"*","capability":"devices.command","effect":"require_approval",
       "note":"everything is proposed and approved until we relax it"}'
```

Then relax capability by capability as trust builds — e.g. `allow`
`devices.lights.*`, keep locks and the garage on `require_approval`.
Every past plan is in the audit trail to justify each loosening. Record
the initial policy set on the install sheet.

## Phase 9 — Handover

- Give the homeowner [homeowner-guide.md](homeowner-guide.md) (print
  it) and walk them through one real interaction end to end.
- Hand over their sealed CA backup and explain what it is.
- Show them how they get help / reach you.
- Confirm they know: what data Atlas holds, where it lives (their box,
  not a cloud), how to see it, and how to make it stop
  (data-handling.md).
- Leave the completed install sheet with them; keep a copy.

## Phase 10 — Ongoing

- **Updates:** `git pull && docker compose up --build -d` — data
  survives; services re-enroll. Test at your bench first.
- **Backups:** schedule `scripts/atlas-backup.sh`; on Proxmox, VM
  snapshots cover it.
- **Monitoring:** `scripts/atlas-preflight.sh` on a cron, or (future)
  the notification adapter pushing Sentinel criticals to you.
- **Cert hygiene:** service certs rotate themselves; operator certs are
  short-lived — mint fresh ones when you visit.

---

## The install sheet (leave a copy with the homeowner)

```
Home / customer:        __________________________
Install date:           __________________________
Installer:              __________________________

Hub machine:            ____________  IP: __________
Network: IoT SSID ______________  VLAN ___  isolated? Y/N
Firewall rule (IoT→adapter only) verified?          Y/N

CA backup location #1 (installer-held):  ___________
CA backup location #2 (homeowner-held):  ___________

AI backend:  stub / ollama (model: __________)
Cameras in scope?  Y/N   consent signed?  Y/N
Nodes installed:  ________________________________
Initial policy posture:  _________________________

Security proofs run (401 / TLS-only / no-egress):   Y/N
Homeowner walked through first interaction?         Y/N
data-handling.md signed?                            Y/N
```
