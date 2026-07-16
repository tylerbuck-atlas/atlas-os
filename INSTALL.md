# Installing Atlas OS

Copyright © 2026 Tyler Buck · AGPL-3.0 · <https://github.com/tylerbuck-atlas/atlas-os>

This guide takes you from a blank machine to a running, verified Atlas —
and through your first conversation with it. Time required: about
15 minutes plus the first image build.

---

## 1. What you need

| Requirement | Minimum | Notes |
|---|---|---|
| A machine | 2 vCPU · 4 GB RAM · 25 GB disk | Proxmox VM, mini-PC, old desktop — anything that runs Docker. The full stack idles around 2 GB RAM. |
| OS | Ubuntu Server 22.04/24.04 (or any Linux with Docker) | These instructions assume Ubuntu. |
| Docker | Engine + Compose plugin | Installed in step 2. |
| Network | LAN only | **Atlas needs no internet after installation.** Internet is used once, to pull base images. |
| Optional: GPU | Any RTX-class card | Only for local AI inference (step 8). Atlas runs fully without it. |

Hardware guidance in depth: [docs/deployment.md](docs/deployment.md).

## 2. Install Docker

```bash
sudo apt update && sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker
```

(Avoid the snap-packaged Docker; it breaks volume permissions.)

## 3. Get Atlas and set the enrollment secret

```bash
git clone https://github.com/tylerbuck-atlas/atlas-os.git
cd atlas-os
cp .env.example .env
sed -i "s|change-me-to-a-long-random-secret|$(openssl rand -base64 24)|" .env
```

`ATLAS_BOOTSTRAP_TOKEN` is the **enrollment secret** — the only
credential that can register new services. Atlas refuses to boot with a
weak or placeholder value. Treat it like a root password.

## 4. First boot

```bash
docker compose up --build -d
docker compose logs -f atlas-core
```

The first build takes a few minutes. Watch for the eight boot stages,
ending with:

```
atlas-core | Atlas Ready.
```

On this first boot, Core **creates the Atlas Certificate Authority** —
the root of trust for everything: service identity, operator access,
plugin and firmware signing. Then every service enrolls (you'll see
`enrolled (instance …, cert valid 24h)` lines), and the virtual adapter
syncs three demo devices.

### ⚠️ Immediately: back up the CA key

```bash
docker cp atlas-core:/app/data/ca/ca.key ~/atlas-ca-backup.key && chmod 600 ~/atlas-ca-backup.key
```

Store it somewhere safe *off this machine*. It is the keys to the
house — and `docker compose down -v` (step 11) destroys the original.

## 5. Mint your operator credentials

In mtls mode (the default) every API call needs a certificate identity.
Yours is the **operator certificate** — only the CA-key holder can mint
one:

```bash
docker exec atlas-core python -c "
from atlas_core.ca import CertificateAuthority
import uuid
ca = CertificateAuthority('/app/data/ca'); ca.ensure()
k, c = ca.issue_self(common_name='atlas.operator', instance_id='manual-'+uuid.uuid4().hex[:8], dns_names=[], ttl_hours=12)
open('/tmp/operator.key','wb').write(k); open('/tmp/operator.crt','wb').write(c); open('/tmp/ca.crt','wb').write(ca.cert_pem)"
mkdir -p operator
for f in operator.key operator.crt ca.crt; do docker cp atlas-core:/tmp/$f operator/; done
chmod 600 operator/operator.key
```

Certificates expire (12 h here) by design — rerun this to mint fresh
ones. Treat the operator key like `sudo`.

Handy alias for everything below:

```bash
OP="--cert operator/operator.crt --key operator/operator.key --cacert operator/ca.crt"
```

## 6. Verify the installation

```bash
# Liveness (unauthenticated by design)
curl -k https://localhost:8000/v1/system/health

# The whole OS at a glance — expect every service "healthy"
curl -s $OP https://localhost:8000/v1/system/status | python3 -m json.tool

# The demo devices the virtual adapter synced
curl -s $OP https://localhost:8700/v1/devices | python3 -m json.tool

# Sentinel should be quiet
curl -s $OP https://localhost:8600/v1/alerts
```

Three checks that prove the security model is live:

```bash
curl -s -o /dev/null -w "%{http_code}\n" --cacert operator/ca.crt \
  https://localhost:8000/v1/registry/services        # → 401 (no cert, no entry)
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/healthz --max-time 3
                                                     # → 000 (TLS only)
docker exec atlas-core python -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=3)" \
                                                     # → fails (egress denied)
```

## 7. Your first conversation with the house

Atlas executes **nothing** until you write a policy — default deny is
real. Set your first rule, then ask:

```bash
# Rule: the AI may PROPOSE device actions; you approve each one
curl -s $OP -X POST https://localhost:8500/v1/policies -H 'Content-Type: application/json' \
  -d '{"requester":"atlas.ai","capability":"devices.command","effect":"require_approval","note":"the AI proposes; a human disposes"}'

# Ask a question (grounded in the household's facts)
curl -s $OP -X POST https://localhost:9000/v1/ask -H 'Content-Type: application/json' \
  -d '{"prompt":"what is the hallway temperature?"}' | python3 -m json.tool

# Ask for an action — it becomes a plan awaiting YOUR approval
curl -s $OP -X POST https://localhost:9000/v1/ask -H 'Content-Type: application/json' \
  -d '{"prompt":"turn on the living room lamp"}' | python3 -m json.tool
# note the "plan_id" in the response, then:
curl -s $OP -X POST https://localhost:8500/v1/plans/<plan_id>/approve

# Watch the truth update
curl -s $OP "https://localhost:8700/v1/devices?kind=light" | python3 -m json.tool
```

## 8. Optional: real local AI (Ollama)

Out of the box the AI runs a deterministic **stub** (lookups and device
commands, no model). For real inference on your own GPU:

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl edit ollama    # add:  [Service]
                              #       Environment="OLLAMA_HOST=0.0.0.0"
sudo systemctl restart ollama
ollama pull qwen3:14b         # 12 GB+ VRAM; use qwen3:8b for 8 GB cards
```

Then in `.env`:

```
ATLAS_AI_BACKEND=ollama
ATLAS_AI_MODEL_NAME=qwen3:14b
```

…uncomment the `extra_hosts` line on the `atlas-ai` service in
`docker-compose.yml` (that mapping is the AI's single, visible
local-inference grant), and `docker compose up -d atlas-ai`. Model
selection guidance: [docs/deployment.md](docs/deployment.md). There is
no cloud backend, deliberately ([docs/privacy.md](docs/privacy.md)).

## 9. Updating Atlas

```bash
git pull
docker compose up --build -d
```

Data (registry, CA, facts, assets, plans, alerts) lives in named
volumes and survives updates. Services re-enroll automatically on
restart.

## 10. Backup & restore

What matters, in order: the CA (`ca.key` — step 4), your `.env`, and
the data volumes:

```bash
docker compose down          # stop cleanly (volumes preserved)
for v in core eventbus memory assets planner sentinel devices skills ai; do
  docker run --rm -v atlas-$v-data:/data -v $(pwd)/backup:/backup alpine \
    tar czf /backup/atlas-$v-data.tgz -C /data .
done
docker compose up -d
```

Restore by reversing the tar into fresh volumes before first start.
On Proxmox, VM-level snapshots/backups cover all of this at once.

## 11. Reset / uninstall

```bash
docker compose down        # stop; keep all data
docker compose down -v     # ⚠ FULL RESET: destroys CA, facts, assets, plans — everything
```

## 12. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Core exits at boot with `ATLAS_BOOTSTRAP_TOKEN` error | Token missing, < 16 chars, or still the placeholder — set a real secret in `.env` |
| A service restarts in a loop | `docker compose logs <service>`; most commonly a stale `.env` value |
| `401 client certificate required` | Expected: mint operator credentials (step 5); certs expire — mint fresh ones |
| `410` on heartbeat in logs | Normal during restarts — the service re-enrolls automatically |
| Can't reach ports from another LAN machine | Check the `atlas-edge` network block in `docker-compose.yml` — inbound published ports are designed to work with masquerade disabled; see [docs/privacy.md](docs/privacy.md) |
| AI answers "the local model is unavailable" | Ollama not running / not listening on `0.0.0.0` / `extra_hosts` line still commented |
| Plan stuck `awaiting_approval` | That's the point — approve it (`POST /v1/plans/{id}/approve`) or write an `allow` policy |
| Sentinel `service.down` alerts after host reboot | Services race Core at startup; they self-heal — ack the alerts |

## Developer install (no Docker, no certs)

For hacking on a single service, `token` mode skips the CA entirely:

```bash
cd services/core
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../../libs/atlas-sdk -e ".[dev]"
pytest                                    # every service ships its suite
ATLAS_SECURITY_MODE=token ATLAS_BOOTSTRAP_TOKEN=$(openssl rand -hex 16) \
  python -m atlas_core.main
```

`token` mode is development-only — never run it for a real household.
