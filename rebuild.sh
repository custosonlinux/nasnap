#!/usr/bin/env bash
# Stop, remove, rebuild and restart the NaSnap container.
set -euo pipefail

cd "$(dirname "$0")"

echo "[nasnap] Stopping container..."
docker compose down

echo "[nasnap] Building image..."
bash build-docker.sh

echo "[nasnap] Starting container..."
docker compose up -d

echo "[nasnap] Done. Logs:"
docker compose logs --tail=30 -f
