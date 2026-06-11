# NaSnap

**Standalone NetApp ONTAP Snapshot Manager for Proxmox VE**

NaSnap is a self-contained web application that brings NetApp ONTAP snapshot and backup management to Proxmox VE environments — without requiring any external framework. It runs as a single Docker container with its own authentication, SQLite database, and Enterprise Blue UI.

---

## Features

- **Snapshot Management** — create, restore, and clone ONTAP snapshots across NFS and SAN (iSCSI / NVMe-oF) datastores
- **Schedules** — cron-based snapshot policies with configurable retention, labels, and email notifications
- **Disaster Recovery** — DR sites, DR plans, VM groups, failover / failback / test workflows
- **SnapMirror** — trigger and monitor SnapMirror updates
- **Provisioning** — auto-configure iSCSI / NVMe-oF / NFS on Proxmox hosts directly from the UI
- **Multi-Host / Multi-Endpoint** — manage multiple Proxmox VE hosts and ONTAP clusters from one instance
- **User Management** — built-in auth with admin and viewer roles; Argon2id password hashing; session tokens
- **Encrypted Credentials** — ONTAP passwords stored with AES-256-GCM at rest
- **DB Export / Import** — backup and restore all configuration (including user accounts) from Settings

## Requirements

| Component | Version |
|---|---|
| Docker Engine | 20.x or later |
| NetApp ONTAP | 9.10 or later |
| Proxmox VE | 7.x / 8.x |

Network access from the NaSnap container to:
- ONTAP management IPs (HTTPS, port 443)
- Proxmox VE host IPs (SSH, port 22)

## Quick Start

```bash
# 1. Clone
git clone https://github.com/custosonlinux/nasnap.git
cd nasnap

# 2. Build image
./build-docker.sh

# 3. Configure
cp .env.example .env
# Edit .env — set NASNAP_ADMIN_PASSWORD and SECRET_KEY

# 4. Start
docker compose up -d

# 5. Open
# http://your-host:5000   →   log in with admin / <NASNAP_ADMIN_PASSWORD>
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `NASNAP_ADMIN_PASSWORD` | `admin` | Password for the default `admin` account (applied on first start only) |
| `SECRET_KEY` | *(random)* | HMAC key for session tokens — **always set a stable value in production** |
| `SESSION_HOURS` | `8` | Session lifetime in hours |
| `WORKERS` | `2` | Gunicorn worker processes |
| `PORT` | `5000` | Listening port |
| `NASNAP_DATA` | `/data` | Persistent data directory (DB + AES key) |
| `DEBUG` | — | Set to `1` for Flask debug mode and the `/dev/autologin` shortcut |

### Minimal `.env` for production

```env
NASNAP_ADMIN_PASSWORD=<strong-password>
SECRET_KEY=<output-of: openssl rand -hex 32>
```

## Data Persistence

Everything lives in `NASNAP_DATA` (Docker volume `nasnap_data` → `/data`):

| File | Contents |
|---|---|
| `nasnap.db` | SQLite — users, sessions, snapshots, schedules, endpoints, DR config |
| `.nasnap_aes256.key` | AES-256-GCM key used to encrypt ONTAP passwords at rest |

> **Important:** back up the entire `/data` directory before upgrading. The AES key and the database must stay together — the key is not recoverable and without it all stored ONTAP passwords become unreadable.

## Upgrading

```bash
./build-docker.sh          # rebuild image with latest code
docker compose up -d       # rolling restart (data volume is preserved)
```

Database migrations run automatically on startup.

## User Management

Open **`/admin`** (top bar → Users) to create, delete users and reset passwords.

| Role | Access |
|---|---|
| `admin` | Full access — all features + user management |
| `viewer` | Read-only — can view snapshots, jobs, and schedules |

## Development Setup

```bash
git clone https://github.com/custosonlinux/nasnap.git
cd nasnap

# Virtual environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run dev server (auto-login available at /dev/autologin)
DEBUG=1 .venv/bin/python app.py
```

## Architecture

```
nasnap/
├── app.py                  # Flask app factory — auth middleware, UI injection, all routes
├── auth.py                 # Argon2id hashing, session CRUD, require_auth / require_admin decorators
├── db.py                   # SQLite singleton with AES-256-GCM field encryption
├── login.html              # Standalone login page
├── admin.html              # User management (admin only)
├── settings.html           # Profile, password change, system + DB info
├── nasnap_core/            # Framework layer — standalone replacements for all external dependencies
│   ├── api/plugins.py      # Route registry (register_plugin_route / get_all_routes)
│   ├── core/db.py          # Delegates to nasnap db.py
│   └── utils/              # auth, ssh_pool, permissions
├── plugins/
│   └── netapp_storage/     # NetApp ONTAP plugin (full source, no external dependency)
│       ├── ui.html         # Plugin UI — served at / with Enterprise Blue theme injected
│       ├── api/            # snapshots, restore, schedules, DR, provisioning, recovery, …
│       └── db/schema.sql
├── build-docker.sh         # Docker build script — creates clean build context via rsync
├── Dockerfile
└── docker-compose.yml
```

### How theme injection works

`ui.html` ships with an orange dark theme. `app.py` patches two layers before serving it:

1. **Static CSS** — direct string replacement of `:root` variable values via `_THEME_SUBS`
2. **JS runtime** — the plugin's theme switcher default is redirected from `proxmoxDark` to `enterpriseBlue` (which is already defined in the plugin's theme table)

### How plugin routes work

The plugin registers routes via `nasnap_core.api.plugins.register_plugin_route()`. After `netapp_storage.register(app)` runs, NaSnap reads all registered paths from `get_all_routes('netapp_storage')` and mounts them as Flask URL rules at `/api/plugins/netapp_storage/api/<path>`. A small skip-set (`_NS_ROUTE_SKIP`) allows NaSnap to override specific routes with its own implementations (e.g. export/import that include user accounts).

## License

GNU Affero General Public License v3.0 (AGPL-3.0)

Copyright © 2024–2026 Custon Online GmbH

See [LICENSE](LICENSE) for full terms.
