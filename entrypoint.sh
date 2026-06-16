#!/bin/bash
# NaSnap entrypoint — reads /data/server.json and starts Gunicorn accordingly.
set -e

DATA_DIR="${NASNAP_DATA:-/data}"
CONF="$DATA_DIR/server.json"

PORT=5000
TLS=http

if [ -f "$CONF" ]; then
    PORT=$(python3 -c "import json; c=json.load(open('$CONF')); print(c.get('port', 5000))")
    TLS=$(python3  -c "import json; c=json.load(open('$CONF')); print(c.get('tls',  'http'))")
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
