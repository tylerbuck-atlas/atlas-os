# Atlas AI — Design (v1)

The final milestone, and deliberately so. `atlas.ai` is the reasoning
service: one service among many, registered like the rest, holding no
special powers whatsoever. Everything the roadmap built — the registry,
the bus, Zero Trust identity, class-aware Memory, the default-deny
Planner, the watched event stream — is the cage this service was always
going to live in.

## What it does

```
POST /v1/ask {"prompt": "turn on the living room lamp"}
```

1. **Gather governed truth.** Context comes only from Atlas Memory
   (`max_class=2` fact queries) and the Device Manager. `atlas.ai`
   stewards nothing, so Class 3 (intimate) data is *structurally
   unreadable* — the policies of M4/M6 withhold or redact it before this
   service ever sees it. A defensive local filter scrubs anything
   intimate again, so even an upstream bug cannot put presence, camera,
   or location data in front of a model.
2. **Propose.** The model backend returns a *proposal*: an answer, or a
   plan (a list of capability invocations). Model output is **data** —
   parsed defensively; anything malformed degrades to text. Hallucinated
   capabilities die in policy; invented device ids die in resolution.
3. **Plan, never act.** Action proposals are submitted to the Planner as
   `atlas.ai`, goal-prefixed `[atlas.ai]`, where **default-deny policy,
   operator approval, and the immutable audit trail** apply — the same
   gate as everyone else. The Device Manager would refuse this service
   directly anyway (M6 returns 403 to anyone but the Planner); there is
   no second door to find.
4. **Audit.** Every interaction (requester, prompt, proposal, plan id,
   model, context size) is stored. Bus events (`ai.interaction`) carry
   metadata only — never prompt or answer text; questions people ask
   their home are personal.

## Model backends — local by contract

- **`ollama` (the real one).** Local inference against an Ollama server
  on the operator's own hardware (`ATLAS_AI_MODEL_URL`). The reach to
  that URL is the service's single, visible network grant.
- **`stub` (the default).** Deterministic keyword lookups and device
  commands. No model at all: the OS boots, tests, and demos everywhere,
  and is honest when asked something beyond it.
- **Cloud: absent, deliberately.** The privacy contract permits a cloud
  model only as an explicit opt-in adapter that may never receive
  Class 3 data. Shipping one is a conscious future decision — the
  backend protocol is three methods when that day comes.

## Operator policy examples

```json
{"requester": "atlas.ai", "capability": "devices.command",
 "effect": "require_approval", "note": "the AI proposes; a human disposes"}

{"requester": "atlas.ai", "capability": "devices.lights.*",
 "effect": "allow", "priority": 50, "note": "lights may be automated"}
```

Start with `require_approval` on everything; relax capability by
capability as trust accrues — every past plan is in the audit trail to
inform that judgment.

## What the founding rules look like as code

| Rule | Enforcement |
|---|---|
| "The LLM is never the source of truth" | context = Memory + Devices reads only; answers cite what was found; unknown = "I don't know" |
| "No direct execution of LLM outputs" | output parsed as proposal → Planner (default-deny) → uniform invoke; Devices 403s the AI directly |
| "Local-first" | Ollama on the operator's hardware; stub default; no cloud backend exists |
| "Class 3 never reaches a model" | structural (steward policy) + defensive filter + redaction, tested at every layer |
| "Everything auditable" | interaction store + plan audit trail + metadata-only events |
