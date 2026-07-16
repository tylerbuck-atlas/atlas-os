# Atlas OS — Deployment Topologies

## Reference deployment: the single-box hub

The most common real-world install: one machine is both the **hub**
(all Atlas services under Docker Compose) and the **inference node**
(Ollama on the host, using the local GPU). Best available privacy
posture — prompts and household context never cross the LAN.

```
┌─ one physical box ──────────────────────────────┐
│  Ollama (host process, GPU)  ←── host-gateway ──┤── atlas-ai container
│  Docker: the 11-container Atlas mesh (mTLS)     │
└─────────────────────────────────────────────────┘
```

Recommended shape on Proxmox: Atlas hub in a VM (snapshots/backups of
the machine that holds the CA), GPU passed through to the same VM — or
to a second inference-only VM so the model can be rebooted/upgraded
without touching the hub.

### Enabling local inference

```bash
# host: install Ollama, make it listen beyond loopback
#   systemd override:  Environment="OLLAMA_HOST=0.0.0.0"
ollama pull qwen3:14b
```

In `docker-compose.yml`, on the `atlas-ai` service: set the backend to
`ollama`, point the model URL at `http://host.docker.internal:11434`,
set the model name, and uncomment the `extra_hosts` line — that
host-gateway mapping is this service's single, visible local-inference
grant. Container→host traffic is bridge-local, so the egress-denied
network topology stays fully intact.

### Choosing the model

Atlas's AI workload is grounded Q&A over a small injected context plus
strict JSON proposal emission — it rewards instruction-following
discipline, not raw size, and punishes slow "reasoning" models (nobody
wants a 20-second think before a lamp turns on).

| GPU VRAM | Model | Notes |
|---|---|---|
| 8 GB | Qwen3 8B (alt: Llama 3.x 8B) | fully adequate for Atlas context sizes |
| 12–16 GB | **Qwen3 14B** ← recommended | consumer sweet spot; excellent structured output |
| 24 GB | Qwen3 30B-A3B (MoE) | 30B-class quality, ~3B active → fast |
| CPU-only / tiny | Gemma 3 4B | constrained-hardware favorite |

Upgrading hardware later = `ollama pull` a bigger model + change one
env var + restart one container. Nothing else in Atlas changes; the
interaction audit (`GET /v1/interactions`) lets you compare models on
your real household questions. The parser makes bad model output *safe*
(it can only degrade to text, never to action); model choice decides
how *rare* bad output is.

### Why Ollama (and why it isn't load-bearing)

Local-only by nature, the de facto standard operators already run,
automatic model management, native JSON mode (Atlas requests
`format: "json"` on every call). But the model backend is a three-method
protocol — llama.cpp server, vLLM, or anything OpenAI-compatible is a
small backend class away. There is deliberately no cloud backend
(docs/privacy.md).

## Hardware guidance

- **Hub:** reliability over power — SSD, UPS, always-on. Fanless N100
  mini-PC, used ThinkCentre/OptiPlex micro, or a Proxmox VM. The whole
  11-container stack idles around 1.5–2 GB RAM; 25 GB disk is ample.
- **Inference:** any RTX-class card; VRAM is the only budget that
  matters (see table). The AI service itself wants none of the GPU.
- **Field tier:** see [nodes.md](nodes.md) — ESP32s from ~$6, PoE
  variants where one cable should do everything.
- Multi-machine LAN access to Atlas ports works through the published
  ports; if a LAN client ever can't reach them, the `atlas-edge`
  no-masquerade network block in Compose is the knob to inspect.

## Operational notes

- **Back up `ca.key`** (in the core data volume under `ca/`) — it is
  the root of all trust; `docker compose down -v` destroys it.
- Mint operator credentials with `scripts/operator_cert.py` (or via
  `docker exec` into atlas-core); treat them like sudo.
- First diagnostics: Sentinel's `/v1/alerts`, Memory's
  `system.services` facts, `docker compose ps`.
