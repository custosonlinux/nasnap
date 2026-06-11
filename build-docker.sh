#!/usr/bin/env bash
# Build the NaSnap Docker image.
# Uses rsync to create a clean build context (no .venv, no .env, no DB files).
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
