"""
Instant Recovery API (NFS)

  instant-recovery/start        POST  – boot a VM straight off a FlexClone (from a snapshot)
  instant-recovery/start-live   POST  – same, but source is a live VM (ad-hoc snapshot first)
  instant-recovery/sessions     GET   – list sessions
  instant-recovery/migrate      POST  – commit: Storage Migrate onto a permanent datastore
  instant-recovery/discard      POST  – tear down: destroy the temp VM + FlexClone
  instant-recovery/status       GET   – job status (?job_id=...)
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
    start_instant_recovery_discard_job,
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
    for field in ("snapshot_id", "src_vmid", "new_vmid"):
        if not str(data.get(field, "")).strip():
            return {"error": f"Required field missing: {field}"}, 400

    db = get_db()
    snap = db.query_one("SELECT id, status FROM netapp_snapshots WHERE id=?", (data["snapshot_id"],))
    if not snap:
        return {"error": "Snapshot not found"}, 404
    if snap["status"] != "done":
        return {"error": f"Snapshot not ready (status: {snap['status']})"}, 409

    return _launch(db, data["snapshot_id"], data, ad_hoc=False)


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


def register_routes():
    register_plugin_route(PLUGIN_ID, "instant-recovery/start", _start)
    register_plugin_route(PLUGIN_ID, "instant-recovery/start-live", _start_live)
    register_plugin_route(PLUGIN_ID, "instant-recovery/sessions", _list_sessions)
    register_plugin_route(PLUGIN_ID, "instant-recovery/migrate", _migrate)
    register_plugin_route(PLUGIN_ID, "instant-recovery/migrate-tpm-check", _migrate_tpm_check)
    register_plugin_route(PLUGIN_ID, "instant-recovery/discard", _discard)
    register_plugin_route(PLUGIN_ID, "instant-recovery/status", _status)
