#!/usr/bin/env bash
# Build the NaSnap Docker image.
# Resolves symlinks (plugins/netapp_storage, pegaprox) into a real temp context
# so Docker can include them — these point outside the build context in dev.
set -euo pipefail

IMAGE="${1:-nasnap:latest}"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

echo "[nasnap] Preparing build context → $TMPDIR"

# Copy project files, dereference symlinks (-L)
rsync -aL --delete \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='data' \
  --exclude='__pycache__' \
  --exclude='*.db' \
  --exclude='*.db-wal' \
  --exclude='*.db-shm' \
  --exclude='*.key' \
  --exclude='.env' \
  . "$TMPDIR/"

echo "[nasnap] Building image: $IMAGE"
docker build -t "$IMAGE" "$TMPDIR"
echo "[nasnap] Done: $IMAGE"
