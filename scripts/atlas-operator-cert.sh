#!/usr/bin/env bash
# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.
#
# Mint operator credentials from the running Atlas CA into ./operator/.
# In mtls mode every authenticated call needs these. Only the CA-key
# holder (i.e. this host) can mint them. Certs are short-lived by design;
# re-run to refresh.
#
#   ./scripts/atlas-operator-cert.sh [ttl_hours]
set -euo pipefail

TTL="${1:-12}"
CONTAINER="${ATLAS_CORE_CONTAINER:-atlas-core}"
OUT="operator"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "error: container '$CONTAINER' is not running. Start Atlas first (docker compose up -d)." >&2
  exit 1
fi

docker exec "$CONTAINER" python -c "
from atlas_core.ca import CertificateAuthority
import uuid
ca = CertificateAuthority('/app/data/ca'); ca.ensure()
k, c = ca.issue_self(common_name='atlas.operator',
                     instance_id='manual-'+uuid.uuid4().hex[:8],
                     dns_names=[], ttl_hours=${TTL})
open('/tmp/operator.key','wb').write(k)
open('/tmp/operator.crt','wb').write(c)
open('/tmp/ca.crt','wb').write(ca.cert_pem)
"

mkdir -p "$OUT"
for f in operator.key operator.crt ca.crt; do
  docker cp "$CONTAINER:/tmp/$f" "$OUT/$f"
done
chmod 700 "$OUT"; chmod 600 "$OUT/operator.key"

echo "operator credentials written to ./$OUT/ (valid ${TTL}h)"
echo
echo "use them like:"
echo "  OP=\"--cert $OUT/operator.crt --key $OUT/operator.key --cacert $OUT/ca.crt\""
echo "  curl -s \$OP https://localhost:8000/v1/system/status | python3 -m json.tool"
