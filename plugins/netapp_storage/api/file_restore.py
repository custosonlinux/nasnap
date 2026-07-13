"""
Single File Restore API

Sessions track an active disk mount + QGA connection for one VM.
Cleanup thread expires sessions after 30 min of inactivity.
"""

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import Response, request
from nasnap_core.api.plugins import register_plugin_route
from nasnap_core.core.db import get_db

from ..core import sfr_engine
from ..core._helpers import PLUGIN_ID, build_pve_client, get_mapping

log = logging.getLogger(__name__)
_BASE = "file-restore"
_SESSION_TTL_MIN = 30

# In-memory copy-progress store keyed by session ID.
# Updated by background copy threads; polled by _copy_status endpoint.
_copy_progress: dict = {}  # sid → {status, bytes, total, error}

# Session IDs for which a cancel was requested (cleared once the thread notices it).
_copy_cancelled: set = set()


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _require_admin():
    from flask import request
    if request.session.get("role") != "admin":
        return {"error": "Admin access required"}, 403
    return None


def _get_sess(db, sid):
    row = db.query_one("SELECT * FROM netapp_sfr_sessions WHERE id=?", (sid,))
    return dict(row) if row else None


def _touch(db, sid):
    db.execute("UPDATE netapp_sfr_sessions SET last_active=? WHERE id=?", (_utcnow(), sid))


def _os_cache_get(db, vmid, mapping_id):
    row = db.query_one(
        "SELECT guest_os, os_name, os_id FROM netapp_vm_os_cache WHERE vmid=? AND mapping_id=?",
        (int(vmid), mapping_id),
    )
    return dict(row) if row else None


def _os_cache_upsert(db, vmid, mapping_id, guest_os, os_name, os_id):
    if not os_name and not guest_os:
        return
    db.execute(
        "INSERT OR REPLACE INTO netapp_vm_os_cache (vmid, mapping_id, guest_os, os_name, os_id, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (int(vmid), mapping_id, guest_os or "", os_name or "", os_id or "", _utcnow()),
    )


def _os_cache_fallback(db, vmid, mapping_id, base):
    """On a failed/negative live probe, returns the last known OS from cache
    (marked stale) instead of a blank result — VMs that are stopped or no
    longer live keep showing their last valid OS info."""
    try:
        cached = _os_cache_get(db, vmid, mapping_id)
    except Exception:
        cached = None
    if cached and (cached.get("os_name") or cached.get("guest_os")):
        return {**base, "guest_os": cached.get("guest_os") or base.get("guest_os", "linux"),
                "os_name": cached.get("os_name", ""), "os_id": cached.get("os_id", ""), "stale": True}
    return base


def _pve_for_mapping(db, mapping):
    """Try mapping's pve_cluster_id first, then fall back to any configured PVE host."""
    candidate = (mapping or {}).get("pve_cluster_id", "")
    ids = [candidate] if candidate else []
    others = db.query(
        "SELECT id FROM netapp_pve_hosts WHERE id!=? ORDER BY id LIMIT 10", (candidate,)
    )
    ids += [r["id"] for r in (others or [])]
    last_exc = None
    for hid in ids:
        try:
            return build_pve_client(db, hid), hid
        except Exception as e:
            last_exc = e
    raise RuntimeError(f"No accessible PVE host found (tried {len(ids)}): {last_exc}")


def _pve_for_sess(db, sess):
    mapping = get_mapping(db, sess["mapping_id"])
    pve, _ = _pve_for_mapping(db, mapping)
    return pve


def _ontap_client_for_mapping(db, mapping):
    from ..core._helpers import get_endpoint, build_ontap_client
    endpoint = get_endpoint(db, mapping["endpoint_id"])
    return build_ontap_client(endpoint)


def _san_cleanup(db, pve, sess):
    """Full cleanup for a SAN session: guest LVMs → umount → nbd → outer VG → NVMe/iSCSI → ONTAP clone."""
    san_state = {}
    try:
        san_state = json.loads(sess.get("san_state") or "{}")
    except Exception:
        pass
    # umount_session deactivates guest LVMs (stored in san_state for both NFS and SAN)
    guest_lvm_vgs = san_state.get("guest_lvm_vgs", [])
    sfr_engine.umount_session(pve, sess.get("nbd_device", ""), sess.get("mount_path", ""),
                               guest_lvm_vgs=guest_lvm_vgs)
    if san_state and san_state.get("protocol"):
        try:
            mapping = get_mapping(db, sess["mapping_id"])
            client  = _ontap_client_for_mapping(db, mapping)
            sfr_engine.cleanup_san_state(pve, client, san_state)
        except Exception as e:
            log.warning(f"[sfr] san cleanup (ONTAP) {sess.get('id','?')}: {e}")


def _sfr_job_log(db, job_id, msg):
    """Append a log line to an SFR job entry (best-effort)."""
    if not job_id:
        return
    try:
        row = db.query_one("SELECT log_json FROM netapp_jobs WHERE id=?", (job_id,))
        if not row:
            return
        from datetime import datetime, timezone
        entries = json.loads(row["log_json"] or "[]")
        entries.append({"ts": datetime.now(timezone.utc).isoformat(), "msg": msg})
        db.execute("UPDATE netapp_jobs SET log_json=? WHERE id=?",
                   (json.dumps(entries), job_id))
    except Exception as e:
        log.warning(f"[sfr] job_log write {job_id}: {e}")


def _sfr_job_finish(db, job_id, status="done"):
    if not job_id:
        return
    try:
        db.execute(
            "UPDATE netapp_jobs SET status=?, progress_pct=100, completed_at=? WHERE id=?",
            (status, datetime.now(timezone.utc).isoformat(), job_id),
        )
    except Exception as e:
        log.warning(f"[sfr] job_finish {job_id}: {e}")


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _create_session():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    vmid = str(data.get("vmid", "")).strip()
    mapping_id = str(data.get("mapping_id", "")).strip()
    if not vmid or not mapping_id:
        return {"error": "vmid and mapping_id required"}, 400

    db = get_db()
    try:
        mapping = get_mapping(db, mapping_id)
        pve, pve_host_id = _pve_for_mapping(db, mapping)
        node = pve.find_vm_node(int(vmid))
        if not node:
            return {"error": f"VM {vmid} not found on any PVE node"}, 404

        osinfo = sfr_engine.get_osinfo(pve, vmid, node)
        if osinfo is None:
            # get-osinfo unavailable (old QGA) — fall back to ping + uname probe
            if not sfr_engine.check_qga(pve, vmid, node):
                return {
                    "error": "QEMU Guest Agent is not running in this VM. "
                             "Install and start qemu-guest-agent to use Single File Restore."
                }, 409
            guest_os = sfr_engine.detect_guest_os(pve, vmid, node)
            os_name  = "Windows" if guest_os == "windows" else "Linux"
            os_id    = "mswindows" if guest_os == "windows" else "linux"
        else:
            guest_os = osinfo["guest_os"]
            os_name  = osinfo["os_name"]
            os_id    = osinfo["os_id"]

        snaps = db.query(
            "SELECT id, snap_name, created_at, label FROM netapp_snapshots "
            "WHERE mapping_id=? AND status='done' AND vmids_json LIKE ? "
            "ORDER BY created_at DESC LIMIT 200",
            (mapping_id, f"%{vmid}%"),
        )

        sid = str(uuid.uuid4())
        now = _utcnow()
        db.execute(
            "INSERT INTO netapp_sfr_sessions "
            "(id,vmid,node,mapping_id,pve_host_id,storage_id,snap_name,disk_file,"
            "nbd_device,mount_path,partition,guest_os,created_at,last_active) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, vmid, node, mapping_id, pve_host_id,
             mapping["pve_storage_id"], "", "", "", "", "", guest_os, now, now),
        )
        is_san = mapping.get("storage_protocol", "nfs") != "nfs"
        return {
            "session_id": sid,
            "node":       node,
            "guest_os":   guest_os,
            "os_name":    os_name,
            "os_id":      os_id,
            "is_san":     is_san,
            "snapshots":  [dict(s) for s in (snaps or [])],
        }
    except Exception as e:
        log.warning(f"[sfr] create_session: {e}")
        return {"error": str(e)}, 500


def _vm_qga_info():
    """Lightweight QGA probe: check agent status + OS for a single VM.

    Called from the VM list UI to show OS icons and disable the SFR button
    when the agent is not running.  Does NOT create a session.

    Query params: vmid, mapping_id
    Returns: {qga_ok, guest_os, os_name, os_id}
    """
    err = _require_admin()
    if err:
        return err
    vmid       = str(request.args.get("vmid", "")).strip()
    mapping_id = str(request.args.get("mapping_id", "")).strip()
    if not vmid or not mapping_id:
        return {"error": "vmid and mapping_id required"}, 400

    db = get_db()
    try:
        mapping = get_mapping(db, mapping_id)
        pve, _ = _pve_for_mapping(db, mapping)
        node = pve.find_vm_node(int(vmid))
        if not node:
            return _os_cache_fallback(db, vmid, mapping_id,
                {"qga_ok": False, "guest_os": "linux", "os_name": "VM not found", "os_id": ""})
        osinfo = sfr_engine.get_osinfo(pve, vmid, node)
        if osinfo is None:
            # get-osinfo not supported by this QGA version — fall back
            if not sfr_engine.check_qga(pve, vmid, node):
                return _os_cache_fallback(db, vmid, mapping_id,
                    {"qga_ok": False, "guest_os": "linux", "os_name": "Agent not running", "os_id": ""})
            guest_os = sfr_engine.detect_guest_os(pve, vmid, node)
            os_name = "Windows" if guest_os == "windows" else "Linux"
            os_id   = "mswindows" if guest_os == "windows" else "linux"
            _os_cache_upsert(db, vmid, mapping_id, guest_os, os_name, os_id)
            return {"qga_ok": True, "guest_os": guest_os, "os_name": os_name, "os_id": os_id}
        _os_cache_upsert(db, vmid, mapping_id, osinfo.get("guest_os", ""),
                          osinfo.get("os_name", ""), osinfo.get("os_id", ""))
        return {"qga_ok": True, **osinfo}
    except Exception as e:
        log.debug(f"[sfr] vm_qga_info {vmid}: {e}")
        return _os_cache_fallback(db, vmid, mapping_id,
            {"qga_ok": False, "guest_os": "linux", "os_name": "Unavailable", "os_id": ""})


def _vm_qga_info_batch():
    """Batch QGA probe — checks agent status + OS for multiple VMs in parallel.

    POST body: {"vms": [{"vmid": "116", "mapping_id": "abc"}, ...]}
    Returns:   {"results": [{vmid, mapping_id, qga_ok, guest_os, os_name, os_id}, ...]}

    Faster than N individual vm-qga-info calls when WORKERS=1 serialises HTTP
    requests: this endpoint does one cluster/resources call per PVE host, then
    probes all VMs concurrently with a ThreadPoolExecutor (I/O-bound — threads
    overlap the PVE/QGA network latency).
    """
    import concurrent.futures as _cf

    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    vms  = data.get("vms", [])
    if not vms:
        return {"results": []}

    db = get_db()

    # ── Pre-build per-cluster state (main thread — no locking needed) ──────────
    pve_by_mid:  dict = {}   # mapping_id  → pve client
    nodes_by_base: dict = {} # pve._base   → {vmid_str: node_name}

    for vm_spec in vms:
        mid = str(vm_spec.get("mapping_id", "")).strip()
        if not mid or mid in pve_by_mid:
            continue
        try:
            mapping = get_mapping(db, mid)
            pve, _  = _pve_for_mapping(db, mapping)
            pve_by_mid[mid] = pve
            if pve._base not in nodes_by_base:
                r = pve._api_get(f"{pve._base}/cluster/resources?type=vm")
                nodes_by_base[pve._base] = (
                    {str(res.get("vmid")): res.get("node")
                     for res in r.json().get("data", [])}
                    if r.ok else {}
                )
        except Exception as exc:
            log.debug(f"[sfr] batch setup mapping {mid}: {exc}")

    # ── Per-VM probe (runs in thread pool) ─────────────────────────────────────
    def _probe_one(vm_spec):
        vmid = str(vm_spec.get("vmid", "")).strip()
        mid  = str(vm_spec.get("mapping_id", "")).strip()
        base = {"vmid": vmid, "mapping_id": mid}
        # Own DB connection per worker thread — SQLite connections aren't thread-safe.
        tdb = get_db()
        if not vmid or mid not in pve_by_mid:
            return _os_cache_fallback(tdb, vmid, mid,
                {**base, "qga_ok": False, "guest_os": "linux", "os_name": "", "os_id": ""})
        try:
            pve  = pve_by_mid[mid]
            node = nodes_by_base.get(pve._base, {}).get(vmid)
            if not node:
                return _os_cache_fallback(tdb, vmid, mid,
                    {**base, "qga_ok": False, "guest_os": "linux", "os_name": "VM not found", "os_id": ""})
            osinfo = sfr_engine.get_osinfo(pve, vmid, node)
            if osinfo is None:
                if not sfr_engine.check_qga(pve, vmid, node):
                    return _os_cache_fallback(tdb, vmid, mid,
                        {**base, "qga_ok": False, "guest_os": "linux", "os_name": "Agent not running", "os_id": ""})
                guest_os = sfr_engine.detect_guest_os(pve, vmid, node)
                os_name = "Windows" if guest_os == "windows" else "Linux"
                os_id   = "mswindows" if guest_os == "windows" else "linux"
                _os_cache_upsert(tdb, vmid, mid, guest_os, os_name, os_id)
                return {**base, "qga_ok": True, "guest_os": guest_os, "os_name": os_name, "os_id": os_id}
            _os_cache_upsert(tdb, vmid, mid, osinfo.get("guest_os", ""),
                              osinfo.get("os_name", ""), osinfo.get("os_id", ""))
            return {**base, "qga_ok": True, **osinfo}
        except Exception as exc:
            log.debug(f"[sfr] batch probe {vmid}: {exc}")
            return _os_cache_fallback(tdb, vmid, mid,
                {**base, "qga_ok": False, "guest_os": "linux", "os_name": "Unavailable", "os_id": ""})

    max_workers = min(len(vms), 12)
    with _cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_probe_one, vms))

    return {"results": results}


def _close_session():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    sid = str(data.get("sid", "")).strip()
    if not sid:
        return {"error": "sid required"}, 400
    db = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"ok": True}
    # Cancel any running copy and wait for the thread to fully exit before
    # unmounting — a running thread may hold the nbd device via an open SSH channel.
    if _copy_progress.get(sid, {}).get("status") == "running":
        _copy_cancelled.add(sid)
    if sid in _copy_cancelled or _copy_progress.get(sid, {}).get("status") == "running":
        for _ in range(50):  # wait up to 5 s
            if _copy_progress.get(sid, {}).get("status") not in ("running", ""):
                break
            time.sleep(0.1)
    job_id = sess.get("job_id", "")
    _sfr_job_log(db, job_id, "Session closed by user")
    _sfr_job_finish(db, job_id, "done")

    try:
        pve = _pve_for_sess(db, sess)
        if sess.get("san_state"):
            _san_cleanup(db, pve, sess)
        else:
            san_state = {}
            try:
                san_state = json.loads(sess.get("san_state") or "{}")
            except Exception:
                pass
            sfr_engine.umount_session(pve, sess["nbd_device"], sess["mount_path"],
                                      guest_lvm_vgs=san_state.get("guest_lvm_vgs", []))
    except Exception as e:
        log.warning(f"[sfr] umount on close {sid}: {e}")
    db.execute("DELETE FROM netapp_sfr_sessions WHERE id=?", (sid,))
    _copy_progress.pop(sid, None)
    _copy_cancelled.discard(sid)
    return {"ok": True}


def _list_disks():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    sid = str(data.get("sid", "")).strip()
    snap_name = str(data.get("snap_name", "")).strip()
    if not sid or not snap_name:
        return {"error": "sid and snap_name required"}, 400
    db = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"error": "Session not found"}, 404
    try:
        mapping = get_mapping(db, sess["mapping_id"])
        pve = _pve_for_sess(db, sess)
        if mapping.get("storage_protocol", "nfs") != "nfs":
            disks = sfr_engine.list_san_vm_disks(pve, mapping["lvm_vg_name"], sess["vmid"])
        else:
            nfs_mount_path = (mapping.get("nfs_mount_path") or "").rstrip("/") \
                             or f"/mnt/pve/{sess['storage_id']}"
            disks = sfr_engine.list_snap_disks(pve, nfs_mount_path, snap_name, sess["vmid"])
        _touch(db, sid)
        return {"disks": disks}
    except Exception as e:
        return {"error": str(e)}, 500


def _mount():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    sid = str(data.get("sid", "")).strip()
    snap_name = str(data.get("snap_name", "")).strip()
    disk_file = str(data.get("disk_file", "")).strip()
    if not sid or not snap_name or not disk_file:
        return {"error": "sid, snap_name and disk_file required"}, 400
    db = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"error": "Session not found"}, 404
    try:
        mapping = get_mapping(db, sess["mapping_id"])
        pve     = _pve_for_sess(db, sess)
        is_san  = mapping.get("storage_protocol", "nfs") != "nfs"

        # Clean up any previous mount before re-mounting
        if sess["nbd_device"] or sess["mount_path"]:
            if is_san and sess.get("san_state"):
                _san_cleanup(db, pve, sess)
            else:
                sfr_engine.umount_session(pve, sess["nbd_device"], sess["mount_path"])

        if is_san:
            client = _ontap_client_for_mapping(db, mapping)
            res = sfr_engine.mount_san_disk(
                pve, client, mapping, snap_name, sess["vmid"], disk_file, sid)
            san_json = json.dumps(res["san_state"])
        else:
            res = sfr_engine.mount_disk(pve, sid, disk_file)
            # Store guest LVM VGs in san_state so cleanup can deactivate them
            guest_lvm_vgs = res.get("guest_lvm_vgs", [])
            san_json = json.dumps({"guest_lvm_vgs": guest_lvm_vgs}) if guest_lvm_vgs else ""

        # Create / update job entry for this SFR session
        username = getattr(request, "session", {}).get("username", "system") \
                   if hasattr(request, "session") else "system"
        job_id = sess.get("job_id", "")
        if not job_id:
            job_id = str(uuid.uuid4())
            protocol = mapping.get("storage_protocol", "nfs").upper()
            db.execute(
                "INSERT INTO netapp_jobs "
                "(id, job_type, vmid, node, status, log_json, created_by, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (job_id, "sfr", int(sess["vmid"]), sess["node"],
                 "running", "[]", username, _utcnow()),
            )
        _sfr_job_log(db, job_id,
                     f"Mounted snapshot '{snap_name}', disk '{disk_file}' "
                     f"({mapping.get('storage_protocol','nfs').upper()} datastore "
                     f"{mapping.get('pve_storage_id','?')})")
        if res.get("guest_lvm_vgs"):
            _sfr_job_log(db, job_id,
                         f"Guest LVM detected — activated VGs: {', '.join(res['guest_lvm_vgs'])}")

        db.execute(
            "UPDATE netapp_sfr_sessions "
            "SET snap_name=?,disk_file=?,nbd_device=?,mount_path=?,partition='',"
            "san_state=?,job_id=?,last_active=? WHERE id=?",
            (snap_name, disk_file, res["nbd_device"], res["mount_base"],
             san_json, job_id, _utcnow(), sid),
        )
        return {"ok": True, "partitions": res["partitions"]}
    except Exception as e:
        log.warning(f"[sfr] mount {sid}: {e}")
        return {"error": str(e)}, 500


def _select_partition():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    sid = str(data.get("sid", "")).strip()
    part_dev = str(data.get("partition", "")).strip()
    if not sid or not part_dev:
        return {"error": "sid and partition required"}, 400
    db = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"error": "Session not found"}, 404
    if not sess["mount_path"]:
        return {"error": "No disk mounted"}, 400
    try:
        pve = _pve_for_sess(db, sess)
        mount_log = sfr_engine.mount_partition(pve, part_dev, sess["mount_path"])
        _sfr_job_log(db, sess.get("job_id", ""), f"Selected partition {part_dev}")
        db.execute("UPDATE netapp_sfr_sessions SET partition=?,last_active=? WHERE id=?",
                   (part_dev, _utcnow(), sid))
        return {"ok": True, "log": mount_log}
    except Exception as e:
        log.warning(f"[sfr] select_partition {sid}: {e}")
        return {"error": str(e)}, 500


def _snap_ls():
    err = _require_admin()
    if err:
        return err
    sid  = request.args.get("sid", "").strip()
    path = request.args.get("path", "/")
    if not sid:
        return {"error": "sid required"}, 400
    db = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"error": "Session not found"}, 404
    if not sess["partition"]:
        return {"error": "No partition mounted"}, 400
    try:
        pve = _pve_for_sess(db, sess)
        result = sfr_engine.snap_ls(pve, f"{sess['mount_path']}/mnt", path)
        _touch(db, sid)
        return result
    except Exception as e:
        return {"error": str(e), "entries": []}, 500


def _vm_ls():
    err = _require_admin()
    if err:
        return err
    sid  = request.args.get("sid", "").strip()
    path = request.args.get("path", "/")
    if not sid:
        return {"error": "sid required"}, 400
    db = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"error": "Session not found"}, 404
    try:
        pve = _pve_for_sess(db, sess)
        result = sfr_engine.vm_ls(pve, sess["vmid"], sess["node"], path, sess["guest_os"])
        _touch(db, sid)
        return result
    except Exception as e:
        return {"error": str(e), "entries": []}, 500


def _vm_mkdir():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    sid  = str(data.get("sid", "")).strip()
    path = str(data.get("path", "")).strip()
    if not sid or not path:
        return {"error": "sid and path required"}, 400
    db = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"error": "Session not found"}, 404
    try:
        pve = _pve_for_sess(db, sess)
        sfr_engine.vm_mkdir(pve, sess["vmid"], sess["node"], path, sess["guest_os"])
        _touch(db, sid)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}, 500


def _vm_delete():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    sid  = str(data.get("sid", "")).strip()
    path = str(data.get("path", "")).strip()
    if not sid or not path:
        return {"error": "sid and path required"}, 400
    db = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"error": "Session not found"}, 404
    try:
        pve = _pve_for_sess(db, sess)
        sfr_engine.vm_delete(pve, sess["vmid"], sess["node"], path, sess["guest_os"])
        _touch(db, sid)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}, 500


def _copy_to_vm():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    sid       = str(data.get("sid", "")).strip()
    snap_path = str(data.get("snap_path", "")).strip()
    vm_path   = str(data.get("vm_path", "")).strip()
    if not sid or not snap_path or not vm_path:
        return {"error": "sid, snap_path and vm_path required"}, 400
    db = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"error": "Session not found"}, 404
    if not sess["partition"]:
        return {"error": "No partition mounted"}, 400

    # Snapshot everything we need before handing off to background thread
    mount_path = sess["mount_path"]
    vmid       = sess["vmid"]
    node       = sess["node"]
    guest_os   = sess["guest_os"]
    try:
        pve = _pve_for_sess(db, sess)
    except Exception as e:
        return {"error": str(e)}, 500

    _copy_progress[sid]  = {"status": "running", "bytes": 0, "total": -1, "error": ""}
    _copy_cancelled.discard(sid)  # clear any stale cancel flag

    job_id = sess.get("job_id", "")
    _sfr_job_log(db, job_id, f"Copy started: '{snap_path}' → VM:{vm_path}")

    def _run():
        _last_touch = [time.time()]

        def _cb(done, total):
            _copy_progress[sid].update({"bytes": done, "total": total})
            now = time.time()
            if now - _last_touch[0] >= 60:
                try:
                    _touch(get_db(), sid)
                except Exception:
                    pass
                _last_touch[0] = now

        def _cancel_check():
            if sid in _copy_cancelled:
                raise RuntimeError("Cancelled by user")

        try:
            sfr_engine.copy_file_to_vm(
                pve, f"{mount_path}/mnt", snap_path,
                vmid, node, vm_path, guest_os,
                progress_cb=_cb, cancel_check=_cancel_check,
            )
            total = _copy_progress[sid].get("total", 0)
            _copy_progress[sid].update({"status": "done", "bytes": total})
            try:
                _db = get_db()
                _touch(_db, sid)
                _sfr_job_log(_db, job_id,
                             f"Copy complete: '{snap_path}' → VM:{vm_path} "
                             f"({total // 1048576} MB)" if total > 0 else
                             f"Copy complete: '{snap_path}' → VM:{vm_path}")
            except Exception:
                pass
        except Exception as e:
            msg = str(e)
            if sid in _copy_cancelled:
                log.info(f"[sfr] copy_to_vm {sid}: cancelled by user")
                _copy_progress[sid].update({"status": "cancelled", "error": ""})
                _copy_cancelled.discard(sid)
                try:
                    _sfr_job_log(get_db(), job_id,
                                 f"Copy cancelled: '{snap_path}'")
                except Exception:
                    pass
            else:
                log.warning(f"[sfr] copy_to_vm {sid}: {msg}")
                _copy_progress[sid].update({"status": "error", "error": msg})
                try:
                    _sfr_job_log(get_db(), job_id,
                                 f"Copy failed: '{snap_path}' — {msg}")
                except Exception:
                    pass

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "async": True}


def _copy_tar_to_vm():
    err = _require_admin()
    if err:
        return err
    data        = request.get_json() or {}
    sid            = str(data.get("sid", "")).strip()
    snap_paths     = data.get("snap_paths", [])
    vm_dest_dir    = str(data.get("vm_dest_dir", "")).strip()
    is_single_file = bool(data.get("is_single_file", False))
    if not sid or not snap_paths or not vm_dest_dir:
        return {"error": "sid, snap_paths and vm_dest_dir required"}, 400
    if not isinstance(snap_paths, list) or not snap_paths:
        return {"error": "snap_paths must be a non-empty list"}, 400

    db   = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"error": "Session not found"}, 404
    if not sess["partition"]:
        return {"error": "No partition mounted"}, 400

    mount_path  = sess["mount_path"]
    vmid        = sess["vmid"]
    node        = sess["node"]
    guest_os    = sess["guest_os"]
    job_id      = sess.get("job_id", "")

    try:
        pve = _pve_for_sess(db, sess)
    except Exception as e:
        return {"error": str(e)}, 500

    _copy_progress[sid] = {"status": "running", "bytes": 0, "total": -1, "error": ""}
    _copy_cancelled.discard(sid)

    names = ", ".join(p.rstrip("/").split("/")[-1] for p in snap_paths[:3])
    if len(snap_paths) > 3:
        names += f" (+{len(snap_paths)-3} more)"
    _sfr_job_log(db, job_id, f"Copy (tar) started: {names} → VM dir:{vm_dest_dir}")

    def _run():
        _last_touch = [time.time()]

        def _cb(done, total):
            _copy_progress[sid].update({"bytes": done, "total": total})
            now = time.time()
            if now - _last_touch[0] >= 60:
                try:
                    _touch(get_db(), sid)
                except Exception:
                    pass
                _last_touch[0] = now

        def _cancel_check():
            if sid in _copy_cancelled:
                raise RuntimeError("Cancelled by user")

        try:
            sfr_engine.copy_tar_to_vm(
                pve, mount_path, snap_paths, vmid, node, vm_dest_dir,
                guest_os, is_single_file=is_single_file,
                progress_cb=_cb, cancel_check=_cancel_check,
            )
            total      = _copy_progress[sid].get("total", 0)
            done_bytes = _copy_progress[sid].get("bytes", 0)
            _copy_progress[sid].update({"status": "done",
                                         "bytes": max(total, done_bytes)})
            try:
                _db = get_db()
                _touch(_db, sid)
                _sfr_job_log(_db, job_id,
                             f"Copy (tar) complete: {names} → VM dir:{vm_dest_dir}")
            except Exception:
                pass
        except Exception as e:
            msg = str(e)
            if sid in _copy_cancelled:
                log.info(f"[sfr] copy_tar {sid}: cancelled")
                _copy_progress[sid].update({"status": "cancelled", "error": ""})
                _copy_cancelled.discard(sid)
                try:
                    _sfr_job_log(get_db(), job_id, f"Copy (tar) cancelled: {names}")
                except Exception:
                    pass
            else:
                log.warning(f"[sfr] copy_tar {sid}: {msg}")
                _copy_progress[sid].update({"status": "error", "error": msg})
                try:
                    _sfr_job_log(get_db(), job_id,
                                 f"Copy (tar) failed: {names} — {msg}")
                except Exception:
                    pass

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "async": True}


def _copy_status():
    err = _require_admin()
    if err:
        return err
    sid = request.args.get("sid", "").strip()
    return _copy_progress.get(sid, {"status": "unknown"})


def _copy_cancel():
    err = _require_admin()
    if err:
        return err
    sid = str((request.get_json() or {}).get("sid", "")).strip()
    if not sid:
        return {"error": "sid required"}, 400
    _copy_cancelled.add(sid)
    return {"ok": True}


def _download_file():
    err = _require_admin()
    if err:
        return err
    sid  = request.args.get("sid", "").strip()
    path = request.args.get("path", "").strip()
    if not sid or not path:
        return {"error": "sid and path required"}, 400
    db = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"error": "Session not found"}, 404
    if not sess["partition"]:
        return {"error": "No partition mounted"}, 400
    try:
        pve = _pve_for_sess(db, sess)
        raw = sfr_engine.read_snap_file_bytes(pve, f"{sess['mount_path']}/mnt", path)
        filename = path.rsplit("/", 1)[-1] or "download"
        _touch(db, sid)
        return Response(
            raw,
            mimetype="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        return {"error": str(e)}, 500


def _download_tar():
    err = _require_admin()
    if err:
        return err
    sid  = request.args.get("sid", "").strip()
    path = request.args.get("path", "").strip()
    if not sid or not path:
        return {"error": "sid and path required"}, 400
    db = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"error": "Session not found"}, 404
    if not sess["partition"]:
        return {"error": "No partition mounted"}, 400
    try:
        pve = _pve_for_sess(db, sess)
        raw = sfr_engine.snap_tar_bytes(pve, f"{sess['mount_path']}/mnt", path)
        name = (path.strip("/").rsplit("/", 1)[-1] or "archive").replace(" ", "_")
        _touch(db, sid)
        return Response(
            raw,
            mimetype="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{name}.tar.gz"'},
        )
    except Exception as e:
        return {"error": str(e)}, 500


def _umount_disk():
    """Disconnect the mounted qemu-nbd disk but keep the session alive."""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    sid  = str(data.get("sid", "")).strip()
    if not sid:
        return {"error": "sid required"}, 400
    db = get_db()
    sess = _get_sess(db, sid)
    if not sess:
        return {"error": "Session not found"}, 404
    try:
        pve = _pve_for_sess(db, sess)
        if sess.get("san_state"):
            _san_cleanup(db, pve, sess)
        else:
            san_state = {}
            try:
                san_state = json.loads(sess.get("san_state") or "{}")
            except Exception:
                pass
            sfr_engine.umount_session(pve, sess["nbd_device"], sess["mount_path"],
                                      guest_lvm_vgs=san_state.get("guest_lvm_vgs", []))
    except Exception as e:
        log.warning(f"[sfr] umount_disk {sid}: {e}")
    _sfr_job_log(db, sess.get("job_id", ""), "Disk unmounted (user action)")
    db.execute(
        "UPDATE netapp_sfr_sessions "
        "SET snap_name='',disk_file='',nbd_device='',mount_path='',partition='',"
        "san_state='',last_active=? WHERE id=?",
        (_utcnow(), sid),
    )
    return {"ok": True}


# ── Route registration ────────────────────────────────────────────────────────

def register_routes():
    R = register_plugin_route
    R(PLUGIN_ID, f"{_BASE}/sessions/create",                 _create_session)
    R(PLUGIN_ID, f"{_BASE}/sessions/vm-qga-info",           _vm_qga_info)
    R(PLUGIN_ID, f"{_BASE}/sessions/vm-qga-info-batch",     _vm_qga_info_batch)
    R(PLUGIN_ID, f"{_BASE}/sessions/close",            _close_session)
    R(PLUGIN_ID, f"{_BASE}/sessions/umount",           _umount_disk)
    R(PLUGIN_ID, f"{_BASE}/sessions/list-disks",       _list_disks)
    R(PLUGIN_ID, f"{_BASE}/sessions/mount",            _mount)
    R(PLUGIN_ID, f"{_BASE}/sessions/select-partition", _select_partition)
    R(PLUGIN_ID, f"{_BASE}/sessions/snap-ls",          _snap_ls)
    R(PLUGIN_ID, f"{_BASE}/sessions/vm-ls",            _vm_ls)
    R(PLUGIN_ID, f"{_BASE}/sessions/vm-mkdir",         _vm_mkdir)
    R(PLUGIN_ID, f"{_BASE}/sessions/vm-delete",        _vm_delete)
    R(PLUGIN_ID, f"{_BASE}/sessions/copy",             _copy_to_vm)
    R(PLUGIN_ID, f"{_BASE}/sessions/copy-tar",        _copy_tar_to_vm)
    R(PLUGIN_ID, f"{_BASE}/sessions/copy-status",     _copy_status)
    R(PLUGIN_ID, f"{_BASE}/sessions/copy-cancel",     _copy_cancel)
    R(PLUGIN_ID, f"{_BASE}/sessions/download",         _download_file)
    R(PLUGIN_ID, f"{_BASE}/sessions/download-tar",     _download_tar)


# ── Session cleanup daemon ────────────────────────────────────────────────────

def start_sfr_cleanup():
    def _loop():
        while True:
            try:
                _cleanup_expired()
            except Exception as e:
                log.warning(f"[sfr] cleanup loop error: {e}")
            time.sleep(300)

    threading.Thread(target=_loop, daemon=True, name="sfr-cleanup").start()


def _cleanup_expired():
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_SESSION_TTL_MIN)).isoformat()
    rows = db.query("SELECT * FROM netapp_sfr_sessions WHERE last_active < ?", (cutoff,))
    for row in (rows or []):
        sess = dict(row)
        sid  = sess["id"]
        log.info(f"[sfr] Expiring session {sid} (VM {sess['vmid']})")
        # If a copy is still running for this session, cancel it and wait
        if _copy_progress.get(sid, {}).get("status") == "running":
            _copy_cancelled.add(sid)
            for _ in range(30):
                if _copy_progress.get(sid, {}).get("status") != "running":
                    break
                time.sleep(0.1)
        job_id = sess.get("job_id", "")
        _sfr_job_log(db, job_id, "Session expired (30 min inactivity) — auto-cleanup")
        _sfr_job_finish(db, job_id, "done")
        try:
            pve = build_pve_client(db, sess["pve_host_id"])
            if sess.get("san_state"):
                _san_cleanup(db, pve, sess)
            else:
                san_state = {}
                try:
                    san_state = json.loads(sess.get("san_state") or "{}")
                except Exception:
                    pass
                sfr_engine.umount_session(pve, sess["nbd_device"], sess["mount_path"],
                                          guest_lvm_vgs=san_state.get("guest_lvm_vgs", []))
        except Exception as e:
            log.warning(f"[sfr] cleanup umount {sid}: {e}")
        db.execute("DELETE FROM netapp_sfr_sessions WHERE id=?", (sid,))
        _copy_progress.pop(sid, None)
        _copy_cancelled.discard(sid)
