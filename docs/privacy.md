# Atlas OS — Privacy Contract

This document is a commitment, not a feature list. It binds every
current and future Atlas milestone. Changes to this document are
breaking changes to the project's promise and are treated accordingly.

## The Local-First Rule

> **Atlas must boot, run, and serve its home with zero internet
> connectivity.** Any capability that requires the internet is an
> adapter: explicitly installed, explicitly granted egress, and
> individually removable. No Atlas service may silently degrade privacy
> for convenience.

A home OS knows occupancy patterns, daily routines, documents, and —
eventually — voice, video, and location. That knowledge lives in the
home it describes. Full stop.

## Enforcement, not promises

"Nothing phones home" is enforced at the network layer, not just
documented:

- All Atlas services communicate on an **internal-only Docker network**
  (`internal: true`) with no route to the internet.
- Services that must be reachable from the host/LAN additionally join an
  **edge network with NAT masquerade disabled**: inbound published ports
  work, but connections *initiated by containers* toward the internet or
  LAN have no return path and die. A compromised or misbehaving service
  cannot exfiltrate, even though nothing in Atlas tries to.
- A future adapter that legitimately needs the internet (a weather feed,
  a cloud-model gateway) must join a separate, explicitly-defined egress
  network — one visible place to audit everything that can leave.
- Known residual channel: Docker's embedded DNS forwards lookups via the
  Docker daemon regardless of network topology. Sentinel (Milestone 5)
  will monitor it; until then it is documented rather than hidden.

## Data classification (binding on Milestone 4+)

Every object Atlas Memory or the Asset Manager stores carries a
sensitivity class. Policy attaches to the class, not the storage
location:

| Class | Examples | Default policy |
|---|---|---|
| 0 — Public | device specs, manuals, schemas | unrestricted |
| 1 — Household | device states, automations, inventory | never leaves the Atlas network |
| 2 — Personal | documents, schedules, notes | never leaves the home; per-person access (see below) |
| 3 — Intimate | presence, cameras, microphones, location, health | never leaves the capturing node without an explicit grant; never provided to any model without an explicit grant; default retention limits |

## The AI service (binding on Milestone 7)

- **Local inference is the default.** The reference deployment runs the
  model on the operator's own hardware.
- Cloud models are an **opt-in adapter with per-capability grants** —
  e.g. "may summarize documents" — and are **never eligible to receive
  Class 3 data**, grant or no grant.
- The model remains subject to the founding rule: it is never the source
  of truth, and nothing it proposes acts on the home except through the
  Planner.

## Telemetry

**None. Ever.** No usage statistics, no crash reporting to anyone's
server, no "anonymous" analytics, no phone-home version checks baked
into services. Diagnostics are written to local disk; sharing them is a
human decision every time. Update checking, when it arrives, is an
opt-in adapter fetching signed artifacts — never auto-applied.

## Remote access

Reaching your Atlas from outside your home, in order of recommendation:

1. **Your own VPN (WireGuard/Tailscale)** — end-to-end encrypted into
   your network; no third party in the data path. This is the documented,
   supported path.
2. **A self-hosted relay** — your own rented endpoint forwarding
   encrypted traffic.
3. **Any future hosted relay** must be end-to-end encrypted such that
   the relay operator cannot read home traffic *even in principle*, and
   its client code must be open for verification.

## The household is not one person

Multi-user privacy is a design requirement for Memory and the Planner:
per-person data (Class 2/3) readable only by that person and policies
they set; guests observable without retention; minors with the strictest
defaults. "The user" is never assumed singular.

## What touches the internet today

- Pulling Docker base images at install time.
- `git clone` of this repository.
- Nothing else. Runtime is fully local.
