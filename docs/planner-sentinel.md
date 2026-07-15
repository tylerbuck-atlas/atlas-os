# Atlas Planner & Sentinel — Design (v1)

Milestone 5 is the safety layer: nothing acts on the home except through
a validated, auditable plan; and an immune system watches the event
stream for trouble.

## Planner (`atlas.planner`)

### The pipeline

```
submit ──► validate against policy ──► rejected            (audited, alerted)
                                   ──► awaiting_approval ──► approve ──► execute
                                   ──► approved (auto)   ─────────────► execute
```

A plan is a goal plus an ordered list of **actions** — capability
invocations with parameters. Validation checks every action against the
policy table; execution resolves each capability to a live service via
Core's registry and calls the uniform endpoint
`POST {address}/v1/invoke/{capability}` (service-contract.md §5a). Steps
run in order; the first failure fails the plan and skips the rest — v1
has no partial-success ambiguity.

### Policy: default deny

Policies are ordered rules (`priority`, first match wins):

```json
{"priority": 100, "requester": "atlas.ai", "capability": "devices.lights.*",
 "effect": "require_approval", "note": "AI may propose lighting changes"}
```

Effects: `allow`, `deny`, `require_approval`. **No matching rule means
deny.** A fresh Atlas executes nothing until the operator writes its
first policy. Only the operator (CA-minted certificate) manages policies
and approvals; services see and submit only their own plans, while the
operator sees everything.

### Audit

Plans and steps are never deleted. Every plan records its requester
(verified identity), every step records the resolved service, result or
error, and timestamps. The lifecycle is published on the bus
(`planner.plan.submitted/approved/rejected/completed/failed`,
`planner.step.completed`, `planner.policy.added/removed`) through the
durable outbox — which is also what Sentinel watches.

### Why this matters for M7

When the AI service arrives, its proposals enter here as plan requests
like anyone else's. The model gets no other door: policy decides,
approval gates what the operator wants gated, and the audit trail makes
every model-initiated action reviewable. "No direct execution of LLM
outputs" stops being a slogan and is simply how the pipes are laid.

## Sentinel (`atlas.sentinel`)

Subscribes to `registry.*` and `planner.*` on the bus and evaluates
deliberately legible rules:

| Rule | Trigger | Severity |
|---|---|---|
| `service.down` | transition to unhealthy / unreachable | warning / critical |
| `service.flapping` | ≥ N status transitions in a sliding window | warning |
| `policy.rejection` | a plan was rejected | info |
| `policy.probing` | ≥ N rejections from one requester in a window | critical |

Alerts are deduplicated per (kind, subject) within a cooldown — a crash
loop raises one alarm, not a siren per heartbeat. Alerts persist, are
listable by any service (`GET /v1/alerts`), acknowledgeable by the
operator, and published as `sentinel.alert.raised` for anything that
wants to react.

### Scope honesty (v1)

Sentinel sees what crosses the bus. It does not yet see raw network
traffic (egress is *enforced* by the Compose topology per
docs/privacy.md; Sentinel's DNS/egress observation is future work), and
its rules are static. Anomaly learning, notification channels, and
device-level watching arrive with M6+.
