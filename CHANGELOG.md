# Changelog

All notable changes to NaSnap are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Added

- **Disaster Recovery — single-instance redesign** — removed the two-instance peer/role/heartbeat/sync model (`netapp_dr_peer` table, `dr/peer/*` and `dr/role/*` routes). One NaSnap instance now manages both the primary and DR side directly with its own registered ONTAP/PVE credentials — restoring/redeploying the NaSnap container itself always takes priority over any peer coordination, which never actually helped in that scenario. DR Plan failover (`_execute_failover`) is consolidated onto the same `recovery_engine` primitives (`_bind_nfs`, `restore_vm_configs`, new `start_vms`) used elsewhere, replacing duplicated inline SSH/pvesm logic — this also fixes a latent bug where failover never called `_ensure_nfs_export_rules`, which could previously cause "access denied" if DR PVE hosts weren't already in the NFS export policy.
- **PVE Cluster Grouping** — new `netapp_pve_clusters` table and `netapp_pve_hosts.cluster_group_id` column. Cluster membership is auto-detected via PVE's own `/cluster/status` + `/nodes` APIs (`pve-hosts/discover`, new `pve-hosts/detect-cluster` route) instead of requiring manual grouping. Settings → PVE Hosts now displays collapsible cluster groups instead of a flat host list.
- **Recover VMs (Disaster Recovery tab)** — the former Datastores-tab "Import VMs" wizard is now the DR tab's **Recover VMs** wizard: source datastore/snapshot selection, target PVE host/cluster selection (with inline cluster discovery, optional — not required if the target is already registered), VMID remap, and a new "Start VMs after recovery" option (`provisioning/recovery/restore-vms` gains a `start_after` param, calling the new `start_vms()` primitive). Includes a hint linking to the existing Bind Wizard for datastores not yet mounted on the recovery cluster.
- **Bulk Migrate** — move a set of VMs from one datastore to another, driven entirely from NaSnap (`storage/bulk-migrate-start`). Floating progress panel tracks each VM's migrate job individually with a live progress bar. On completion, the panel now shows an explicit success/failure summary and a Close button (previously it stayed open with no indication that the migration had finished).
- **VM OS type persistence** — detected VM OS type (used for restore/clone icons) is now cached in a new `netapp_vm_os_cache` table and only refreshed in the background, instead of disappearing and reloading on every view. VMs that are off or no longer live keep their last known value.

### Fixed

- **Bulk Migrate / Datastore move progress** — the disk-move progress bar for bulk datastore migration now reflects real progress (previously stuck at 0% while Proxmox showed live progress for the same move).

---

## [1.6.1] — 2026-07-06

### Added

- **PVE Host — package & service stack check** — the "Check All Hosts" health check in Settings → PVE Host Maintenance now also verifies the storage stack on each node: `nfs-common` (binary), `open-iscsi` (binary, `iscsid` service active, initiator name configured), `nvme-cli` (binary, `nvme_tcp` kernel module loaded and persistent across reboots), `lvm2`, and `qemu-utils`. Auto-triggered when a new PVE host is added or imported. For auto-fixable issues a Fix button is shown directly in the UI — enabling `iscsid` and loading/persisting `nvme_tcp` are applied with one click. Missing packages display the required `apt-get install` command.

- **Add-host access test** — each add-host job (NFS, iSCSI, NVMe) now ends with a live access test after all configuration steps complete. NFS: a temporary read-only mount with `timeout 20 mount -t nfs -o vers=3,ro` (temp mountpoint auto-cleaned). iSCSI: verifies an active `iscsiadm` session exists for the target IQN and the LVM VG is accessible. NVMe: verifies the subsystem NQN appears in `nvme list-subsys` and the LVM VG is accessible. The job fails if the access test fails, making misconfigured datastores immediately visible. The host stays registered in the DB so the repair button is available for retry without re-entering parameters.

- **Repair / re-run for existing host connections** — the Add Host dialog now also lists already-connected hosts as repair targets. Re-running add-host for a connected host re-syncs all configuration (ONTAP export rules, iSCSI/NVMe reconnect, LVM VG activation) and runs the access test. Protocol-specific hint shown: NFS → "Re-sync ONTAP export rules", iSCSI → "Re-connect iSCSI + activate VG", NVMe → "Re-connect NVMe + activate VG".

- **Datastore host connectivity in Status column** — the Status column now shows a colored host-count pill: **green** `N/N` = all PVE hosts connected, **orange** `N/M` = some missing, **gray** `0/M` = none. Hovering shows a tooltip listing each host with ✓ (connected) or — (missing). The separate Hosts column has been removed.

### Fixed

- **NFS add-host: export rule false-positive 409** — ONTAP returns HTTP 409 "Entry already exists" when any rule in the policy shares the same `ro-rule`/`rw-rule`/`superuser` values, regardless of whether the client IP is actually in that rule. The previous code treated 409 as proof the client IP was covered, silently skipping the addition and leaving the host with "access denied by server" despite a seemingly successful job. Fixed by pre-checking existing rules via `list_nfs_export_rules()` and only skipping if the client IP is an exact match in a rule's `clients` array.

- **NFS add-host: pvesm status false-negative on new cluster nodes** — on a newly-joined PVE cluster node, `pvesm status <id>` exits non-zero even when the storage is defined in the cluster-wide `storage.cfg` (NFS not yet mounted on that node). This caused NaSnap to re-run `pvesm add`, which then failed with "already defined". Fixed by checking `grep -qw <id> /etc/pve/storage.cfg` directly, which reads the pmxcfs-shared config and is correct on all nodes regardless of mount state.

- **Job tracker ✕ dismiss button non-functional** — `JSON.stringify(jobId)` produces a double-quoted string (`"uuid"`) that conflicts with the surrounding `onclick="…"` HTML attribute quotes, causing the browser to mis-parse the attribute and never register the click handler. Fixed by using single-quote delimiters: `onclick="_dismissJobTracker('uuid')"`.

- **Job tracker: failed jobs never dismissed** — failed jobs remained in the bottom-right panel permanently (dismiss button broken, no auto-dismiss). Failed jobs now auto-dismiss after 30 seconds.

---

## [1.6.0] — 2026-07-03

### Added

- **Liquid Glass theme** — a third UI theme alongside dark and light, inspired by Apple's Liquid Glass design language. The theme toggle in the topbar now cycles dark → light → glass → dark (icon: ☀ / ☽ / ★). Glass mode features an animated deep-blue gradient background with colour orbs, `backdrop-filter` blur on all surfaces (cards, stat boxes, tab bar, toasts, job trackers, modals, wizards), a consistent Apple-style radius scale (panel 18 px, card 16 px, element 12 px, input 10 px, button 10 px, pill 100 px), glass inputs with focus glow, and gradient primary buttons with glow. All wizard overlays (Protection Plan, Restore, Clone, Deploy, Log viewer, …) receive the same glass treatment. The theme is stored in `localStorage` and flash-free on reload. Reverting to dark is a single click — no code change required.

- **SFR — Multi-file and directory restore** — the snapshot panel now supports multi-select: clicking any file or directory toggles it in/out of the selection (checkmark prefix, light-blue highlight). A counter badge ("N selected") appears in the panel header with a ✕ clear button. F5 Copy → VM sends all selected items as a single `tar` stream from the PVE host into the VM via QGA (`tar -czf - | chunks → base64 -d | tar xzf -C dest_dir` pattern). Works for single file, multiple files, and entire directories. The copy button label updates dynamically ("F5 Copy 3 → VM", "F5 Copy dir → VM"). ↓ Download and ↓ tar.gz continue to operate on the most recently clicked single item.

- **SFR — Windows single-file copy** — F5 Copy → VM now works for single files on Windows VMs. Data flows via QGA `agent/file-write` (which writes content verbatim): NaSnap writes base64 chunks to temp files in `C:\Windows\Temp\`, then executes a PowerShell command in the VM to concatenate, decode from base64, and write the final file. Fixed 30 KB chunk size (multiple of 3, no mid-stream padding). Multi-file and directory copy is still unsupported for Windows VMs (use ↓ tar.gz).

- **SFR — Batch QGA OS-type loading in Restore & Clone view** — the VM list in the Restore & Clone tab previously probed each VM's OS type one by one (serialised by `WORKERS=1`), causing up to ~2 s × N VMs of wait before the list was interactive. A new batch endpoint (`sessions/vm-qga-info-batch`) probes all VMs concurrently (up to 12 threads) with one `cluster/resources` call per PVE cluster. Expected speedup: 10 VMs from ~20 s → ~2–3 s.

- **SFR — Finish button** — a `✓ Finish Restore` button in the modal header replaces the emergency-only ✕ Close as the primary completion path. Clicking it awaits `sessions/close` (unmount + full cleanup), shows a success toast, and closes the modal. The old ✕ Close button remains for emergency use.

- **SFR — Custom New Folder dialog** — the F7 New Folder action now shows a NaSnap-styled inline modal (z-index 4000, Enter/Escape key support, error display) instead of the browser's native `prompt()`. Errors from the backend (permission denied, folder already exists) are shown inline in red.

- **SFR — Copy destination bar tracks VM navigation** — clicking into a directory in the VM panel now automatically updates the Copy Destination input to the newly entered directory, keeping copy destination and browsed directory in sync (Total Commander behaviour).

- **SFR — Disk sizes in disk selector** — when multiple disks are available (e.g. EFI system partition, TPM, data disk on Windows Server 2025), the disk selector dropdown now shows the size next to each disk name. `list_san_vm_disks()` uses `lvs --noheadings -o lv_name,lv_size` and `list_snap_disks()` uses `stat -c '%n %s'` to populate size information.

- **Active Directory / LDAP Authentication** — connect NaSnap to an AD domain or any LDAP directory. Users authenticate with their domain credentials; group membership determines the role. Configure in **Settings → Active Directory / LDAP**: server, port, encryption (None / STARTTLS / LDAPS), service-account bind DN and password (AES-256-GCM encrypted at rest), Base DN, user search filter, Admin group DN, and Viewer group DN.
  - Login flow: local accounts are checked first; LDAP is only attempted if no local account matches the username.
  - Users not in either configured group are denied — access is always explicit.
  - Local accounts (including the built-in `admin`) remain fully active regardless of LDAP state — they act as a permanent fallback if AD is unreachable or misconfigured.
  - Service-account bind → `memberOf` search → user-password bind → group check.
  - **Test Connection** button in the settings UI verifies the service-account bind and returns the user's groups before saving.

### Fixed

- **SFR — in-VM assembly of transferred chunks was broken** — the QGA `agent/file-write` API writes the `content` field verbatim (no base64 decoding occurs server-side). Chunk files in the VM therefore contained base64 text, not binary data. The assembly command (`cat chunks | base64 -d`) now correctly decodes the concatenated base64. Chunk size is forced to a multiple of 3 bytes so concatenating chunks never introduces mid-stream `=` padding that would break base64 decoding. Previously, every file copy produced a corrupted file.

- **SFR — Windows mkdir failed silently** — `vm_mkdir` used `cmd.exe /c mkdir "path"` and ignored the exit code entirely, so any failure (permission denied, parent path wrong, folder already exists) was silently swallowed — the API returned `{"ok": True}` and the UI showed "Folder created" while no folder was actually created. Now uses `powershell.exe New-Item -ItemType Directory -Path '...'` (consistent with the rest of the Windows QGA code) and raises a `RuntimeError` with the PowerShell error message on non-zero exit code.

- **SFR — mkdir path double-backslash on Windows** — the new-folder path was constructed by stripping only `/` from the current path, not `\`. On Windows paths ending with `\`, this produced double-backslash in the constructed path (e.g. `C:\Users\\NewFolder`). The strip now uses `replace(/[\/\\]+$/, '')` to handle both separators.

- **Admin-role check broken across all plugin API endpoints** (`_require_admin`) — a missing `from flask import request` import caused every admin-only endpoint (snapshots, schedules, restore, clone, dr, provisioning, recovery, snapmirror, file_restore, settings) to return HTTP 500 instead of 403 when called without admin rights. The `_require_admin` helper now imports `request` correctly. Affected features: UI Features toggles (auto-scan, DR), all write operations.
- **Session role not propagated** — existing sessions after the DB migration had `role = 'viewer'` even for admin users. A migration SQL statement now backfills `role = 'admin'` for sessions belonging to admin users.

---

## [1.5.0] — 2026-06-22

### Added

- **Single File Restore (SFR)** — restore individual files from ONTAP snapshots directly into a running VM without full restore. A Total Commander–style two-panel modal shows the snapshot filesystem (left) and the VM filesystem (right). Features: partition detection with filesystem icons, file browser with Name/Size/Date columns, ↓ Download, ↓ tar.gz, F5 Copy→VM, F8 Delete.
  - **Linux VMs**: large files (>4 MB) transferred via chunked QGA write (SSH reads 1 MB blocks, Python splits into ~30 KB pieces that fit PVE's base64 limit, assembled in-VM via `cat`). No VM network connection required — all data flows through the QGA socket on the PVE host.
  - **Windows VMs**: drive browser ("This PC" view), PowerShell-based file listing (locale-independent), small file copy via QGA. Drive navigation uses backslash paths with proper JS escaping.
  - **OS auto-detection**: detects Windows via QGA `uname` exception ("Failed to execute child process") with `cmd.exe` fallback probe, prevents false "linux" result.
  - **Transfer cancellation**: Abort button cancels the running copy at the next 1 MB block boundary (<2 s latency). Closing the modal or the browser tab also cancels the transfer and unmounts cleanly (`beforeunload` + `fetch keepalive`).
  - **Auto-close progress bar**: after a successful transfer the progress bar auto-hides after 3 seconds.
  - **Session cleanup daemon**: expired SFR sessions (>30 min inactivity) are automatically unmounted and cleaned up in the background.
  - New API routes under `file-restore/`: `sessions/create`, `sessions/mount`, `sessions/umount`, `sessions/ls`, `sessions/copy`, `sessions/copy-status`, `sessions/copy-cancel`, `sessions/close`, `sessions/download`, `sessions/download-tar`.
  - New DB table `netapp_sfr_sessions`.
  - Requires: `paramiko`, `qemu-nbd` on PVE hosts, QEMU Guest Agent running in target VM.

- **Snapshot Timeline — bucket clustering** — snapshot dots are now clustered into time buckets (size scales with zoom: 5 min at hour-view, 6 h at week-view, 1 day at month-view). Multiple snapshots at the same time no longer overlap. Bubble size grows with count (r=5.5 for 1, r=8 for <10, r=10 for ≥10), count shown as label inside the bubble.
- **Snapshot Timeline — Dashboard-consistent colors and interaction** — the timeline now uses the same color logic, hover tooltips, and click behavior as the Dashboard Activity timeline:
  - **Green** (`#56d364`): all snapshots in the window succeeded
  - **Orange** (`#f0a040`): some failures, failure rate < 50 %
  - **Red** (`#f08080`): any scheduled snapshot failed, or failure rate ≥ 50 %
  - **Grey** (muted): ONTAP-native snapshots only (no failure tracking)
  - Hover on single bubble: full snapshot detail + "Click to go to snapshot"
  - Hover on cluster: count header, time window, ● N done / ● N failed / ● N native breakdown, failure rate or scheduled-failure warning
  - Click single: scrolls to snapshot in table with flash highlight
  - Click cluster: opens positioned popup list (identical layout to Dashboard job popup) — each entry clickable, ✕ close, click-outside and Escape close

---

## [1.4.0] — 2026-06-19

### Added

- **Protection Plans (multi-datastore schedules)** — Veeam-style 1:N protection: one plan covers any number of datastores. Assigning multiple datastores to a plan runs one snapshot job per datastore sequentially with independent failure isolation — if one fails, the others continue. The Schedules tab is renamed "Protection" and the wizard title becomes "Protection Plan".
- **Consolidated email notifications** — when a protection plan covers multiple datastores, NaSnap sends one consolidated email per plan run instead of one email per datastore. The email includes a summary table (Datastore | Status | Snapshot | VMs | SnapMirror) and individual per-datastore job log sections in a dark terminal block.
- **Snapshot job progress tracking** — the snapshot engine now updates `progress_pct` at eight milestones (VM fetch → manifest → pre-script → consistency applied → ONTAP snapshot → ONTAP poll → consistency released → done: 5% / 15% / 25% / 45% / 60% / 75% / 90% / 100%). The Activity Log progress bar now reflects real job progress.
- **Activity Log linger** — when all jobs finish, the Activity Log panel stays visible for 10 seconds showing ✓ Done / ✗ Failed state per job instead of disappearing immediately. A new job during the linger window cancels the timer and switches back to active mode.

### Fixed

- **VMs not appearing in Protection view after first run** — for single-datastore plans with "Auto-sync VMs" enabled, the synced VMID list is now persisted back to the plan record after each run. This DB write was accidentally dropped during the multi-DS refactor, causing the VM column to always show "will be populated on first run".
- **Multi-DS VM column** — plans with two or more datastores now show an "Auto" badge plus the VM list (populated after the first run) instead of an empty or misleading state.
- **SnapMirror wizard step disabled for mixed plans** — if a protection plan contained one datastore with SnapMirror and one without, the SnapMirror step was incorrectly disabled. NaSnap now checks all selected datastores in parallel and enables the step as soon as any one of them has a SnapMirror relationship.
- **Datastore host count incorrect** — the Datastores tab showed inflated host counts (e.g. 4 instead of 2) due to stale `netapp_volume_mapping` rows left over from deleted PVE hosts. Host counts are now calculated with an `INNER JOIN` against `netapp_pve_hosts`, and use `volume_uuid` as the join key so a volume visible under different storage names on different hosts is still counted correctly.
- **Datastores tab renamed** — the "Storage" tab is now called "Datastores" across all seven supported locales.
- **Activity Log shows stale failed job permanently** — a failed job from a previous run would resurface in the Activity Log every time a new job completed, eventually blocking the view of current activity. The completed-state view now only shows jobs that were active in the most recent polling cycle.
- **Protection plan Last Run shows "failed" despite successful jobs** — for single-datastore plans the snapshot job runs asynchronously, so reading the job status immediately after launch returned "running", which was interpreted as a failure. The schedule status is now written by the snapshot engine once the job actually completes.

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
