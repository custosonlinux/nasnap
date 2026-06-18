#!/bin/bash
# NaSnap entrypoint — reads server settings from nasnap.db (primary) or server.json (fallback).
set -e

DATA_DIR="${NASNAP_DATA:-/data}"

PORT=5000
TLS=http

# Read port + TLS from the database (primary — always in sync with the UI)
if [ -f "$DATA_DIR/nasnap.db" ]; then
    read -r PORT TLS < <(python3 - <<PYEOF
import sqlite3, sys
try:
    conn = sqlite3.connect('$DATA_DIR/nasnap.db')
    conn.execute(
        "CREATE TABLE IF NOT EXISTS np_settings "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
    )
    conn.commit()
    rows = dict(conn.execute(
        "SELECT key, value FROM np_settings WHERE key IN ('server_port','server_tls')"
    ).fetchall())
    print(rows.get('server_port', '5000'), rows.get('server_tls', 'http'))
except Exception as e:
    sys.stderr.write(f'[nasnap] DB read error: {e}\n')
    print('5000 http')
PYEOF
    ) || true
    echo "[nasnap] Config read from DB: port=${PORT}, tls=${TLS}"
else
    echo "[nasnap] No DB found at ${DATA_DIR}/nasnap.db — using defaults"
fi

# Fall back to server.json if DB had no entries yet
CONF="$DATA_DIR/server.json"
if [ "${PORT}" = "5000" ] && [ -f "$CONF" ]; then
    _PORT=$(python3 -c "import json; c=json.load(open('$CONF')); print(c.get('port', 5000))" 2>/dev/null) && PORT=$_PORT || true
    _TLS=$(python3  -c "import json; c=json.load(open('$CONF')); print(c.get('tls',  'http'))" 2>/dev/null) && TLS=$_TLS   || true
    echo "[nasnap] Config read from server.json: port=${PORT}, tls=${TLS}"
fi

mkdir -p /root/.ssh && chmod 700 /root/.ssh

GUNICORN_EXTRA=""

if [ "$TLS" = "self-signed" ]; then
    TLS_DIR="$DATA_DIR/tls"
    CERT="$TLS_DIR/cert.pem"
    KEY="$TLS_DIR/key.pem"
    mkdir -p "$TLS_DIR"
    if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
        echo "[nasnap] Generating self-signed TLS certificate (10 years)..."
        openssl req -x509 -newkey rsa:2048 \
            -keyout "$KEY" -out "$CERT" \
            -days 3650 -nodes \
            -subj "/CN=nasnap" 2>/dev/null
        echo "[nasnap] Certificate written to $CERT"
    fi
    GUNICORN_EXTRA="--certfile $CERT --keyfile $KEY"
    echo "[nasnap] Starting HTTPS on port $PORT (self-signed certificate)"
else
    echo "[nasnap] Starting HTTP on port $PORT"
fi

exec gunicorn -w "${WORKERS:-1}" -b "0.0.0.0:$PORT" \
    --timeout 120 --access-logfile - \
    $GUNICORN_EXTRA \
    'app:create_app()'
