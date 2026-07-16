#!/usr/bin/env bash
# Copyright (C) 2026 Tyler Buck
# SPDX-License-Identifier: AGPL-3.0-only
# This file is part of Atlas OS <https://github.com/tylerbuck-atlas/atlas-os>.
#
# Back up every Atlas data volume (registry, CA, facts, assets, plans,
# alerts, interactions) plus .env into a timestamped tarball set.
# Stops the stack for a consistent snapshot, then restarts it.
#
#   ./scripts/atlas-backup.sh [dest_dir]
#
# The CA key inside atlas-core-data is the root of all trust. Store the
# output OFF this machine.
set -euo pipefail

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="${1:-backup}/atlas-$STAMP"
VOLUMES=(core eventbus memory assets planner sentinel devices skills ai)

mkdir -p "$DEST"
echo "Backing up to $DEST ..."

[ -f .env ] && cp .env "$DEST/env.backup" && echo "  .env saved"

echo "Stopping stack for a consistent snapshot..."
docker compose stop >/dev/null 2>&1 || true

for v in "${VOLUMES[@]}"; do
  vol="atlas-${v}-data"
  if docker volume inspect "$vol" >/dev/null 2>&1; then
    docker run --rm -v "${vol}:/data:ro" -v "$(pwd)/$DEST:/backup" alpine \
      tar czf "/backup/${vol}.tgz" -C /data . 2>/dev/null
    echo "  ${vol} -> ${vol}.tgz"
  fi
done

echo "Restarting stack..."
docker compose start >/dev/null 2>&1 || docker compose up -d >/dev/null 2>&1

echo
echo "Backup complete: $DEST"
echo "⚠  Contains ca.key (the root of trust). Move it somewhere safe and off this host."
