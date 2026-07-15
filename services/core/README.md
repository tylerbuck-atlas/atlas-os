# Atlas Core

The bootstrap point of Atlas OS: service registry, discovery, health
monitoring, configuration, authentication, plugin loading, and the boot
sequence that ends in `Atlas Ready.`

See [docs/architecture.md](../../docs/architecture.md) for the design and
[docs/service-contract.md](../../docs/service-contract.md) for the API
contract Core enforces.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ATLAS_BOOTSTRAP_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(24))") \
  python -m atlas_core.main
```

## Test

```bash
pytest
```

## API

Interactive docs at `/docs` while running.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/healthz` | none | liveness |
| GET | `/v1/system/health` | none | liveness alias |
| GET | `/v1/system/status` | any caller | boot stage + overview |
| GET | `/v1/system/events` | any caller | recent system events |
| POST | `/v1/registry/services` | bootstrap token | register |
| GET | `/v1/registry/services` | any caller | discover |
| GET | `/v1/registry/services/{id}` | any caller | inspect |
| POST | `/v1/registry/services/{id}/heartbeat` | instance token | heartbeat |
| DELETE | `/v1/registry/services/{id}` | instance token | deregister |
