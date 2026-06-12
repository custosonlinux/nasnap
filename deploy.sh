#!/usr/bin/env bash
# Deploy NaSnap to the lab server: rsync sources, then rebuild the container.
set -euo pipefail

REMOTE_HOST="${NASNAP_DEPLOY_HOST:-root@10.230.40.100}"
REMOTE_DIR="/docker/nasnap"

echo "[deploy] Syncing sources to ${REMOTE_HOST}:${REMOTE_DIR} …"
rsync -az --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='data/' \
  --exclude='ssh/' \
  "$(dirname "$0")/" "${REMOTE_HOST}:${REMOTE_DIR}/"

echo "[deploy] Rebuilding container on ${REMOTE_HOST} …"
ssh "${REMOTE_HOST}" "cd ${REMOTE_DIR} && bash rebuild.sh"
