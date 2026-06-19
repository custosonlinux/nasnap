# Changelog

All notable changes to NaSnap are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [1.3.0] — 2026-06-19

### Added

- **Datastore Index for iSCSI and NVMe-oF** — the index feature (previously NFS-only) now covers SAN datastores. Before each ONTAP snapshot, NaSnap mounts the `netapp_snapmanifest` LV, reads the existing `index.json`, prepends the new snapshot entry, and writes it back atomically — exactly as it does for NFS. The index travels inside every ONTAP snapshot. Requires snapmanifest LV to be initialized.
- **Storage tab — ⟳ Index button for SAN** — the index scan button is now shown for iSCSI and NVMe-oF datastores (when snapmanifest is initialized), in addition to NFS.
- **Startup auto-scan for SAN** — the auto-scan setting now includes SAN datastores with an initialized snapmanifest LV.

### Fixed

- **Manual snapshot on empty datastores** — creating a snapshot of a datastore with no VMs no longer fails with "Required field missing: vmids". An empty `vmids` list is now accepted; the engine takes the ONTAP volume snapshot without per-VM consistency steps, identical to how scheduled snapshots with no VM selection already work. The snapshot dialog shows "No VMs on this datastore — volume snapshot only" instead of blocking.
- **⟳ Index button missing for provisioned SAN datastores** — `snapinfo_initialized` and `mapping_id` are now backfilled from `netapp_volume_mapping` for provisioned iSCSI/NVMe entries in the unified storage response. Previously these fields were missing because `netapp_provisioned_datastores` has no `snapinfo_initialized` column, so the Index button was never shown even when snapmanifest was initialized.
- **Restore & Clone — VM snapshots lost after datastore migration** — when a VM was moved from one datastore to another (e.g. nvme1 → nfs1) and a new snapshot was taken on the new datastore, the Restore & Clone tab previously showed only the new datastore's snapshots. The old snapshots appeared to vanish because the VM list was keyed on VMID alone, and the newer datastore entry silently overwrote `dsName`/`mappingId`. The list now keys on `vmid + pve_storage_id`, so a VM that has lived on multiple datastores shows one row per datastore — each with its own independent snapshot history, protocol badge, and Restore/Clone button. The restore and clone wizards (`cwPopulateSnaps`, `openRestoreWizard`, `openCloneWizard`) have been updated to filter by `pve_storage_id` and carry the correct datastore context through from button click to API call.

---

## [1.2.0] — 2026-06-19

### Added

- **Datastore Index** — every NFS snapshot now bakes a `.nasnap/index.json` file into the ONTAP snapshot before it is created. The index records VM inventory (VMIDs, names, config checksums) and a full snapshot history, making every datastore completely self-describing without an external database.
- **Startup auto-scan** — new toggle in Settings: *Auto-scan datastore indexes on startup*. When enabled, NaSnap scans all NFS datastores in a background thread on every startup and reconciles discovered snapshots into the local database (marked `source = index_import`).
- **Storage tab — ⟳ Index button** — manually trigger an index scan and import for any individual NFS datastore (both auto-discovered and provisioned).
- **Import Tool: index-first** — the VM import wizard now uses the datastore index as its primary snapshot source (faster, works without manifest files). Falls back to the legacy `.netapp-snapmanifest` directory when no index is present. A source badge on each snapshot entry shows the data origin.
- **RC tab — Imported badge and filter** — snapshots imported from the index are marked with an *Imported* badge. A header filter button narrows the VM list to entries with at least one imported snapshot.
- **Snapshot timeline — Week default** — the timeline zoom now defaults to *Week* (previously *Month*).
- **Snapshot timeline — list filtering** — changing the timeline zoom also filters the snapshot list below to show only snapshots within the selected window.
- **Snapshot timeline — All button** — shows the complete snapshot history across all time.
- **Snapshot timeline — Custom date-range picker** — a *Custom* button opens an opaque popover with *From / To* date inputs to display any arbitrary time window. The button label updates to show the active range.
- New API endpoints: `provisioning/datastores/index`, `/scan`, `/scan-all`, `/reindex`, `provisioning/plugin-settings`.
- `auto_scan_on_startup` column in `netapp_plugin_config` (DB migration applied automatically).

### Fixed

- **Snapshots shown multiple times** — the Snapshots API now deduplicates by `(volume_uuid, snap_name)`. Multiple `netapp_volume_mapping` rows per datastore (one per PVE host) previously caused each snapshot to appear 4× in the overview.
- **Restore wizard missing snapshots** — the restore wizard now filters by `pve_storage_id` instead of `mapping_id`, and includes "all VMs" snapshots (those with an empty `vmids_json`, created by scheduled jobs that iterate an empty VM list). Previously only snapshots recorded under the exact `mapping_id` were shown.
- **PVE host not found during index scan** — `_ds_scan_creds()` now falls back through all configured PVE hosts when the primary `pve_cluster_id` is stale or has been deleted.
- **Reconcile duplicate imports** — the deduplication check in `_reconcile_index_into_db()` now joins on `volume_uuid` instead of `mapping_id`, preventing duplicate DB entries when the same ONTAP snapshot is visible under multiple mapping rows.
- **vmid type mismatch in restore filter** — the restore wizard now checks both `int` and `string` forms of a VMID when filtering snapshots.

---

## [1.1.0] — 2026-06

### Added

- **Configurable port and TLS** — set the listening port and HTTP/HTTPS mode in *Settings → Server/Network*. Self-signed certificates are auto-generated in `/data/tls/` when HTTPS is selected. The container uses `network_mode: host` so port changes take effect on restart without editing `docker-compose.yml`.
- **DR tab toggle** — *Settings → UI Features* lets you hide the Disaster Recovery tab for environments without a DR site.
- **DR Failover** *(In Development)* — peer-to-peer DR between two NaSnap instances: PRIMARY/SECONDARY roles, background heartbeat, config sync, DR plans with ordered VM boot groups, precheck, and planned/emergency failover.
- **Tamperproof Snapshots** *(Alpha)* — ONTAP Snapshot Locking (WORM) with configurable lock duration per schedule. Automatic harmonization ensures the lock expires before the retention policy would attempt to delete the snapshot (`max_lock_days = floor((retention - 1) × interval_days)`).
- **SnapMirror destination tamperproof** *(Alpha)* — independent lock duration for SnapMirror destination volumes. After each transfer, NaSnap polls the destination until the replicated snapshot appears, then sets the expiry. Configured separately from source locking.
- **Dashboard** — landing page with 7-day rolling stats, snapshot timeline, SnapMirror health indicators, and alert banners for failed snapshots and unhealthy relationships. Environment widget shows ONTAP endpoints and PVE hosts at a glance.
- **Light / Dark theme** — one-click toggle in the top bar; preference persisted per browser session.
- **PVE cluster auto-discovery** — when adding a Proxmox host, NaSnap can scan the cluster network for additional nodes using a configurable domain suffix.
- **SSH key auto-push** — SSH public key is pushed to all cluster nodes automatically during cluster import.
- **Storage tab instant load** — volume mapping list loads immediately from cache while ONTAP data refreshes in the background.

### Fixed

- Dashboard snapshot counts switched from cumulative totals to 7-day rolling window for a more actionable view.
- DR tab description corrected to English.
- BFCache fix — open dialogs are closed when navigating back to the page.
- Subtab bar always receives click events (overlay-blocking regression fixed).
- Logo, footer, and Settings/Info links no longer produce 404 errors.
- Server/Network and DR settings now persist across restarts via the database.
- Duplicate schedule execution under multi-worker Gunicorn (WORKERS now defaults to 1; scheduler must run as a single instance).

---

## [1.0.0] — 2026-05

### Added

- Initial release of NaSnap — a self-contained NetApp ONTAP snapshot manager for Proxmox VE.
- **VM-consistent snapshots** — crash, app (QEMU guest agent fsfreeze), and suspend consistency levels.
- **Restore** — SFSR (NFS, single VM/disk), volume revert (NFS and SAN), single-VM LV-copy restore (iSCSI/NVMe).
- **Clone** — VM clone from any snapshot with fresh VMID and MAC addresses. DR clone from SnapMirror secondary.
- **Schedules** — cron-style schedules with retention, pre/post hooks, SnapMirror trigger, and email notifications per schedule.
- **SnapMirror** — relationship visibility, transfer trigger per schedule, DR restore and clone from secondary.
- **Storage Provisioning** — end-to-end NFS, iSCSI, and NVMe-oF datastore provisioning including ONTAP volume/LUN/namespace/iGroup/subsystem creation, host-side setup, LVM VG creation, and PVE storage registration.
- **Import VMs from Datastore** *(Alpha)* — adopt existing ONTAP volumes with live VMs without reprovisioning. Reads snapmanifest, reconstructs VM inventory, and reassigns VMIDs on conflict.
- **Built-in auth** — admin and viewer roles, Argon2id password hashing, AES-256-GCM encrypted ONTAP credentials at rest.
- **DB export / import** — full JSON backup of all plugin config, schedules, endpoints, DR plans, and user accounts.
- **Enterprise Blue UI** — clean dark theme with full light theme support.
- **Multi-protocol support** — NFS (stable), iSCSI (Beta), NVMe-oF (Beta), including ASA with NVMe/TCP.
