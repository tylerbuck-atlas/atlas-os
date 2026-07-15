# atlas.echo

The reference Atlas service: the smallest program that fully satisfies the
[Atlas service contract](../../../docs/service-contract.md).

It serves `/healthz`, registers with Atlas Core (retrying until Core is
up), heartbeats on the negotiated interval, re-registers if its token is
invalidated, deregisters on clean shutdown, and publishes one capability:
`echo.reply`.

```bash
curl -X POST http://localhost:8100/v1/echo \
  -H 'Content-Type: application/json' \
  -d '{"message": "hello atlas"}'
```

Use this as the template for every new Atlas service until the Atlas SDK
extracts the registration client.
