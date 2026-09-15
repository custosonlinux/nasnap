"""
Clone API

  clone/start    POST  – start clone job
  clone/nextid   GET   – next free VMID from PVE
  clone/nodes    GET   – available PVE nodes for a mapping
  clone/orphan-san-clones        GET  – scan ONTAP for leftover temporary SAN
                                         clone objects (volumes / NVMe
                                         subsystems / iSCSI igroups) from
                                         Clone, Single-VM Restore and SFR
  clone/orphan-san-clones/delete POST – delete one, after explicit admin review
"""

import uuid
import json
import logging
from datetime import datetime, timezone

from flask import request
from nasnap_core.core.db import get_db
from nasnap_core.api.plugins import register_plugin_route

from ..core.clone_engine import (
    start_clone_job, start_clone_san_job, start_dr_clone_job,
    start_clone_live_nfs_job, start_clone_live_san_job,
)

log = logging.getLogger(__name__)
from ..core._helpers import PLUGIN_ID  # noqa: F401


def _require_admin():
    from flask import request
    if request.session.get("role") != "admin":
        return {"error": "Admin access required"}, 403
    return None


def _start_clone():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}

    for field in ("src_vmid", "new_vmid"):
        if not str(data.get(field, "")).strip():
            return {"error": f"Required field missing: {field}"}, 400

    db = get_db()
    now      = datetime.now(timezone.utc).isoformat()
    username = request.session.get("user", "system")

    # ── ONTAP-native snapshot (not in plugin DB) ───────────────────────
    if data.get("native"):
        mapping_id = data.get("mapping_id")
        snap_name  = data.get("snap_name")
        if not all([mapping_id, snap_name]):
            return {"error": "mapping_id and snap_name required"}, 400

        from ..core._helpers import get_mapping, load_plugin_config
        mapping = get_mapping(db, mapping_id)
        is_san  = mapping.get("storage_protocol", "nfs") in ("iscsi", "nvme")

        if is_san:
            # SAN: manifest is in the snapmanifest LV inside the snapshot;
            # not directly readable here. Pass empty path so the clone engine
            # falls back to PVE config + VG LV discovery.
            manifest_path = ""
        else:
            cfg = load_plugin_config()
            manifest_subdir = cfg.get("manifest_subdir", ".netapp-snapmanifest")
            manifest_path = (
                f"{mapping['nfs_mount_path']}/.snapshot/{snap_name}"
                f"/{manifest_subdir}/{snap_name}/manifest.json"
            )

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
        if not str(data.get("snapshot_id", "")).strip():
            return {"error": "Required field missing: snapshot_id"}, 400

        snapshot_id = data["snapshot_id"]
        snap = db.query_one("SELECT id, status FROM netapp_snapshots WHERE id=?",
                            (snapshot_id,))
        if not snap:
            return {"error": "Snapshot not found"}, 404
        if snap["status"] != "done":
            return {"error": f"Snapshot not ready (status: {snap['status']})"}, 409

    job_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO netapp_jobs "
        "(id, job_type, snapshot_id, vmid, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, "clone", snapshot_id, int(data["new_vmid"]),
         "running", username, now),
    )

    params = {
        "snapshot_id":     snapshot_id,
        "src_vmid":        data["src_vmid"],
        "new_vmid":        data["new_vmid"],
        "target_node":     data.get("target_node", ""),
        "new_name":        data.get("new_name", ""),
        "start_after":     bool(data.get("start_after", False)),
        "network_isolated": bool(data.get("network_isolated", False)),
    }

    # Route SAN snapshots to the SAN clone engine
    from ..core._helpers import get_mapping, get_snapshot_record
    snap    = get_snapshot_record(db, snapshot_id)
    mapping = get_mapping(db, snap["mapping_id"])
    if mapping.get("storage_protocol") in ("iscsi", "nvme"):
        start_clone_san_job(job_id, params, username)
    else:
        start_clone_job(job_id, params, username)
    return {"success": True, "job_id": job_id}


def _start_clone_live():
    """Clone directly from a VM's CURRENT state — no source snapshot picked.

    NFS: storage-efficient file clone straight from the active filesystem.
    SAN (iSCSI/NVMe): transparently creates a temporary snapshot, clones from
    it, then deletes it — FlexClone for LUN/namespace always needs a named
    snapshot, there is no active-filesystem shortcut for SAN.
    """
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}

    for field in ("src_vmid", "new_vmid", "mapping_id"):
        if not str(data.get(field, "")).strip():
            return {"error": f"Required field missing: {field}"}, 400

    db = get_db()
    now      = datetime.now(timezone.utc).isoformat()
    username = request.session.get("user", "system")

    from ..core._helpers import get_mapping
    try:
        mapping = get_mapping(db, data["mapping_id"])
    except Exception as exc:
        return {"error": str(exc)}, 400

    job_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO netapp_jobs "
        "(id, job_type, vmid, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (job_id, "clone_live", int(data["new_vmid"]), "running", username, now),
    )

    params = {
        "mapping_id":       data["mapping_id"],
        "src_vmid":         data["src_vmid"],
        "new_vmid":         data["new_vmid"],
        "target_node":      data.get("target_node", ""),
        "new_name":         data.get("new_name", ""),
        "start_after":      bool(data.get("start_after", False)),
        "network_isolated": bool(data.get("network_isolated", False)),
    }

    if mapping.get("storage_protocol") in ("iscsi", "nvme"):
        start_clone_live_san_job(job_id, params, username)
    else:
        start_clone_live_nfs_job(job_id, params, username)
    return {"success": True, "job_id": job_id}


def _get_nextid():
    """Returns the next free VMID from the PVE cluster."""
    pve_cluster_id = request.args.get("pve_cluster_id")
    mapping_id = request.args.get("mapping_id")
    if not pve_cluster_id and not mapping_id:
        return {"error": "pve_cluster_id required"}, 400
    db = get_db()
    try:
        from ..core._helpers import build_pve_client, get_mapping, pve_for_mapping
        if mapping_id:
            mgr, _ = pve_for_mapping(db, get_mapping(db, mapping_id))
        else:
            mgr = build_pve_client(db, pve_cluster_id)
        r = mgr._api_get(f"{mgr._base}/cluster/nextid")
        if r.ok:
            return {"vmid": r.json().get("data")}
        return {"error": f"PVE error: {r.status_code}"}, 500
    except Exception as exc:
        return {"error": str(exc)}, 500


def _get_nodes():
    """Returns available PVE nodes for a PVE host."""
    pve_cluster_id = request.args.get("pve_cluster_id")
    if not pve_cluster_id:
        return {"error": "pve_cluster_id required"}, 400
    db = get_db()
    try:
        from ..core._helpers import build_pve_client
        mgr = build_pve_client(db, pve_cluster_id)
        nodes = sorted(mgr.get_node_status().keys())
        if nodes:
            return {"nodes": nodes}
        log.warning("[netapp_storage] clone/nodes: get_node_status returned empty for %s", pve_cluster_id)
    except Exception as exc:
        log.warning("[netapp_storage] clone/nodes: PVE API failed for %s: %s", pve_cluster_id, exc)
    # Fallback: collect nodes seen in recent snapshots for this cluster
    rows = db.query(
        "SELECT DISTINCT node FROM netapp_snapshots "
        "WHERE pve_cluster_id=? AND node != '' "
        "ORDER BY created_at DESC LIMIT 20",
        (pve_cluster_id,),
    )
    nodes = list(dict.fromkeys(r["node"] for r in rows))
    log.info("[netapp_storage] clone/nodes: fallback returned %d nodes for %s", len(nodes), pve_cluster_id)
    return {"nodes": nodes}


def _start_dr_clone():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    for field in ("relationship_id", "snap_name", "src_vmid", "new_vmid", "mapping_id"):
        if not str(data.get(field, "")).strip():
            return {"error": f"Required field missing: {field}"}, 400

    db = get_db()
    username = request.session.get("user", "system")
    now = datetime.now(timezone.utc).isoformat()
    job_id = str(uuid.uuid4())

    db.execute(
        "INSERT INTO netapp_jobs "
        "(id, job_type, vmid, node, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, "clone_dr", int(data["new_vmid"]), "", "running", username, now),
    )

    params = {
        "relationship_id": data["relationship_id"],
        "snap_name":       data["snap_name"],
        "src_vmid":        int(data["src_vmid"]),
        "new_vmid":        int(data["new_vmid"]),
        "new_name":        data.get("new_name", ""),
        "mapping_id":      data["mapping_id"],
        "start_after":     bool(data.get("start_after", False)),
    }
    start_dr_clone_job(job_id, params, username)
    return {"success": True, "job_id": job_id}


# ── Orphan scan: leftover temp SAN clone objects ────────────────────────────
#
# Clone, Single-VM Restore and SFR all create short-lived ONTAP objects under
# these name prefixes (see clone_engine.py, restore_engine.py, sfr_engine.py)
# and are supposed to remove them again within the same run. Unlike Instant
# Recovery, none of these features has a persistent "session" row for most of
# its lifetime, so anything still here that isn't tied to a currently-running
# job (or, for SFR, a currently-open session) has outlived its purpose.

_SAN_CLONE_VOL_PREFIXES    = ("nsclone_", "nsvol_nsclone_", "nasnap_sfr_", "nsvol_nasnap_sfr_")
_SAN_CLONE_SUBSYSTEM_PREFIX = "nsclone-"
_SAN_CLONE_IGROUP_PREFIX    = "nsclone-"


def _job_id_prefix_still_running(db, name, marker):
    """True if `name` embeds a still-'running' netapp_jobs id prefix right
    after `marker` — e.g. 'nsclone_ab12cd34' -> job id starting 'ab12cd34'."""
    token = name.split(marker, 1)[1][:8] if marker in name else ""
    if len(token) < 8:
        return False
    return bool(db.query_one(
        "SELECT 1 FROM netapp_jobs WHERE status='running' AND id LIKE ?", (token + "%",)))


def _sfr_session_active_for(db, name):
    """True if any open SFR session's persisted san_state JSON references this
    clone/subsystem name — SFR sessions can legitimately stay mounted far
    longer than a single job run."""
    if not name:
        return False
    rows = db.query("SELECT san_state FROM netapp_sfr_sessions") or []
    return any(name in (dict(r).get("san_state") or "") for r in rows)


def _scan_orphan_san_clones():
    """Scans every ONTAP endpoint for leftover temporary SAN clone objects
    (volumes / NVMe subsystems / iSCSI igroups) that no in-flight job or open
    SFR session still needs. Read-only; deletion is a separate, explicit,
    per-object step so a same-prefixed object created outside NaSnap can't be
    deleted by accident.
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
        except Exception as exc:
            log.warning(f"[netapp_storage] orphan-san-clone scan: endpoint '{ep['name']}': {exc}")
            continue

        def _add(kind, uuid_, name, svm_name):
            orphans.append({
                "endpoint_id": ep["id"], "endpoint_name": ep["name"],
                "svm_name": svm_name, "kind": kind, "uuid": uuid_, "name": name,
            })

        try:
            for v in client.get_volumes() or []:
                name, uuid_ = v.get("name", ""), v.get("uuid", "")
                if not uuid_ or not name.startswith(_SAN_CLONE_VOL_PREFIXES):
                    continue
                marker = next(p for p in _SAN_CLONE_VOL_PREFIXES if name.startswith(p))
                if _job_id_prefix_still_running(db, name, marker) or _sfr_session_active_for(db, name):
                    continue
                _add("volume", uuid_, name, (v.get("svm") or {}).get("name", ""))
        except Exception as exc:
            log.warning(f"[netapp_storage] orphan-san-clone scan volumes: endpoint '{ep['name']}': {exc}")

        try:
            for s in client.list_nvme_subsystems() or []:
                name, uuid_ = s.get("name", ""), s.get("uuid", "")
                if not uuid_ or not name.startswith(_SAN_CLONE_SUBSYSTEM_PREFIX):
                    continue
                if (_job_id_prefix_still_running(db, name, _SAN_CLONE_SUBSYSTEM_PREFIX)
                        or _sfr_session_active_for(db, name)):
                    continue
                _add("nvme_subsystem", uuid_, name, (s.get("svm") or {}).get("name", ""))
        except Exception as exc:
            log.warning(f"[netapp_storage] orphan-san-clone scan subsystems: endpoint '{ep['name']}': {exc}")

        try:
            for ig in client.list_igroups() or []:
                name, uuid_ = ig.get("name", ""), ig.get("uuid", "")
                if not uuid_ or not name.startswith(_SAN_CLONE_IGROUP_PREFIX):
                    continue
                if _job_id_prefix_still_running(db, name, _SAN_CLONE_IGROUP_PREFIX):
                    continue
                _add("iscsi_igroup", uuid_, name, (ig.get("svm") or {}).get("name", ""))
        except Exception as exc:
            log.warning(f"[netapp_storage] orphan-san-clone scan igroups: endpoint '{ep['name']}': {exc}")

    return {"orphans": orphans}


def _delete_orphan_san_clone():
    """Deletes a single orphaned SAN clone object (volume / NVMe subsystem /
    iSCSI igroup) by uuid, only after the admin has reviewed and explicitly
    confirmed it in the UI."""
    err = _require_admin()
    if err:
        return err
    data        = request.get_json() or {}
    endpoint_id = data.get("endpoint_id", "")
    kind        = data.get("kind", "")
    uuid_       = data.get("uuid", "")
    name        = data.get("name", "")
    svm_name    = data.get("svm_name", "")
    if not endpoint_id or not kind or not uuid_:
        return {"error": "endpoint_id, kind and uuid required"}, 400

    from ..core._helpers import get_endpoint, build_ontap_client
    db = get_db()
    try:
        endpoint = get_endpoint(db, endpoint_id)
        client = build_ontap_client(endpoint)
        if kind == "volume":
            try:
                client.unmount_volume(uuid_)
            except Exception:
                pass
            client._delete_clone_volume(uuid_, name, svm_name)
        elif kind == "nvme_subsystem":
            client.delete_nvme_subsystem(uuid_)
        elif kind == "iscsi_igroup":
            client.delete_igroup(uuid_)
        else:
            return {"error": f"Unknown kind '{kind}'"}, 400
    except Exception as exc:
        return {"error": str(exc)}, 500
    return {"success": True}


def register_routes():
    register_plugin_route(PLUGIN_ID, "clone/start",      _start_clone)
    register_plugin_route(PLUGIN_ID, "clone/start-live", _start_clone_live)
    register_plugin_route(PLUGIN_ID, "clone/dr-start",   _start_dr_clone)
    register_plugin_route(PLUGIN_ID, "clone/nextid",     _get_nextid)
    register_plugin_route(PLUGIN_ID, "clone/orphan-san-clones",        _scan_orphan_san_clones)
    register_plugin_route(PLUGIN_ID, "clone/orphan-san-clones/delete", _delete_orphan_san_clone)
    register_plugin_route(PLUGIN_ID, "clone/nodes",      _get_nodes)
