#!/usr/bin/env bash
# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.
#
# Commissioning / health board. Prints a green-or-red line per service
# using unauthenticated liveness (/healthz), then — if operator creds are
# present in ./operator/ — the authenticated system roll-up and any open
# Sentinel alerts. Safe to run any time; changes nothing.
#
#   ./scripts/atlas-preflight.sh
set -uo pipefail

HOST="${ATLAS_HOST:-localhost}"
GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; BOLD=$'\033[1m'; NC=$'\033[0m'

declare -A SVC=(
  [core]=8000 [eventbus]=8200 [memory]=8300 [assets]=8400
  [planner]=8500 [sentinel]=8600 [devices]=8700 [skills]=8800
  [ai]=9000
)
ORDER=(core eventbus memory assets planner sentinel devices skills ai)

echo "${BOLD}Atlas OS — preflight  (${HOST})${NC}"
echo "${DIM}liveness is unauthenticated by design${NC}"
echo

fail=0
for name in "${ORDER[@]}"; do
  port="${SVC[$name]}"
  code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 4 "https://${HOST}:${port}/healthz" 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then
    printf "  %s●%s  %-10s %sup%s   (:%s)\n" "$GREEN" "$NC" "$name" "$GREEN" "$NC" "$port"
  else
    printf "  %s●%s  %-10s %sDOWN%s (:%s, http %s)\n" "$RED" "$NC" "$name" "$RED" "$NC" "$port" "$code"
    fail=$((fail+1))
  fi
done

echo
if [ -f operator/operator.crt ]; then
  OP="--cert operator/operator.crt --key operator/operator.key --cacert operator/ca.crt"
  echo "${BOLD}System roll-up${NC} ${DIM}(operator cert)${NC}"
  curl -s $OP "https://${HOST}:8000/v1/system/status" 2>/dev/null | python3 - <<'PY' 2>/dev/null || echo "  (could not read system status — operator cert expired? re-run atlas-operator-cert.sh)"
import json,sys
d=json.load(sys.stdin)
print(f"  boot_stage: {d.get('boot_stage')}   ready: {d.get('ready')}")
svcs=d.get("services",{})
print(f"  registered services: {len(svcs)}")
for n,s in sorted(svcs.items()):
    mark = "ok" if s=="healthy" else s.upper()
    print(f"    - {n}: {mark}")
PY
  echo
  echo "${BOLD}Open Sentinel alerts${NC}"
  curl -s $OP "https://${HOST}:8600/v1/alerts" 2>/dev/null | python3 - <<'PY' 2>/dev/null || echo "  (could not read alerts)"
import json,sys
a=json.load(sys.stdin)
if not a: print("  none — quiet.")
for x in a[:20]:
    print(f"  [{x['severity']:8}] {x['kind']:18} {x['subject']}: {x['detail'][:70]}")
PY
else
  echo "${DIM}No operator creds in ./operator/ — run scripts/atlas-operator-cert.sh"
  echo "for the authenticated roll-up and alert board.${NC}"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "${GREEN}${BOLD}All services up.${NC}"
else
  echo "${RED}${BOLD}${fail} service(s) down — check: docker compose logs <service>${NC}"
  exit 1
fi
