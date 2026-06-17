# NaSnap — NetApp® ONTAP® Snapshot Manager for Proxmox VE

A self-contained web application that brings VM-consistent NetApp® ONTAP® snapshot management to Proxmox VE environments. Runs as a single Docker container with built-in authentication, SQLite database, and a clean Enterprise Blue UI.

**Current stable: 1.1.0** · [Changelog](CHANGELOG.md)

---

## What NaSnap does

NaSnap connects to one or more NetApp ONTAP systems and gives you full snapshot lifecycle management for Proxmox VE — from a standalone web UI that any browser can reach:

- **Snapshot** any VM or set of VMs on a shared ONTAP datastore — crash-consistent, app-consistent (QEMU guest agent), or suspend-based.
- **Restore** individual VMs (SFSR for NFS, LV copy for SAN) or revert an entire datastore to a snapshot in seconds (volume revert).
- **Clone** VMs from any snapshot to a new VMID with fresh MAC addresses.
- **Schedule** automatic snapshots with retention policies, pre/post hooks, and email notifications.
- **Replicate** snapshots to a secondary ONTAP cluster via SnapMirror® and restore or clone directly from the replica — without touching the primary.
- **Provision** new SAN datastores end-to-end (iSCSI and NVMe-oF): ONTAP volume + LUN/namespace + iGroup/subsystem creation, host-side iSCSI/NVMe setup, LVM VG creation, and PVE storage registration — in a single wizard.
- **Import VMs from Datastore** *(Beta)* — adopt an existing ONTAP volume with live VMs without reprovisioning. Reads the snapmanifest from the volume, reconstructs VM inventory, reassigns VMIDs on conflicts, and registers the datastore.
- **Manage users** — built-in admin/viewer roles with Argon2id password hashing and AES-256-GCM encrypted ONTAP credentials.

All operations run as background jobs with live log streaming. Every snapshot embeds a manifest (VM inventory + configs) that travels inside the ONTAP snapshot, making restores self-contained.

---

## Feature Matrix

| Feature | NFS | iSCSI | NVMe-oF |
|---|:---:|:---:|:---:|
| Auto-Discovery | ✅ | ✅ | ✅ |
| VM-consistent Snapshots (crash / app / suspend) | ✅ | ✅ | ✅ |
| Scheduled Snapshots | ✅ | ✅ | ✅ |
| Email notifications per schedule | ✅ | ✅ | ✅ |
| Manifest (VM inventory, disk layout, configs) rides inside ONTAP snapshot | ✅ | ✅ | ✅ |
| Restore — SFSR (Single File / VM Disk, NFS only) | ✅ | ❌ n/a | ❌ n/a |
| Restore — Single VM (LV-copy via temp clone) | ❌ n/a | 🟡 Beta | 🟡 Beta¹ |
| Restore — Volume Revert (all VMs) | ✅ | 🟡 Beta | 🟡 Beta |
| VM Clone from snapshot | ✅ | 🟡 Beta | 🟡 Beta¹ |
| Clone from ONTAP-native snapshots | ✅ | 🟡 Beta | 🟡 Beta |
| Multi-VM snapshot | ✅ | 🟡 Beta | 🟡 Beta |
| ONTAP-native snapshot visibility | ✅ | 🟡 Beta | 🟡 Beta |
| SnapMirror® visibility & DR restore/clone | ✅ | 🟡 Beta | 🟡 Beta |
| Storage Provisioning (auto-setup) | ✅ | 🟡 Beta | 🟡 Beta |
| Storage Resize | ✅ grow & shrink | 🟡 Beta grow only | 🟡 Beta grow only |
| Job cancellation | ✅ | 🟡 Beta | 🟡 Beta |
| Import VMs from Datastore (adopt existing volumes with VMs) | 🟡 Beta | 🟡 Beta | 🟡 Beta |
| Full DR Failover (planned & emergency) | 🟡 Beta | 🟡 Beta | 🟡 Beta |
| DR Test via FlexClone | 🔄 Planned | 🔄 Planned | 🔄 Planned |
| DR Failback | 🔄 Planned | 🔄 Planned | 🔄 Planned |
| Built-in Auth (admin / viewer, Argon2id) | ✅ | ✅ | ✅ |
| AES-256-GCM credential encryption at rest | ✅ | ✅ | ✅ |
| DB Export / Import (full config backup) | ✅ | ✅ | ✅ |

Legend: ✅ Stable · 🟡 Beta · 🟠 Alpha · 🔵 In Development · 🔄 Planned · ❌ N/A

¹ NVMe Single VM Restore and Clone on ASA use a full volume clone via the ONTAP CLI bridge (`private/cli/volume/clone`). Direct namespace clone APIs are not available on ASA, but the volume clone approach achieves identical results.

---

> **Maturity levels:**
> - ✅ **Stable** — Tested in a lab environment and found to be reliable and stable under test conditions.
> - 🟡 **Beta** — Implemented and partially tested. Occasional errors may still occur that require investigation. Use with caution.
> - 🟠 **Alpha** — Implemented, but errors still occur regularly and may require manual intervention. Not suitable for routine use.
> - 🔵 **In Development** — Feature is implemented in code but has not been fully tested yet.
> - 🔄 **Planned** — Not yet implemented.
> - ❌ **N/A** — Not applicable for this protocol.
>
> **Protocol status:**
> - 🟢 **NFS** — Stable. All core workflows (snapshot, restore, clone, SnapMirror DR) are fully implemented and tested.
> - 🟡 **SAN — iSCSI** — Beta. Auto-discovery, snapshots, schedules, single-VM restore, volume revert, VM clone, end-to-end provisioning, and SnapMirror DR restore/clone are fully implemented and tested.
> - 🟡 **SAN — NVMe-oF** — Beta. Auto-discovery, snapshots, schedules, single-VM restore, volume revert, VM clone, end-to-end provisioning, and SnapMirror DR restore/clone are fully implemented and tested on NetApp ASA (NVMe/TCP, ONTAP 9.18.1) and AFF (NVMe/TCP, ONTAP 9.16.1).

---

## Platform & Protocol Compatibility

| Platform | Protocol | Snapshot | Single VM Restore | Volume Revert | Clone |
|---|---|:---:|:---:|:---:|:---:|
| FAS / AFF | NFS | ✅ | ✅ SFSR | — | ✅ FlexClone |
| FAS / AFF | iSCSI | ✅ | ✅ LUN clone | ✅ | ✅ LUN clone |
| FAS / AFF | NVMe-oF | ✅ | ✅ NS clone | ✅ | ✅ NS clone |
| ASA | iSCSI | ✅ | ✅ LUN clone | ✅ | ✅ LUN clone |
| ASA | NVMe-oF | ✅ | ✅ Volume clone² | ✅ | ✅ Volume clone² |

² ASA NVMe uses `POST private/cli/volume/clone` (CLI bridge) instead of the native REST namespace clone. The restore/clone result is identical to iSCSI/FAS/AFF.

---

## Requirements

### Docker

| Component | Version |
|---|---|
| Docker Engine | 20.x or later |
| Docker Compose | v2 |

### ONTAP

All features are included in **ONTAP One** (ONTAP 9.10.1+) at no extra cost:

| Feature | License | Included in ONTAP One |
|---|---|---|
| Volume Snapshots | Base | ✓ |
| Single-File Snapshot Restore (SFSR) | SnapRestore® | ✓ |
| Volume Snapshot Restore (revert) | SnapRestore® | ✓ |
| FlexClone | FlexClone® | ✓ |
| NVMe-oF / iSCSI | SAN | ✓ |

**Tested platforms:** ONTAP 9.13+ (NFS/iSCSI), NetApp ASA (All-SAN Array) with NVMe/TCP on ONTAP 9.18.1, NetApp AFF with NVMe/TCP on ONTAP 9.16.1 — including end-to-end provisioning, snapshot, restore, clone, and SnapMirror DR restore/clone.

### Proxmox VE

Proxmox VE 7.x or 8.x on the managed hosts.

**For NFS** — no additional packages required on PVE nodes.

**For iSCSI:**
```bash
apt install open-iscsi multipath-tools lvm2
```

**For NVMe-oF:**
```bash
apt install nvme-cli lvm2
modprobe nvme-tcp
echo nvme-tcp >> /etc/modules-load.d/nvme-tcp.conf
```

### Network access from the NaSnap container

```
NaSnap  →  ONTAP cluster-mgmt  TCP 443
NaSnap  →  Proxmox VE nodes    TCP 22 (SSH)
NaSnap  →  SMTP server         TCP 25/465/587  (optional, for email notifications)
```

---

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

After logging in, open **Settings → Initial Setup** to walk through ONTAP connectivity, dedicated user creation, SSH key setup, and first discovery.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `NASNAP_ADMIN_PASSWORD` | `admin` | Password for the default `admin` account (applied on first start only) |
| `SECRET_KEY` | *(random)* | HMAC key for session tokens — **always set a stable value in production** |
| `SESSION_HOURS` | `8` | Session lifetime in hours |
| `WORKERS` | `1` | Gunicorn worker processes — keep at 1; the background scheduler thread is single-instance and must not run in multiple workers |
| `NASNAP_DATA` | `/data` | Persistent data directory (DB + AES key) |
| `DEBUG` | — | Set to `1` for Flask debug mode and the `/dev/autologin` shortcut |

> **Port and TLS** are configured via **Settings → Server/Network** in the UI and persisted in `/data/server.json`. The container uses `network_mode: host` so port changes take effect on the next restart without modifying `docker-compose.yml`.

### Minimal `.env` for production

```env
NASNAP_ADMIN_PASSWORD=<strong-password>
SECRET_KEY=<output-of: openssl rand -hex 32>
```

---

## Data Persistence

Everything lives in `NASNAP_DATA` (Docker volume `nasnap_data` → `/data`):

| File | Contents |
|---|---|
| `nasnap.db` | SQLite — users, sessions, snapshots, schedules, endpoints, DR config |
| `.nasnap_aes256.key` | AES-256-GCM key used to encrypt ONTAP passwords at rest |
| `server.json` | Port and TLS mode (set via Settings → Server/Network) |
| `tls/cert.pem`, `tls/key.pem` | Auto-generated self-signed TLS certificate (created on first start when HTTPS is selected) |

> **Important:** back up the entire `/data` directory before upgrading. The AES key and the database must stay together — the key is not recoverable, and without it all stored ONTAP passwords become unreadable.

### DB Export / Import

Open **Settings → Export** to download a complete JSON backup of all plugin configuration (endpoints, schedules, DR plans, volume mappings, SMTP — excluding user accounts for security). Use **Settings → Import** to restore.

---

## Upgrading

```bash
./build-docker.sh          # rebuild image with latest code
docker compose up -d       # rolling restart — data volume is preserved
```

Database migrations run automatically on startup.

---

## User Management

Open **`/admin`** (top bar → Users) to create and delete users and reset passwords.

| Role | Access |
|---|---|
| `admin` | Full access — all features + user management |
| `viewer` | Read-only — can view snapshots, jobs, and schedules |

The default `admin` account is created on first start using `NASNAP_ADMIN_PASSWORD`.

---

## Setup

### 1. ONTAP user

Create a dedicated ONTAP user. The required role depends on which features you use:

**Snapshots and restore only (NFS):**
```bash
security login role create -role nasnap-snap -cmddirname "volume snapshot"             -access all
security login role create -role nasnap-snap -cmddirname "volume snapshot restore"      -access all
security login role create -role nasnap-snap -cmddirname "volume snapshot restore-file" -access all
security login role create -role nasnap-snap -cmddirname "storage/file/clone"           -access all

security login create -user-or-group-name nasnap \
  -application http -authmethod password -role nasnap-snap
```

**Full feature set (SAN provisioning, iSCSI/NVMe, SnapMirror):**
```bash
security login create -user-or-group-name nasnap \
  -application http -authmethod password -role admin
```

> The `admin` role is needed for provisioning operations: creating volumes, LUNs, NVMe subsystems/namespaces, iGroups, and SnapMirror management.

### 2. Add ONTAP endpoint

In the UI under **Settings → NetApp Systems → Add**:

| Field | Description |
|---|---|
| Name | Friendly label (e.g. `prod-cluster`) |
| Host | Cluster management LIF hostname or IP |
| Username / Password | ONTAP credentials |
| SSL Verify | Recommended: enabled |

### 3. Add Proxmox host

Under **Settings → Proxmox Hosts → Add** — add each Proxmox node or cluster that has datastores backed by ONTAP. Standalone nodes (not in a PVE cluster) are supported.

### 4. Run Auto-Discovery

Under **Settings → Discovery → Run** — NaSnap scans your Proxmox hosts for NFS, iSCSI, and NVMe datastores and matches them to ONTAP volumes automatically.

---

## SAN-specific setup (iSCSI / NVMe-oF)

### snapmanifest LV

SAN datastores (LVM-over-iSCSI or LVM-over-NVMe) do not have a filesystem that can hold manifest files. NaSnap uses a small dedicated LV called **snapmanifest** that lives inside the same VG as your VM disks. It is formatted ext4 (64 MB by default) and rides inside every ONTAP snapshot automatically.

After discovery has found your SAN mapping, click **"Setup snapmanifest"** next to the mapping in the Settings tab. This is a one-time operation per VG.

### Restore methods (SAN)

Two restore methods are available. NaSnap selects the correct options automatically based on platform and protocol.

#### Single VM Restore (iSCSI / NVMe)

Restores only the target VM's logical volumes without affecting other VMs on the same datastore:

1. The target VM is stopped.
2. A temporary clone is created from the snapshot on ONTAP (LUN clone for iSCSI; namespace clone for FAS/AFF NVMe; volume clone via CLI bridge for ASA NVMe).
3. The clone is mapped to the Proxmox host.
4. `vgimportclone` imports the clone's LVM VG under a temporary name.
5. Each disk LV of the target VM is copied (`dd bs=512M iflag=direct oflag=direct`) from the temporary VG to the live VG.
6. The temporary clone is unmapped and deleted from ONTAP.
7. The VM config is restored from the NaSnap database.
8. The VM is started.

Other VMs on the same datastore remain running throughout.

#### Volume Revert (all SAN, including ASA NVMe)

Reverts the entire ONTAP volume to the snapshot state — affects **all VMs** on that datastore:

1. The target VM is stopped.
2. The LVM VG is deactivated on the Proxmox host (`vgchange -an`).
3. ONTAP reverts the entire volume to the snapshot state.
4. The VG is re-scanned and reactivated (`pvscan --cache && vgchange -ay`).
5. The VM config is restored from the NaSnap database.
6. The VM is started.

> ⚠️ **Volume Revert is destructive**: all data written to the volume *after* the snapshot is permanently lost. All VMs on the same SAN datastore are affected.

### multipath.conf — NetApp recommended settings

Required on every PVE node for iSCSI. Add to `/etc/multipath.conf`:

```
defaults {
    find_multipaths    yes
    user_friendly_names yes
}
devices {
    device {
        vendor                "NETAPP"
        product               "LUN.*"
        path_grouping_policy  group_by_prio
        prio                  alua
        hardware_handler      "1 alua"
        failback              immediate
        path_checker          tur
        no_path_retry         queue
        features              "3 queue_if_no_path pg_init_retries 50"
        rr_weight             uniform
        rr_min_io_rq          1
    }
}
```

After writing: `systemctl restart multipathd`.

---

## Storage Provisioning (NFS / iSCSI / NVMe-oF)

The **Provisioning** tab automates the complete setup of a new datastore — from ONTAP object creation to PVE storage registration — across all cluster nodes in a single operation.

**NFS:**
1. Create (or reuse) a volume and dedicated export policy; add per-host export rules.
2. Register the PVE storage cluster-wide (`pvesm add nfs`).
3. Create `.netapp-snapmanifest/` inside the mount point.

**iSCSI:**
1. Create (or reuse) a thin-provisioned volume, LUN, and iGroup; add all selected host IQNs; map the LUN.
2. Per PVE host — iSCSI discovery, target login, multipath device detection.
3. First host — `pvcreate`, `vgcreate`; all hosts — `pvscan --cache -aay`.
4. Register LVM/LVM-thin storage cluster-wide.

**NVMe-oF:**
1. Create (or reuse) a namespace and NVMe subsystem; add all selected host NQNs; map the namespace. Supports AFF/FAS and ASA with automatic API fallback.
2. Per PVE host — `nvme connect-all`, namespace rescan, wait for block device.
3. First host — `pvcreate`, `vgcreate`, snapmanifest LV initialization; all hosts — `pvscan --cache -aay`.
4. Register LVM/LVM-thin storage cluster-wide.

The Provisioning tab also handles **resize** (non-disruptive for running VMs) and **teardown** (PVE deregistration, VG removal, ONTAP LUN/namespace/volume deletion).

---

## Email notifications

Each schedule can send email notifications on snapshot job completion. Configure SMTP under **Settings → SMTP**, then enable notifications per schedule.

Notification emails include:
- **Status banner** — full-width, colour-coded: green (success), amber (success with warnings), red (failure).
- **Summary table** — schedule name, snapshot name, datastore, status, and VM list as colour-coded badges.
- **Dark terminal log block** — last 50 job log lines with `[INFO]` / `[WARN]` / `[ERR]` severity tags.
- **Plain-text fallback** — included as a `text/plain` MIME part.

---

## Job management

All snapshot, restore, and clone operations run as background jobs visible under **Jobs & History**.

- **Cancel**: Running jobs can be cancelled. The job stops at the next safe checkpoint and cleans up any partial work (temporary ONTAP clones, imported VGs, reserved VMIDs).
- **Delete**: Completed, failed, or cancelled jobs can be deleted individually or in bulk via "Cleanup".
- **Stale jobs**: If a job is stuck at "running" after a NaSnap restart, Cancel will detect the dead thread and immediately mark the job as cancelled.

---

## Troubleshooting

### Stale iSCSI clone LUN after a failed job

If a clone or restore job fails after the temporary ONTAP LUN was mapped to the Proxmox host but before cleanup completes, the host may be left with a stale multipath device. Because the NetApp multipath configuration uses `no_path_retry queue`, any process that touches the lost device — including LVM (`vgs`, `pvs`) — will **hang indefinitely**.

**Cleanup — run on every affected PVE host:**

1. Identify the stale WWID: `multipath -ll | grep -B1 'failed faulty'`
2. Disable I/O queuing: `multipathd disablequeueing map <WWID>`
3. Flush the device: `multipath -f <WWID>`
4. Remove stale SCSI paths: `echo 1 > /sys/block/<dev>/device/delete` for each path
5. Delete the temporary ONTAP clone volume (`pgxclone_*`) via System Manager or CLI
6. Verify: `multipath -ll` and `vgs` must return cleanly

### SQLite "database is locked"

NaSnap uses SQLite in WAL + autocommit mode. If you see this error in older deployments, verify that `db.py` connects with `isolation_level=None`:

```python
conn = sqlite3.connect(DB_FILE, check_same_thread=False,
                       timeout=30.0, isolation_level=None)
```

This is the default since v1.0.0 and prevents lock contention from background threads (DR heartbeat, schedule ticker).

---

## Performance — SAN disk copy

The `dd` copy used during Single VM Restore and VM Clone is tuned for NVMe storage and high-bandwidth networks:

```
dd if=<src_lv> of=<dst_lv> bs=512M iflag=direct oflag=direct conv=fsync
```

- **`bs=512M`** — large blocks minimise syscall overhead.
- **`iflag=direct oflag=direct`** — O_DIRECT on both sides bypasses the page cache and lets NVMe saturate the full device bandwidth.
- **Timeout: 4 hours** — covers very large volumes even at constrained throughput.

> **DR iSCSI throughput:** During DR restore/clone from a SnapMirror secondary, the `dd` copy runs across clusters. Throughput is bounded by the inter-site link bandwidth, not by local NVMe/iSCSI speed.

---

## Naming conventions

### ONTAP snapshot names

| Type | Pattern | Example |
|---|---|---|
| Manual snapshot | `{prefix}{user_input}` | `NaSnap_before_update` |
| Scheduled snapshot | `{prefix}{YYYYMMDD}_{HHMM}[_{schedule_name}]` | `NaSnap_20260507_1400_nightly` |

`prefix` defaults to `NaSnap_` and is configurable via `snapshot_prefix` in `config.json`.

### Temporary ONTAP objects

All temporary objects created during a clone or restore operation use the prefix **`pgxclone_`** and are deleted automatically when the job completes or fails.

| Object | Pattern |
|---|---|
| NFS FlexClone volume | `pgxclone_{job_id[:8]}` |
| iSCSI temporary LUN | `pgxclone_{job_id[:8]}` |
| NVMe temporary namespace | `pgxclone_{job_id[:8]}` |
| DR FlexClone volume (on secondary) | `pgxdrclone_{job_id[:8]}` |
| DR temporary iGroup (on secondary) | `pgxdr_{job_id[:8]}` |

### SAN: LVM objects on Proxmox

| Object | Pattern |
|---|---|
| snapmanifest LV | `netapp_snapmanifest` (fixed name, configurable via `snapmanifest_lv_name`) |
| Temp mount for snapmanifest write | `/tmp/.pgsi_{random[:10]}` |

### Local temp mount points on PVE nodes

| Purpose | Pattern |
|---|---|
| FlexClone NFS mount | `/mnt/nasnap-clone/{clone_name}` |
| DR restore NFS mount | `/mnt/nasnap-clone/dr-{job_id[:8]}` |
| DR clone NFS mount | `/mnt/nasnap-clone/dr-clone-{job_id[:8]}` |

`flexclone_mount_base` defaults to `/mnt/nasnap-clone`. Configurable in `config.json`.

---

## Consistency levels

| Level | Behaviour |
|---|---|
| `crash` | Snapshot taken immediately — fastest, crash-consistent |
| `app` | QEMU Guest Agent `fsfreeze-freeze` before snapshot, `fsfreeze-thaw` after |
| `suspend` | VM suspended before snapshot, resumed after |

LXC containers: only `crash` is supported (no guest agent).

> ⚠️ **One-datastore-per-VM requirement**: The plugin snapshots an entire ONTAP volume at once. A VM whose disks span multiple ONTAP volumes will only have the disks on the selected datastore included in the snapshot. For reliable snapshots and restores, keep all disks of a VM on the same datastore.

---

## Manifest

### NFS

Every plugin-managed NFS snapshot stores metadata inside the NFS datastore:

```
<nfs_mount_path>/.netapp-snapmanifest/<snap-name>/
  manifest.json    snapshot metadata + VM inventory
  100.conf         Proxmox config of VM 100 at snapshot time
  101.conf         …
```

### SAN (iSCSI / NVMe-oF)

The manifest is written to the **snapmanifest LV** (64 MB ext4 LV in the same VG) before each ONTAP snapshot. The manifest travels inside the snapshot and is always available for restore.

```
/dev/{vg}/netapp_snapmanifest  (ext4, 64 MB)
  manifest.json
  vmconfigs/100.conf
  vmconfigs/101.conf
```

Additionally, the manifest is stored in the NaSnap database as a fallback.

---

## Configuration (`config.json`)

| Key | Default | Description |
|---|---|---|
| `snapshot_prefix` | `"NPP_"` | Prefix added to all snapshot names |
| `default_consistency` | `"crash"` | Default consistency level (`crash`, `app`, `suspend`) |
| `default_restore_method` | `"sfsr"` | Default restore method (`sfsr`, `flexclone`, `san`) |
| `job_poll_interval_s` | `3` | How often to poll ONTAP job status (seconds) |
| `job_poll_timeout_s` | `300` | Max wait time for an ONTAP job (seconds) |
| `manifest_subdir` | `".netapp-snapmanifest"` | Directory inside the NFS mount for manifests |
| `flexclone_mount_base` | `"/mnt/nasnap-clone"` | Temp mount point for FlexClone restores |
| `san_volume_multiplier` | `2.5` | ONTAP volume size = LUN/namespace size × this factor. Leaves headroom for snapshots. |

---

## API Reference

All plugin routes are relative to `/api/plugins/netapp_storage/api/`.

### NaSnap Auth & System

| Method | Path | Description |
|---|---|---|
| POST | `/api/auth/login` | Log in — returns session token |
| POST | `/api/auth/logout` | Invalidate current session |
| GET | `/api/auth/me` | Current user info |
| GET | `/api/auth/users` | List all users (admin only) |
| POST | `/api/auth/users` | Create user (admin only) |
| DELETE | `/api/auth/users/<username>` | Delete user (admin only) |
| POST | `/api/auth/users/<username>/password` | Reset another user's password (admin only) |
| POST | `/api/auth/me/password` | Change own password |
| GET | `/api/system/info` | NaSnap version, DB size, Python version |

### Endpoints & Hosts

| Method | Path | Description |
|---|---|---|
| GET | `endpoints` | List ONTAP endpoints |
| POST | `endpoints/add` | Add endpoint |
| POST | `endpoints/update` | Update endpoint |
| POST | `endpoints/delete` | Delete endpoint |
| POST | `endpoints/test` | Test connectivity |
| GET | `pve-hosts` | List Proxmox hosts |
| POST | `pve-hosts/add` | Add host |
| POST | `pve-hosts/delete` | Delete host |
| POST | `pve-hosts/test` | Test SSH connectivity |
| GET | `volume-mappings` | List volume mappings |
| POST | `volume-mappings/delete` | Delete a volume mapping |
| POST | `discover` | Run auto-discovery |

### Snapshots & Restore

| Method | Path | Description |
|---|---|---|
| GET | `snapshots` | List snapshots (last 200) |
| POST | `snapshots/create` | Create snapshot (async) |
| POST | `snapshots/delete` | Delete snapshot |
| GET | `snapshots/volumes` | List ONTAP volumes for an endpoint |
| GET | `snapshots/vms-for-mapping` | List VMs on a mapped datastore |
| GET | `snapshots/manifest` | Read snapshot manifest |
| POST | `san/snapmanifest-init` | Initialize snapmanifest LV on a SAN mapping |
| GET | `san/snapmanifest-check` | Check snapmanifest LV status |
| POST | `restore/start` | Start restore job (`method`: `sfsr` / `san_single` / `san` / `dr`) |
| GET | `restore/status` | Restore job status |
| POST | `clone/start` | Start clone job |
| POST | `clone/dr-start` | Start DR clone job |
| GET | `clone/nextid` | Suggest next free VMID |
| GET | `clone/nodes` | List available Proxmox nodes |

### Schedules & Jobs

| Method | Path | Description |
|---|---|---|
| GET | `schedules` | List schedules |
| POST | `schedules/add` | Create schedule |
| POST | `schedules/update` | Update schedule |
| POST | `schedules/delete` | Delete schedule |
| POST | `schedules/run-now` | Trigger schedule immediately |
| GET | `jobs/status` | List all jobs or single job (`?job_id=`) |
| POST | `jobs/cancel` | Cancel a running job |
| POST | `jobs/delete` | Delete a completed/failed/cancelled job |
| POST | `jobs/cleanup` | Delete all completed and failed jobs |

### SnapMirror

| Method | Path | Description |
|---|---|---|
| GET | `snapmirror/relationships` | List SnapMirror relationships |
| POST | `snapmirror/scan` | Scan / refresh SnapMirror relationships |
| POST | `snapmirror/update` | Trigger a SnapMirror transfer |
| GET | `snapmirror/secondary-snapshots` | List snapshots on a secondary volume |
| POST | `snapmirror/ensure-export` | Ensure secondary volume is exported (NFS DR) |
| POST | `snapmirror/check-secondary` | Check secondary connectivity |
| GET | `snapmirror/dr-snap-vms` | List VMs available in a replicated snapshot |

### Provisioning

| Method | Path | Description |
|---|---|---|
| GET | `provisioning/datastores` | List provisioned datastores |
| POST | `provisioning/datastores` | Create datastore (starts provisioning job) |
| POST | `provisioning/datastores/import` | Register an existing datastore |
| POST | `provisioning/datastores/remove` | Remove datastore |
| POST | `provisioning/datastores/resize` | Resize datastore |
| POST | `provisioning/datastores/add-host` | Add a PVE host to an existing datastore |
| POST | `provisioning/datastores/remove-host` | Remove a PVE host from a datastore |
| GET | `provisioning/ontap-resources` | Browse volumes/LUNs/iGroups on an endpoint |
| GET | `provisioning/pve-hosts` | List configured PVE hosts (wizard) |
| GET | `provisioning/recovery/scan-volumes` | Scan ONTAP volumes for existing datastores |
| GET | `provisioning/recovery/manifests` | Read snapmanifest from an existing volume |
| POST | `provisioning/recovery/bind` | Adopt an existing volume as a NaSnap datastore |
| POST | `provisioning/recovery/restore-vms` | Import VM configs from a bound datastore |
| GET | `provisioning/recovery/used-vmids` | List VMIDs in use on the target cluster |

### Disaster Recovery

| Method | Path | Description |
|---|---|---|
| GET | `dr/role` | Current DR role (PRIMARY / SECONDARY / STANDALONE) + peer status |
| POST | `dr/role/set` | Set DR role |
| GET | `dr/peer/status` | Peer configuration + live connectivity |
| POST | `dr/peer/configure` | Store peer URL, credentials, and sync token |
| POST | `dr/peer/remove` | Remove peer |
| POST | `dr/peer/sync/push` | Trigger immediate config sync to peer |
| GET | `dr/plans` | List DR plans |
| POST | `dr/plans/create` | Create DR plan |
| GET | `dr/plans/detail` | DR plan details (`?plan_id=`) |
| POST | `dr/plans/update` | Update DR plan |
| POST | `dr/plans/delete` | Delete DR plan |
| POST | `dr/plans/entries/add` | Add datastore entry to plan |
| POST | `dr/plans/entries/update` | Update datastore entry |
| POST | `dr/plans/entries/delete` | Remove datastore entry |
| POST | `dr/plans/auto-detect` | Auto-detect SnapMirror entries for a plan |
| POST | `dr/plans/groups/create` | Create VM boot group |
| POST | `dr/plans/groups/update` | Update VM boot group |
| POST | `dr/plans/groups/delete` | Delete VM boot group |
| POST | `dr/plans/groups/reorder` | Reorder VM boot groups |
| POST | `dr/plans/groups/vms/add` | Assign VM to boot group |
| POST | `dr/plans/groups/vms/delete` | Remove VM from boot group |
| POST | `dr/plans/groups/vms/update` | Update VM assignment |
| GET | `dr/plans/status` | Current plan state + SnapMirror health |
| GET | `dr/plans/precheck` | Pre-failover checks (SnapMirror lag, secondary health) |
| POST | `dr/plans/failover` | Start failover (`failover_type`: `planned` or `emergency`) |
| GET | `dr/plans/failover-jobs` | List failover job history for a plan |
| GET | `dr/plans/snapshots` | List replicated snapshots available for failover |

### Settings

| Method | Path | Description |
|---|---|---|
| GET | `settings/smtp` | Load SMTP configuration |
| POST | `settings/smtp/save` | Save SMTP configuration |
| POST | `settings/smtp/test` | Test SMTP connection |
| POST | `settings/notify-test` | Send a test notification email |
| GET | `/api/plugins/netapp_storage/api/settings/export` | Export all plugin config as JSON |
| POST | `/api/plugins/netapp_storage/api/settings/import` | Restore plugin config from JSON |
| GET | `/api/ui-settings` | Load NaSnap UI settings (DR tab visibility) |
| POST | `/api/ui-settings` | Save NaSnap UI settings |
| GET | `/api/server-settings` | Load server settings (port, TLS mode) |
| POST | `/api/server-settings` | Save server settings — takes effect on next container restart |

---

## Architecture

```
nasnap/
├── app.py                  # Flask app factory — auth middleware, UI injection, all routes
├── auth.py                 # Argon2id hashing, session CRUD, require_auth / require_admin
├── db.py                   # SQLite singleton + AES-256-GCM field encryption
├── login.html              # Standalone login page
├── admin.html              # User management (admin only)
├── settings.html           # Profile, password change, system + DB info
├── nasnap_core/            # Framework shim — standalone replacements for all external deps
│   ├── api/plugins.py      # Route registry (register_plugin_route / get_all_routes)
│   ├── core/db.py          # Delegates to nasnap db.py
│   └── utils/              # auth, ssh_pool, permissions
├── plugins/
│   └── netapp_storage/     # NetApp ONTAP plugin (full source, no external dependency)
│       ├── ui.html         # Plugin UI — served at / with Enterprise Blue theme injected
│       ├── api/            # snapshots, restore, schedules, DR, provisioning, recovery …
│       └── db/schema.sql
├── build-docker.sh         # Docker build via rsync — clean build context, no .venv/.env/DB
├── Dockerfile
└── docker-compose.yml
```

### How theme injection works

`ui.html` is the full plugin UI. When serving it, `app.py` applies `_UI_PATCHES` (string replacements) to hide sections not relevant to the standalone deployment (Deploy Wizard, Plugin Update card) and inject NaSnap-specific copy (labels, default account names, export description). Additionally, `_AUTH_GUARD` (redirect-on-401 fetch interceptor + username display) and `_LOGOUT_BTN` (sign-out button + admin/settings links) are injected at serve time.

### How plugin routes work

The plugin registers routes via `nasnap_core.api.plugins.register_plugin_route()`. After `netapp_storage.register(app)` runs, NaSnap reads all registered paths from `get_all_routes('netapp_storage')` and mounts them as Flask URL rules at `/api/plugins/netapp_storage/api/<path>`. A skip-set (`_NS_ROUTE_SKIP`) allows NaSnap to override specific routes with its own implementations (e.g. export/import that include user accounts).

## Development Setup

```bash
git clone https://github.com/custosonlinux/nasnap.git
cd nasnap

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Dev server — auto-login available at /dev/autologin
DEBUG=1 .venv/bin/python app.py
```

---

## Roadmap

### v1.1 — Released

- **Configurable port + TLS** — set the listening port and HTTP/HTTPS mode in Settings → Server/Network. Self-signed certificates are auto-generated in `/data/tls/`. The container uses `network_mode: host` so port changes take effect on restart without editing `docker-compose.yml`.
- **DR tab toggle** — Settings → UI Features lets you disable (hide) the Disaster Recovery tab for environments without a DR site.
- **DR Completion *(Beta)*** — Peer-to-peer DR between two NaSnap instances with PRIMARY/SECONDARY roles, background heartbeat, config sync, DR plans with ordered VM boot groups, precheck, and planned/emergency failover are fully implemented. Still planned:

- **DR Test via FlexClone** — bring up a DR test environment without breaking SnapMirror: FlexClone each DR volume → mount clones with isolated storage IDs → optionally start VMs with a VMID offset → one-click cleanup.
- **Failback** — guided return to primary: reverse SnapMirror, final resync, re-mount on primary PVE, restore SnapMirror in the original direction.

### v1.2 — Hardening & Operations

- **Login rate limiting** — brute-force protection for the `/api/auth/login` endpoint
- **Health endpoint** — `/api/health` for external monitoring and load balancer probes
- **Audit log export** — CSV/JSON download of the `netapp_audit_log` table
- **Tamper-proof snapshots** — ONTAP Snapshot Locking (compliance / governance) for ransomware protection and regulatory requirements (GDPR, BSI); conflict validation between lock duration and schedule retention

---

## License

GNU Affero General Public License v3.0 (AGPL-3.0)

See [LICENSE](LICENSE) for full terms.

---

## Trademarks

NetApp, ONTAP, SnapMirror, SnapVault, SnapRestore, and FlexClone are registered trademarks of NetApp, Inc. in the United States and/or other countries. All other trademarks are the property of their respective owners.

This project is an independent open-source application and is not affiliated with, endorsed by, or sponsored by NetApp, Inc.
