#!/usr/bin/env bash
# NaSnap — fresh-server bootstrap.
#
# Takes a bare Debian/Ubuntu VM or LXC container and gets it to a running
# NaSnap instance: installs Docker CE from Docker's own apt repo (not the
# distro's outdated docker.io package), clones/updates the repo, writes a
# .env with a real SECRET_KEY and admin password, then builds and starts
# the container via the existing build-docker.sh / docker-compose.yml.
#
# Usage:
#   git clone https://github.com/custosonlinux/nasnap.git && cd nasnap && ./install.sh
#   -- or, on a bare machine with nothing cloned yet --
#   curl -fsSL https://raw.githubusercontent.com/custosonlinux/nasnap/main/install.sh | bash
#
# Safe to re-run: every step checks whether it's already done before acting.
#
# Env overrides:
#   NASNAP_DEPLOY_DIR   Where the repo/data live (default: /docker/nasnap)
#   NASNAP_REPO_URL     Git remote to clone (default: the public NaSnap repo)
set -euo pipefail

NASNAP_DEPLOY_DIR="${NASNAP_DEPLOY_DIR:-/docker/nasnap}"
NASNAP_REPO_URL="${NASNAP_REPO_URL:-https://github.com/custosonlinux/nasnap.git}"

log()  { echo "[nasnap-install] $*"; }
die()  { echo "[nasnap-install] ERROR: $*" >&2; exit 1; }

# ── Preflight ────────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
    die "must run as root (this installs system packages). Try: sudo ./install.sh"
fi

if [ ! -r /etc/os-release ]; then
    die "cannot read /etc/os-release — this script targets Debian/Ubuntu only."
fi
. /etc/os-release
case "$ID" in
    debian|ubuntu) : ;;
    *) die "unsupported distro '$ID' — this script targets Debian and Ubuntu only." ;;
esac
log "Detected $PRETTY_NAME"

VIRT="$(systemd-detect-virt 2>/dev/null || echo unknown)"
if [ "$VIRT" = "lxc" ]; then
    log "Running inside an LXC container."
    log "If Docker fails to start below, the LXC almost certainly needs nesting"
    log "enabled on the Proxmox HOST (not in here). On the Proxmox host, run:"
    log "  pct set <VMID> --features nesting=1,keyctl=1"
    log "  pct reboot <VMID>"
    log "...then re-run this script."
fi

# ── Base packages ────────────────────────────────────────────────────────────

log "Installing base packages (curl, gnupg, git, ca-certificates)…"
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl gnupg git

# ── Docker CE from Docker's own apt repo (not distro docker.io) ────────────────

if dpkg -l docker.io 2>/dev/null | grep -q '^ii'; then
    die "the distro's 'docker.io' package is installed — remove it first (apt remove docker.io) so Docker CE from Docker's own repo can be installed cleanly instead. Re-run this script afterward."
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "Docker + Compose plugin already installed ($(docker --version)) — skipping install."
else
    log "Adding Docker's official apt repository…"
    install -m 0755 -d /etc/apt/keyrings
    if [ ! -f /etc/apt/keyrings/docker.asc ]; then
        curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
    fi
    ARCH="$(dpkg --print-architecture)"
    echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list

    log "Installing Docker CE, CLI, containerd, buildx, and the compose plugin…"
    apt-get update -qq
    apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    systemctl enable --now docker
fi

log "Verifying Docker actually works (docker run hello-world)…"
if ! docker run --rm hello-world >/dev/null 2>&1; then
    if [ "$VIRT" = "lxc" ]; then
        die "'docker run hello-world' failed inside this LXC container. This is almost always missing nesting — on the Proxmox HOST run: pct set <VMID> --features nesting=1,keyctl=1 && pct reboot <VMID>, then re-run this script."
    fi
    die "'docker run hello-world' failed — Docker isn't working correctly. Check 'systemctl status docker' and 'journalctl -u docker'."
fi
log "Docker OK."

# ── Clone / update the repo ─────────────────────────────────────────────────

if [ -f "./Dockerfile" ] && [ -f "./docker-compose.yml" ] && [ -d "./.git" ]; then
    NASNAP_DEPLOY_DIR="$(pwd)"
    log "Already running from inside a NaSnap checkout ($NASNAP_DEPLOY_DIR) — using it."
elif [ -d "$NASNAP_DEPLOY_DIR/.git" ]; then
    log "Found existing checkout at $NASNAP_DEPLOY_DIR — pulling latest…"
    git -C "$NASNAP_DEPLOY_DIR" pull --ff-only
elif [ -e "$NASNAP_DEPLOY_DIR" ] && [ -n "$(ls -A "$NASNAP_DEPLOY_DIR" 2>/dev/null)" ]; then
    die "$NASNAP_DEPLOY_DIR already exists, is non-empty, and isn't a git checkout (e.g. deployed by rsync) — this script is for a fresh install only. Set NASNAP_DEPLOY_DIR to a different path, or use your existing deploy.sh/rebuild.sh workflow here instead."
else
    log "Cloning NaSnap into $NASNAP_DEPLOY_DIR…"
    mkdir -p "$(dirname "$NASNAP_DEPLOY_DIR")"
    git clone "$NASNAP_REPO_URL" "$NASNAP_DEPLOY_DIR"
fi

cd "$NASNAP_DEPLOY_DIR"
mkdir -p data ssh
chmod 700 ssh

# ── .env ─────────────────────────────────────────────────────────────────────

if [ -f .env ]; then
    log "Found existing .env — leaving it untouched."
else
    log "Writing .env…"
    SECRET_KEY="$(openssl rand -hex 32)"

    ADMIN_PASSWORD=""
    if [ -t 0 ]; then
        read -rs -p "[nasnap-install] Set the initial admin password (leave empty to auto-generate): " ADMIN_PASSWORD < /dev/tty || true
        echo
    fi
    GENERATED_PASSWORD=0
    if [ -z "$ADMIN_PASSWORD" ]; then
        ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -d '=+/')"
        GENERATED_PASSWORD=1
    fi

    cat > .env <<EOF
NASNAP_ADMIN_PASSWORD=${ADMIN_PASSWORD}
SECRET_KEY=${SECRET_KEY}
EOF
    chmod 600 .env

    if [ "$GENERATED_PASSWORD" -eq 1 ]; then
        log "Generated admin password: ${ADMIN_PASSWORD}"
        log "(shown once — also saved in ${NASNAP_DEPLOY_DIR}/.env)"
    fi
fi

# ── Firewall heads-up (network_mode: host — no docker port mapping to check) ──
# 5000 is NaSnap's default on first start (no config yet in the DB); it's only
# ever changed later via Settings → Server inside the running app, not by env.
PORT=5000
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    log "ufw is active — allowing TCP ${PORT}…"
    ufw allow "${PORT}/tcp" >/dev/null || true
fi

# ── Build & start ────────────────────────────────────────────────────────────

log "Building the NaSnap image…"
./build-docker.sh

log "Starting the container…"
docker compose up -d

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
log ""
log "Done. NaSnap should be reachable at:"
log "  http://${IP:-<this-host>}:${PORT}"
log ""
log "Log in as 'admin' with the password shown above (or from .env if you set your own)."
log "Next: Settings → Initial Setup to connect ONTAP and Proxmox."
