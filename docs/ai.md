# Atlas AI — Design (v1)

The AI arrives last, on purpose. By the time it registers, the operating
system around it already enforces everything the model cannot be trusted
to enforce itself. `atlas.ai` is a service like any other — it enrolls,
gets a certificate, heartbeats — and it is bound by three rules that are
*code paths*, not promises.

## 1. Truth first — the model sees only governed data

`atlas.ai` gathers context from Atlas Memory and the Device Manager
**using its own identity**. That is the whole privacy model: the OS
decides what the model may see, not the prompt.

- Memory queries are capped at **Class 2** (`max_class=2`) and Memory
  filters server-side anyway.
- Device state arrives already redacted for `atlas.ai` (a non-steward),
  and the gatherer drops any Class-3 device entirely as belt-and-braces.
- **Class 3 — intimate data (cameras, presence, microphones, location) —
  never reaches any model. Ever.** No prompt, no grant, no exception in
  v1.

Everything gathered carries its provenance, so answers can cite where
knowledge came from. If it is not in the gathered truth, the model has
no business asserting it.

## 2. Proposals, not actions — the Planner is the only door

The backend may return an answer and a list of *proposed actions*. Those
are submitted to the **Planner** as a plan in `atlas.ai`'s name, where
default-deny policy, operator approval, and the audit trail all apply.

`atlas.ai` has no client that can invoke a capability directly — the
code path does not exist. Even holding a valid certificate, it reaches
the world only by asking the Planner, which asks your policies, which
(by default) say no. "No direct execution of LLM outputs" is enforced by
the plumbing, verified by a test that inspects every call the service
makes and asserts none of them act.

## 3. Everything on the record

Every assist is stored (requester, prompt, answer, resulting plan) and
announced on the bus as `ai.assist.completed` — **without prompt
content**. Prompts are personal; the audit log is readable only by the
requester and the operator.

## Backends — local-first (docs/privacy.md)

| Backend | Runs | Sees | Notes |
|---|---|---|---|
| `builtin` (default) | in-process, no model | gathered truth | Deterministic reasoner: household status + device-command intents. Atlas is fully functional with **zero** model and zero egress. |
| `ollama` | your hardware | gathered truth | Local LLM via an Ollama server. Reaching it is an explicit local-inference grant in Compose (`extra_hosts`). |
| *cloud* | — | — | **Not implemented.** The privacy contract permits cloud models only as a separate opt-in adapter, never eligible for Class-3 data. No such adapter ships in v1. |

Whatever a backend returns is **data**: parsed strictly into
`{answer, proposed_actions[]}`. Malformed output, injection attempts,
oversized action lists — all degrade to answer-only. Text can never
become action except by passing, structured, through the Planner.

## What the AI is, in one line

A reasoning engine over trusted household data that can *suggest* but
never *do* — exactly the role the first line of this project reserved
for it: the LLM is one service inside the operating system, and never
the source of truth.
