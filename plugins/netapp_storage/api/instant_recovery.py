"""
Instant Recovery API (NFS)

  instant-recovery/start        POST  – boot a VM straight off a FlexClone (from a snapshot,
                                         plugin-managed or ONTAP-native via `native: true`)
  instant-recovery/start-live   POST  – same, but source is a live VM (ad-hoc snapshot first)
  instant-recovery/sessions     GET   – list sessions
  instant-recovery/migrate       POST  – commit: Storage Migrate onto a permanent datastore
  instant-recovery/cleanup-clone POST  – VM was migrated manually (e.g. in Proxmox) — tear down
                                          just the FlexClone/temp storage, never touch the VM
  instant-recovery/dismiss       POST  – remove a completed (migrated) session from the list
                                          (VM already permanent, clone already gone — bookkeeping only)
  instant-recovery/discard       POST  – tear down: destroy the temp VM + FlexClone
  instant-recovery/status        GET   – job status (?job_id=...)
  instant-recovery/orphan-clones        GET  – scan ONTAP for leftover FlexClone
                                                volumes no active session still needs
  instant-recovery/orphan-clones/delete POST – delete one, after explicit admin review
"""

import json
import uuid
import logging
from datetime import datetime, timezone

from flask import request
from nasnap_core.core.db import get_db
from nasnap_core.api.plugins import register_plugin_route

from ..core.instant_recovery_engine import (
    start_instant_recovery_job, start_instant_recovery_migrate_job,
    start_instant_recovery_discard_job, start_instant_recovery_cleanup_job,
)

log = logging.getLogger(__name__)
from ..core._helpers import PLUGIN_ID  # noqa: F401


def _require_admin():
    if request.session.get("role") != "admin":
        return {"error": "Admin access required"}, 403
    return None


def _start():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    db = get_db()

    # ── ONTAP-native snapshot (not in the plugin DB) ────────────────────
    if data.get("native"):
        for field in ("mapping_id", "snap_name", "src_vmid", "new_vmid"):
            if not str(data.get(field, "")).strip():
                return {"error": f"Required field missing: {field}"}, 400

        from ..core._helpers import get_mapping, load_plugin_config
        mapping = get_mapping(db, data["mapping_id"])
        if mapping.get("storage_protocol", "nfs") != "nfs":
            return {"error": "Instant Recovery currently supports NFS datastores only"}, 400

        snap_name = data["snap_name"]
        cfg = load_plugin_config()
        manifest_subdir = cfg.get("manifest_subdir", ".netapp-snapmanifest")
        manifest_path = (
            f"{mapping['nfs_mount_path']}/.snapshot/{snap_name}"
            f"/{manifest_subdir}/{snap_name}/manifest.json"
        )
        now = datetime.now(timezone.utc).isoformat()
        snapshot_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO netapp_snapshots "
            "(id, mapping_id, snap_name, consistency, pve_cluster_id, node, "
            "vmids_json, vm_types_json, manifest_path, manifest_json, label, status, created_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, data["mapping_id"], snap_name, "–",
             mapping["pve_cluster_id"], "",
             "[]", "{}", manifest_path, "", "", "done", now, now),
        )

    # ── Plugin-managed snapshot (already in the DB) ─────────────────────
    else:
        for field in ("snapshot_id", "src_vmid", "new_vmid"):
            if not str(data.get(field, "")).strip():
                return {"error": f"Required field missing: {field}"}, 400
        snapshot_id = data["snapshot_id"]
        snap = db.query_one("SELECT id, status FROM netapp_snapshots WHERE id=?", (snapshot_id,))
        if not snap:
            return {"error": "Snapshot not found"}, 404
        if snap["status"] != "done":
            return {"error": f"Snapshot not ready (status: {snap['status']})"}, 409

    return _launch(db, snapshot_id, data, ad_hoc=False)


def _start_live():
    """Source is a live VM — create a small ad-hoc snapshot of just that VM first
    (matches clone_engine._run_clone_live_san's pattern), then proceed exactly
    like the snapshot-based path. The ad-hoc snapshot is tracked on the session
    so it gets cleaned up alongside the FlexClone (see ad_hoc_snapshot flag)."""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    for field in ("mapping_id", "src_vmid", "new_vmid"):
        if not str(data.get(field, "")).strip():
            return {"error": f"Required field missing: {field}"}, 400

    from ..core._helpers import get_mapping, pve_for_mapping
    from ..core.snapshot_engine import run_snapshot_sync

    db = get_db()
    username = request.session.get("user", "system")
    mapping = get_mapping(db, data["mapping_id"])
    src_vmid = int(data["src_vmid"])

    node = ""
    try:
        mgr, _ = pve_for_mapping(db, mapping)
        node = mgr.find_vm_node(src_vmid) or ""
    except Exception as exc:
        log.warning(f"[netapp_storage] Instant Recovery live: node lookup failed: {exc}")

    temp_job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO netapp_jobs (id, job_type, vmid, node, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (temp_job_id, "snapshot", src_vmid, node, "running", username, now),
    )
    run_snapshot_sync(temp_job_id, {
        "cluster_id": mapping["pve_cluster_id"], "node": node, "vmids": [src_vmid],
        "mapping_id": data["mapping_id"], "consistency": "crash",
        "snap_name_suffix": "instantrecovery",
    }, username)

    job_row = db.query_one("SELECT status, snapshot_id, log_json FROM netapp_jobs WHERE id=?", (temp_job_id,))
    if not job_row or job_row["status"] != "done" or not job_row["snapshot_id"]:
        detail = ""
        try:
            lines = json.loads(job_row["log_json"] or "[]") if job_row else []
            if lines:
                detail = lines[-1].get("msg", "")
        except Exception:
            pass
        return {"error": f"Could not create the source snapshot for Instant Recovery: {detail}"}, 500

    return _launch(db, job_row["snapshot_id"], data, ad_hoc=True)


def _launch(db, snapshot_id, data, ad_hoc):
    username = request.session.get("user", "system")
    now = datetime.now(timezone.utc).isoformat()
    job_id = str(uuid.uuid4())
    src_vmid = int(data["src_vmid"])
    new_vmid = int(data["new_vmid"])
    db.execute(
        "INSERT INTO netapp_jobs (id, job_type, snapshot_id, vmid, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, "instant_recovery_start", snapshot_id, new_vmid, "running", username, now),
    )
    params = {
        "snapshot_id": snapshot_id, "src_vmid": src_vmid, "new_vmid": new_vmid,
        "new_name": data.get("new_name", ""),
        "network_isolated": bool(data.get("network_isolated", True)),
        "ad_hoc_snapshot": ad_hoc,
    }
    start_instant_recovery_job(job_id, params, username)
    return {"success": True, "job_id": job_id}


def _list_sessions():
    db = get_db()
    rows = db.query(
        "SELECT * FROM netapp_instant_recovery_sessions "
        "WHERE status != 'discarded' ORDER BY created_at DESC"
    ) or []
    return [dict(r) for r in rows]


def _migrate():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    session_id = data.get("session_id")
    target_mapping_id = data.get("target_mapping_id")
    if not session_id or not target_mapping_id:
        return {"error": "session_id and target_mapping_id required"}, 400

    db = get_db()
    sess = db.query_one(
        "SELECT * FROM netapp_instant_recovery_sessions WHERE id=?", (session_id,))
    if not sess:
        return {"error": "Session not found"}, 404
    if sess["status"] not in ("running",):
        return {"error": f"Session is not in a migratable state (status: {sess['status']})"}, 409

    from ..core._helpers import get_mapping
    target_mapping = get_mapping(db, target_mapping_id)
    if target_mapping["pve_cluster_id"] != sess["pve_cluster_id"]:
        return {"error": "Target datastore must be on the same PVE cluster as the Instant Recovery VM"}, 400

    username = request.session.get("user", "system")
    now = datetime.now(timezone.utc).isoformat()
    job_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO netapp_jobs (id, job_type, vmid, node, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, "instant_recovery_migrate", sess["new_vmid"], sess["node"], "running", username, now),
    )
    start_instant_recovery_migrate_job(job_id, session_id, target_mapping["pve_storage_id"], username)
    return {"success": True, "job_id": job_id}


def _cleanup_clone():
    """The user migrated the VM's disks themselves outside NaSnap (e.g. staged
    it in Proxmox directly: disks live now, TPM device later once nobody's on
    the VM) — tear down just the FlexClone/temp storage, never touch the VM.
    Refuses if the VM still has any disk on the temp storage, since deleting
    that storage out from under a live disk would corrupt the VM."""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    session_id = data.get("session_id")
    if not session_id:
        return {"error": "session_id required"}, 400

    db = get_db()
    row = db.query_one("SELECT * FROM netapp_instant_recovery_sessions WHERE id=?", (session_id,))
    if not row:
        return {"error": "Session not found"}, 404
    sess = dict(row)
    if sess["status"] not in ("running",):
        return {"error": f"Session is not in a cleanable state (status: {sess['status']})"}, 409

    from ..core._helpers import get_mapping, pve_for_mapping
    from ..core.migrate_engine import _disks_on_storage
    mapping = get_mapping(db, sess["mapping_id"])
    try:
        mgr, _hid = pve_for_mapping(db, mapping)
        vt_ep = "qemu" if sess["vm_type"] == "qemu" else "lxc"
        r = mgr._api_get(f"{mgr._base}/nodes/{sess['node']}/{vt_ep}/{sess['new_vmid']}/config")
    except Exception as exc:
        return {"error": f"Could not reach the VM to verify its disks: {exc}"}, 502
    if not r.ok:
        return {"error": f"Could not read VM config (HTTP {r.status_code}) — "
                          f"refusing to clean up without confirming no disks remain on the temp storage"}, 502
    cfg = r.json().get("data", {})
    remaining = _disks_on_storage(cfg, sess["temp_storage_id"])
    if remaining:
        return {"error": f"VM {sess['new_vmid']} still has disk(s) on the temporary storage: "
                          f"{', '.join(remaining)}. Migrate them (in Proxmox or via Storage Migrate) first."}, 409

    username = request.session.get("user", "system")
    now = datetime.now(timezone.utc).isoformat()
    job_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO netapp_jobs (id, job_type, vmid, node, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, "instant_recovery_cleanup", sess["new_vmid"], sess["node"], "running", username, now),
    )
    start_instant_recovery_cleanup_job(job_id, session_id, username)
    return {"success": True, "job_id": job_id}


def _migrate_tpm_check():
    session_id = request.args.get("session_id", "")
    if not session_id:
        return {"has_tpm": False}
    db = get_db()
    row = db.query_one("SELECT * FROM netapp_instant_recovery_sessions WHERE id=?", (session_id,))
    if not row:
        return {"has_tpm": False}
    sess = dict(row)
    from ..core._helpers import get_mapping, pve_for_mapping
    from ..core.migrate_engine import vm_tpm_on_storage
    try:
        mapping = get_mapping(db, sess["mapping_id"])
        mgr, _ = pve_for_mapping(db, mapping)
        has_tpm = vm_tpm_on_storage(mgr, sess["node"], sess["new_vmid"], sess["vm_type"], sess["temp_storage_id"])
    except Exception:
        has_tpm = False
    return {"has_tpm": has_tpm}


def _dismiss():
    """Removes a completed (migrated) session from the list. The FlexClone and
    temp storage were already torn down at the end of a successful migrate —
    this only clears the bookkeeping row, it never touches the VM. Not to be
    confused with Discard, which stops+destroys the (still temporary) VM."""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    session_id = data.get("session_id")
    if not session_id:
        return {"error": "session_id required"}, 400

    db = get_db()
    sess = db.query_one("SELECT status FROM netapp_instant_recovery_sessions WHERE id=?", (session_id,))
    if not sess:
        return {"error": "Session not found"}, 404
    if sess["status"] != "done":
        return {"error": f"Only a completed (migrated) session can be removed (status: {sess['status']})"}, 409

    db.execute("DELETE FROM netapp_instant_recovery_sessions WHERE id=?", (session_id,))
    return {"success": True}


def _discard():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    session_id = data.get("session_id")
    if not session_id:
        return {"error": "session_id required"}, 400

    db = get_db()
    sess = db.query_one(
        "SELECT * FROM netapp_instant_recovery_sessions WHERE id=?", (session_id,))
    if not sess:
        return {"error": "Session not found"}, 404
    if sess["status"] in ("discarding", "discarded", "migrating"):
        return {"error": f"Session is currently {sess['status']}"}, 409

    username = request.session.get("user", "system")
    now = datetime.now(timezone.utc).isoformat()
    job_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO netapp_jobs (id, job_type, vmid, node, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, "instant_recovery_discard", sess["new_vmid"], sess["node"], "running", username, now),
    )
    start_instant_recovery_discard_job(job_id, session_id, username)
    return {"success": True, "job_id": job_id}


def _status():
    job_id = request.args.get("job_id")
    if not job_id:
        return {"error": "job_id required"}, 400
    db = get_db()
    row = db.query_one("SELECT * FROM netapp_jobs WHERE id=?", (job_id,))
    if not row:
        return {"error": "Job not found"}, 404
    d = dict(row)
    d["log"] = json.loads(d.get("log_json") or "[]")
    return d


def _scan_orphan_flexclones():
    """Scans every ONTAP endpoint for Instant-Recovery FlexClone volumes
    (name prefix 'nsir_', the convention _run_instant_recovery uses) that no
    active session still needs — either nothing references them anymore, or
    the referencing session is already discarded/done/erroring. Read-only;
    deletion is a separate, explicit, per-volume step so a false positive
    (e.g. a same-prefixed volume created outside NaSnap) can't be deleted
    by accident.
    """
    err = _require_admin()
    if err:
        return err
    from ..core._helpers import get_endpoint, build_ontap_client

    db = get_db()
    endpoints = db.query("SELECT id, name FROM netapp_endpoints") or []
    orphans = []
    for ep_row in endpoints:
        ep = dict(ep_row)
        try:
            endpoint = get_endpoint(db, ep["id"])
            client = build_ontap_client(endpoint)
            vols = client.get_volumes()
        except Exception as exc:
            log.warning(f"[netapp_storage] orphan-flexclone scan: endpoint '{ep['name']}': {exc}")
            continue
        for v in (vols or []):
            name = v.get("name", "")
            uuid_ = v.get("uuid", "")
            if not name.startswith("nsir_") or not uuid_:
                continue
            sess_row = db.query_one(
                "SELECT id, status, error, new_vmid, new_name FROM netapp_instant_recovery_sessions "
                "WHERE clone_volume_uuid=?", (uuid_,)
            )
            sess = dict(sess_row) if sess_row else None
            still_active = sess and sess["status"] in ("running", "migrating", "discarding") and not sess["error"]
            if still_active:
                continue
            orphans.append({
                "endpoint_id":      ep["id"],
                "endpoint_name":    ep["name"],
                "svm_name":         (v.get("svm") or {}).get("name", ""),
                "volume_uuid":      uuid_,
                "volume_name":      name,
                "session_id":       sess["id"]     if sess else "",
                "session_status":   sess["status"] if sess else "",
                "session_error":    sess["error"]  if sess else "",
                "session_vmid":     sess["new_vmid"] if sess else 0,
                "session_vm_name":  sess["new_name"] if sess else "",
            })
    return {"orphans": orphans}


def _delete_orphan_flexclone():
    """Deletes a single orphaned FlexClone volume by uuid, only after the
    admin has reviewed and explicitly confirmed it in the UI."""
    err = _require_admin()
    if err:
        return err
    data        = request.get_json() or {}
    endpoint_id = data.get("endpoint_id", "")
    volume_uuid = data.get("volume_uuid", "")
    if not endpoint_id or not volume_uuid:
        return {"error": "endpoint_id and volume_uuid required"}, 400

    from ..core._helpers import get_endpoint, build_ontap_client
    db = get_db()
    try:
        endpoint = get_endpoint(db, endpoint_id)
        client = build_ontap_client(endpoint)
        try:
            client.unmount_volume(volume_uuid)
        except Exception:
            pass  # may already be unmounted — the delete below is what matters
        del_job = client.delete_volume(volume_uuid)
        if del_job:
            client.poll_job(del_job, timeout_s=120)
    except Exception as exc:
        return {"error": str(exc)}, 500

    db.execute(
        "UPDATE netapp_instant_recovery_sessions SET status='discarded', error='' WHERE clone_volume_uuid=?",
        (volume_uuid,),
    )
    return {"success": True}


def register_routes():
    register_plugin_route(PLUGIN_ID, "instant-recovery/start", _start)
    register_plugin_route(PLUGIN_ID, "instant-recovery/start-live", _start_live)
    register_plugin_route(PLUGIN_ID, "instant-recovery/sessions", _list_sessions)
    register_plugin_route(PLUGIN_ID, "instant-recovery/migrate", _migrate)
    register_plugin_route(PLUGIN_ID, "instant-recovery/migrate-tpm-check", _migrate_tpm_check)
    register_plugin_route(PLUGIN_ID, "instant-recovery/cleanup-clone", _cleanup_clone)
    register_plugin_route(PLUGIN_ID, "instant-recovery/dismiss", _dismiss)
    register_plugin_route(PLUGIN_ID, "instant-recovery/discard", _discard)
    register_plugin_route(PLUGIN_ID, "instant-recovery/status", _status)
    register_plugin_route(PLUGIN_ID, "instant-recovery/orphan-clones", _scan_orphan_flexclones)
    register_plugin_route(PLUGIN_ID, "instant-recovery/orphan-clones/delete", _delete_orphan_flexclone)
