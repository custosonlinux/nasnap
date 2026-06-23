"""
Restore API

  restore/start   POST  – start restore job (method: sfsr | flexclone)
  restore/status  GET   – query job status (?job_id=...)
  restore/jobs    GET   – all restore jobs (filtered by vmid or snapshot_id)
"""

import uuid
import json
import logging
from datetime import datetime, timezone

from flask import request
from nasnap_core.core.db import get_db
from nasnap_core.api.plugins import register_plugin_route

from ..core.restore_engine import start_restore_job

log = logging.getLogger(__name__)
from ..core._helpers import PLUGIN_ID  # noqa: F401


def _require_admin():
    from flask import request
    if request.session.get("role") != "admin":
        return {"error": "Admin access required"}, 403
    return None


def _start_restore():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}

    method = data.get("method", "sfsr")
    if method not in ("sfsr", "flexclone", "san", "san_single"):
        return {"error": "method must be 'sfsr', 'flexclone', 'san', or 'san_single'"}, 400

    db = get_db()
    username = request.session.get("user", "system")
    now = datetime.now(timezone.utc).isoformat()

    # ── ONTAP-native snapshot (not in plugin DB) ───────────────────────
    if data.get("native"):
        mapping_id = data.get("mapping_id")
        snap_name  = data.get("snap_name")
        vmid       = data.get("vmid")
        if not all([mapping_id, snap_name, vmid]):
            return {"error": "mapping_id, snap_name and vmid required"}, 400

        from ..core._helpers import get_mapping, load_plugin_config
        mapping = get_mapping(db, mapping_id)
        cfg = load_plugin_config()
        manifest_subdir = cfg.get("manifest_subdir", ".netapp-snapmanifest")

        # Manifest is in the .snapshot directory of the NFS volume
        manifest_path = (
            f"{mapping['nfs_mount_path']}/.snapshot/{snap_name}"
            f"/{manifest_subdir}/{snap_name}/manifest.json"
        )

        # Create a temporary snapshot record for the restore engine
        snapshot_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO netapp_snapshots "
            "(id, mapping_id, snap_name, consistency, pve_cluster_id, node, "
            "vmids_json, vm_types_json, manifest_path, manifest_json, label, status, created_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, mapping_id, snap_name, "–",
             mapping["pve_cluster_id"], "",
             "[]", "{}", manifest_path, "", "", "done", now, now),
        )

    # ── Plugin-managed snapshot (in DB) ──────────────────────────────
    else:
        for field in ("snapshot_id", "vmid"):
            if not data.get(field):
                return {"error": f"Required field missing: {field}"}, 400

        snapshot_id = data["snapshot_id"]
        vmid = data["vmid"]
        snap = db.query_one("SELECT id, status FROM netapp_snapshots WHERE id=?", (snapshot_id,))
        if not snap:
            return {"error": "Snapshot not found"}, 404
        if snap["status"] != "done":
            return {"error": f"Snapshot not ready (status: {snap['status']})"}, 409

    job_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO netapp_jobs "
        "(id, job_type, snapshot_id, vmid, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, f"restore_{method}", snapshot_id, int(vmid), "running", username, now),
    )

    start_restore_job(job_id, {"snapshot_id": snapshot_id, "vmid": vmid, "method": method}, username)
    return {"success": True, "job_id": job_id}


def _restore_status():
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


def _list_restore_jobs():
    db = get_db()
    vmid = request.args.get("vmid")
    snap_id = request.args.get("snapshot_id")

    if vmid:
        rows = db.query(
            "SELECT * FROM netapp_jobs WHERE job_type LIKE 'restore%' AND vmid=? "
            "ORDER BY created_at DESC LIMIT 50",
            (int(vmid),),
        )
    elif snap_id:
        rows = db.query(
            "SELECT * FROM netapp_jobs WHERE job_type LIKE 'restore%' AND snapshot_id=? "
            "ORDER BY created_at DESC LIMIT 50",
            (snap_id,),
        )
    else:
        rows = db.query(
            "SELECT * FROM netapp_jobs WHERE job_type LIKE 'restore%' "
            "ORDER BY created_at DESC LIMIT 100"
        )

    result = []
    for r in rows:
        d = dict(r)
        d["log"] = json.loads(d.get("log_json") or "[]")
        result.append(d)
    return result


def _start_dr_restore():
    """Restore from SnapMirror® secondary volume."""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    for field in ("relationship_id", "snap_name", "vmid", "mapping_id"):
        if not data.get(field):
            return {"error": f"Required field missing: {field}"}, 400

    db = get_db()
    username = request.session.get("user", "system")
    now = datetime.now(timezone.utc).isoformat()
    job_id = str(uuid.uuid4())
    vmid = int(data["vmid"])

    db.execute(
        "INSERT INTO netapp_jobs "
        "(id, job_type, vmid, node, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, "restore_dr", vmid, "", "running", username, now),
    )

    params = {
        "relationship_id": data["relationship_id"],
        "snap_name": data["snap_name"],
        "vmid": vmid,
        "mapping_id": data["mapping_id"],
        "vm_type": data.get("vm_type", "qemu"),
        "method": "dr",
    }
    start_restore_job(job_id, params, username)
    return {"success": True, "job_id": job_id}


def register_routes():
    register_plugin_route(PLUGIN_ID, "restore/start", _start_restore)
    register_plugin_route(PLUGIN_ID, "restore/dr-start", _start_dr_restore)
    register_plugin_route(PLUGIN_ID, "restore/status", _restore_status)
    register_plugin_route(PLUGIN_ID, "restore/jobs", _list_restore_jobs)
