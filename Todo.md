# NaSnap — Open Items

---

## Scaling Architecture: Split into Web / Scheduler / Worker

**Priority: High (for enterprise environments), but implementation deferred — design only, no go-ahead for implementation yet**

Target environments: 10–20 PVE hosts, ~1000 VMs, large storage systems. The
current architecture (one Gunicorn process, `WORKERS=1`, SQLite, in-memory job
registry) was built for small/medium installations and hits several
independent limits at this scale — not primarily raw throughput, but blocking
behavior and the lack of horizontal scaling.

### Problem (grounded in the existing code)

1. **`WORKERS=1` is enforced** because the scheduler (`_scheduler_loop` in
   `api/schedules.py`) runs as an in-process daemon thread inside
   `create_app()`. Multiple Gunicorn workers would fire every schedule
   multiple times.
2. **Long SSH/PVE/ONTAP calls block the single worker.** The codebase already
   works around this repeatedly with the "background-refresh cache" pattern
   (`_vm_cache` in `api/snapshots.py`, `_cap_cache` in `api/provisioning.py`)
   — a symptom that the architecture actually needs a real worker pool,
   rather than more workarounds inside the web process.
3. **`_job_registry.py` is an in-memory dict** (thread object + cancel event
   per `job_id`). Only works as long as job start and job execution happen in
   the same process. Once web and worker are separate containers,
   `jobs/cancel` no longer works without further changes.
4. **SQLite as a file** is fine for a single process, but risky once multiple
   containers (web + scheduler + several workers) write to it concurrently —
   especially over a bind mount / NFS volume.
5. **PVE polling is cached in RAM per web process** (`_vm_cache`,
   `_STORAGE_UNIFIED_CACHE_KEY` client-side) — with multiple web replicas
   this would be inconsistent (each replica polls independently, no shared
   state).
6. **Parallel operations** (Bulk Migrate, multi-datastore schedules) currently
   run as a `ThreadPoolExecutor` inside a single process — this doesn't scale
   beyond the CPU/network capacity of a single container.

### Target architecture

```
┌─────────────┐      ┌──────────────┐      ┌──────────────────┐
│  nasnap-web  │─────▶│  Job queue    │◀────│ nasnap-scheduler  │
│ (N replicas) │      │ (Redis+RQ or  │      │   (1 replica,     │
│  Gunicorn,   │      │  DB table)    │      │   singleton)      │
│  no sched.   │      └───────┬──────┘      └──────────────────┘
└──────┬───────┘              │
       │                      ▼
       │             ┌──────────────────┐
       │             │  nasnap-worker    │
       │             │  (M replicas)     │
       │             │  SSH/PVE/ONTAP    │
       │             └─────────┬────────┘
       │                       │
       ▼                       ▼
┌─────────────────────────────────────┐
│         nasnap-db (Postgres)         │
│  netapp_jobs, netapp_snapshots, …     │
└──────────────────────────────────────┘
```

- **`nasnap-web`**: Flask/Gunicorn, multiple workers possible since no
  scheduler and no long-running calls run inline anymore. Creates jobs
  (DB insert + queue publish) and reads status/progress from the DB —
  identical to today's polling pattern (`jobs/status?job_id=`), which stays
  unchanged.
- **`nasnap-scheduler`**: Singleton (exactly 1 replica) — exactly today's
  `WORKERS=1` constraint, just isolated to a small, replaceable component
  instead of the entire web server. Fires schedules, puts jobs on the queue,
  no longer executes them itself.
- **`nasnap-worker`**: N replicas, consume jobs from the queue
  (Snapshot/Restore/Clone/Bulk-Migrate/SFR). Each worker opens its own
  PVE/ONTAP session — the pattern already exists (`pve_for_mapping`,
  `build_pve_client`), it just needs to move from the web process into the
  worker process.
- **Queue**: Redis+RQ (simple, proven) or, less invasively, a DB table as the
  queue (`netapp_job_queue`, workers poll with `SELECT ... FOR UPDATE SKIP
  LOCKED` — only works with Postgres, not SQLite).
- **DB**: Postgres instead of SQLite — not for throughput, but because
  multiple processes/containers now write concurrently.

### Phase plan (each phase individually shippable, implementation only after go-ahead)

**Phase 1 — Detach the scheduler from the web process** (smallest risk,
biggest immediate win: `WORKERS>1` becomes possible for the web tier)
- Extract the scheduler loop out of `create_app()` into its own entry point
  (`scheduler_main.py` or similar), as a separate deployment/container with a
  fixed 1 replica.
- The web process no longer calls `start_scheduler()` itself.
- **Effort: 2–3 days**

**Phase 2 — Job queue + worker container**
- New queue integration (Redis+RQ recommended — smallest rework, good Python
  integration).
- Switch all `start_*_job()` functions (`snapshot_engine.py`,
  `clone_engine.py`, `restore_engine.py`, `migrate_engine.py`, …) from
  `threading.Thread(daemon=True).start()` to `queue.enqueue(...)` — the
  actual `_run_*` functions stay almost unchanged in content, only the start
  mechanism changes.
- Switch `_job_registry.py` (cancel events) to a DB flag
  (`netapp_jobs.cancel_requested`) or Redis, since the web and worker
  processes are separate.
- **Effort: 5–8 days** (including migrating all existing engines)

**Phase 3 — SQLite → Postgres**
- Schema migration (`schema.sql` is already plain standard SQL, should be
  largely compatible — SQLite-specific syntax like `INSERT OR REPLACE` needs
  to be unified to `INSERT ... ON CONFLICT DO UPDATE`, most of the code
  already uses that).
- Switch the DB access layer (`db.py`) to a Postgres driver, replace the
  thread-local connection pattern with a real connection pool (e.g.
  `psycopg` pool).
- The existing backup/restore feature (JSON export) must keep working.
- **Effort: 5–8 days** (including testing backup/restore compatibility)

**Phase 4 — Centralized PVE polling** (optional, as needed)
- Move `_vm_cache`/`_cap_cache` out of web-process RAM into a DB table or
  Redis, so multiple web replicas see the same cache state.
- **Effort: 2–3 days**

### Total effort

**14–22 days** spread across 4 independently shippable phases. Phase 1 can be
implemented and tested in isolation without phases 2–4 having to follow
immediately.

### Status

Design only — **implementation waits for explicit go-ahead.** Do not start
implementation until it is explicitly requested.

---

## VM Database Garbage Collection

**Priority: Medium**

VMs that were deleted or migrated stay permanently visible in the RC view
even though restore is no longer possible. The same applies to datastores
whose mapping was deleted. Cleanup must happen automatically — no admin can
keep track of which entries are stale.

### Problem

The `netapp_snapshots` table holds `vmids_json` per snapshot. Once all
snapshots of a VM have been deleted (via retention), the VM still shows up in
the restore view (because earlier entries remain in the DB). The same applies
to datastores whose `netapp_volume_mapping` was deleted — `ON DELETE CASCADE`
does delete the snapshots, but the RC view aggregates VMs across all known
`vmids_json` entries.

### Cleanup rules

1. **Snapshot without an associated mapping** → mapping was deleted, CASCADE
   deletes the snapshots — not a problem.
2. **VM with no snapshots left** → the VM no longer appears in `vmids_json` of
   any active snapshot → the VM entry must disappear from the aggregation.
   Currently there is no explicit VM entry in the DB (VMs are aggregated
   dynamically from `vmids_json`) → GC must check old `vmids_json` entries in
   snapshots that *still exist*.
3. **Snapshot in the DB but no longer on ONTAP** → the snapshot was deleted
   directly on ONTAP without going through NaSnap → the DB entry is a
   corpse.

### Planned approach

- **Automatic reconciliation** during the snapshot scan (index import):
  The index in every snapshot contains the snapshot history. During the
  startup scan or a manual index scan, it's checked which snapshots still
  actually exist on ONTAP. Entries without an ONTAP counterpart are marked
  `status='orphaned'` or deleted.
- **Background GC thread** (daily, e.g. 03:00):
  Compares `netapp_snapshots` against the ONTAP snapshot list via the REST
  API. Snapshots that no longer exist on ONTAP are removed. After that: all
  VMIDs that no longer appear in any remaining `done` snapshot are
  automatically cleaned up.
- **Mapping check**:
  GC also checks whether the ONTAP volume for each mapping still exists. If
  the volume is missing, all associated snapshots are marked orphaned.

### What can be reused

- ONTAP client `list_snapshots(volume_uuid)` — already exists
- Index scan logic from `_ds_scan_creds()` + `_reconcile_index_into_db()`
- DB query `DELETE FROM netapp_snapshots WHERE ...` (incl. `ON DELETE
  CASCADE` onto jobs/manifests)

### What needs to be newly built

- GC thread with configurable interval (default: daily)
- ONTAP snapshot reconciliation per mapping
- UI hint in Settings: "Last GC run / N orphaned entries removed"
- Optional: manual "Run GC now" button in Settings

**Effort: 2–3 days**
- 1 day: ONTAP reconciliation logic + DB cleanup
- 0.5 day: GC thread + scheduling
- 0.5 day: Settings UI (status display + manual trigger)
- 0.5 day: tests + edge cases (ONTAP offline, partial snapshots)

---

## Active Directory / LDAP Authentication

**Priority: High**

Users can sign in to the web GUI with their AD/LDAP account. Local accounts
remain available as a fallback.

### Scope

- Settings page: LDAP server, port, SSL/TLS, base DN, bind user, bind
  password, user search filter, group→role mapping, "Test Connection" button
- Login flow: AD bind first, fall back to a local account if AD is
  unreachable or the user is known locally
- Group-to-role mapping: one AD group → Admin, one → Viewer (configurable)
- Local emergency accounts always stay active (no lockout on AD outage)
- Session handling stays unchanged (HMAC token)
- Library: `ldap3` (pure Python, no C dependencies)

### Technical assessment

- Extend `nasnap_core/utils/auth.py`: LDAP bind as an alternative auth path
- New table `nasnap_ldap_config` in the DB (one row, encrypted bind password)
- Settings tab "Authentication" with a form + test button
- Login endpoint: tries a local match first, then LDAP bind if configured

**Effort: 3–4 days**
- 1 day: LDAP bind logic + DB schema + settings API
- 1 day: settings UI (form, test button, connection status)
- 1 day: login flow integration, error handling, fallback logic
- 0.5 day: tests + edge cases (AD outage, wrong password, group mapping)

---

## Single File Restore for SAN (iSCSI / NVMe-oF)

**Priority: Medium**

SFR on block storage: copy a file from an ONTAP snapshot into a running VM,
without a full restore.

### Problem

On SAN there is no directly accessible filesystem on the PVE host — only a
block device (LUN). The workflow therefore needs a temporary ONTAP clone
before mounting is possible.

### Planned flow

```
ONTAP Snapshot
  └─ FlexClone (read-only, temp)
       └─ LUN mapping → PVE host
            └─ kpartx / multipath → block device
                 └─ qemu-nbd mount (like NFS SFR)
                      └─ file browser + QGA transfer (identical to NFS SFR)
  └─ Cleanup: unmount → LUN unmap → delete FlexClone
```

### What can be reused

- The complete file browser (left side) — identical to NFS
- The complete QGA transfer code — identical to NFS
- ONTAP FlexClone + LUN mapping from `restore_engine.py` — already exists

### What needs to be newly built

- SFR session type "san" with FlexClone lifecycle management
- Robust cleanup on session timeout or error (avoid FlexClone corpses)
- `file_restore.py`: new mount path for SAN sessions
- UI: minimal change (enable the SFR button for SAN VMs)

### Risks

- Cleanup reliability: a stuck FlexClone blocks storage on ONTAP
- kpartx/multipath mapping can cause issues on some PVE hosts
- Timeout handling is more complex than with NFS (more resources in play)

**Effort: 4–5 days**
- 1 day: SAN mount sequence (FlexClone → LUN map → kpartx → qemu-nbd)
- 1 day: session lifecycle + cleanup daemon for SAN sessions
- 1 day: integration into `file_restore.py` + API changes
- 0.5 day: UI (enable SFR button for SAN VMs)
- 1–1.5 days: tests + error handling + cleanup robustness

---

## DR Failover (low priority, deferred)

Full failover scenario with a SnapMirror secondary as the production system.
Implementation exists but hasn't been sufficiently tested yet.
Deferred until core features are stable.

Includes:
- Planned Failover (clean, with reverse resync)
- Emergency Failover (dirty, SnapMirror broken)
- DR Test via FlexClone (without interrupting production)
- DR Failback

**Effort: 5–8 days** (implementation exists, mainly testing + edge cases)

---

## Completed Features (for reference)

| Feature | Version |
|---|---|
| Datastore Index / Self-Describing Snapshots | v1.2.0 |
| SAN Datastore Index (snapmanifest LV) | v1.3.0 |
| Multi-Datastore Protection Plans | v1.4.0 |
| Single File Restore (NFS, Linux + Windows VMs) | v1.5.0 |
| Snapshot Timeline — Bucket Clustering + Dashboard Colors | v1.5.0 |
