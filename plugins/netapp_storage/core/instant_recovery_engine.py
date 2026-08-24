"""
Instant Recovery Engine (NFS)

Boots a VM directly off a NetApp FlexClone of a datastore volume — no data
copy at clone time (space-efficient; the clone only diverges once written
to). The user tests the VM, then either:

  - commits it: "Storage Migrate" moves the VM's disks onto a permanent
    datastore online (reuses migrate_engine._migrate_one_vm — the same
    per-disk move_disk/move_volume mechanism Bulk Migrate uses), then the
    FlexClone is torn down; or
  - discards it: the temporary VM is destroyed and the FlexClone is torn
    down immediately — no lasting footprint either way.

Unlike restore_engine._run_restore_flexclone (which also creates a FlexClone
but then copies each disk out of it before deleting it) or clone_engine's
CoW file-clone (which clones individual files within the SAME volume), this
never copies at creation time — the whole-volume clone is registered as its
own temporary PVE storage and the VM boots straight off it.

NFS only for now — SAN would need its own "leave the cloned VG un-copied"
path, deliberately out of scope for v1.
"""

import json
import re
import random
import shlex
import threading
import uuid
import logging
from datetime import datetime, timedelta, timezone

from nasnap_core.core.db import get_db

from ._helpers import (
    load_plugin_config, get_endpoint, get_mapping, get_snapshot_record,
    build_ontap_client, pve_for_mapping,
    get_ssh_creds, ssh_run, JobLogger, JobCancelledError, check_cancel,
)
from ._job_registry import register as _reg_register, unregister as _reg_unregister
from .restore_engine import (
    _load_manifest, _find_vm_in_manifest, _vm_start, _vm_stop, _resolve_node_host,
)
from .migrate_engine import _migrate_one_vm

log = logging.getLogger(__name__)

_DISK_KEYS = ("scsi", "virtio", "ide", "sata", "efidisk", "tpmstate", "rootfs", "mp")
_REMINDER_AFTER_DAYS = 3


def _random_mac():
    return "52:54:00:%02x:%02x:%02x" % (
        random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)
    )


def _set_progress(db, job_id, pct):
    db.execute("UPDATE netapp_jobs SET progress_pct=? WHERE id=?", (pct, job_id))


def _finish_job(db, job_id):
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE netapp_jobs SET status='done', progress_pct=100, completed_at=? WHERE id=?",
        (now, job_id))


def _fail_job(db, job_id):
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE netapp_jobs SET status='failed', completed_at=? WHERE id=?",
        (now, job_id))


def _cancel_job(db, job_id):
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE netapp_jobs SET status='cancelled', completed_at=? WHERE id=?",
        (now, job_id))


def _build_instant_recovery_config(raw_conf, storage_id_old, storage_id_new,
                                   new_name, vm_type, network_isolated=True):
    """Rewrites only the storage NAME in disk references, not the file path —
    the clone contains the exact same file layout (including the original
    VMID subdirectory) as the source, since nothing was copied or renamed."""
    lines = []
    for k, v in sorted(raw_conf.items()):
        if isinstance(v, (dict, list)) or k == "digest":
            continue
        v = str(v)

        if any(k.startswith(p) for p in _DISK_KEYS) and f"{storage_id_old}:" in v:
            v = v.replace(f"{storage_id_old}:", f"{storage_id_new}:", 1)

        if k.startswith("net"):
            v = re.sub(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', lambda m: _random_mac(), v)
            if network_isolated and ',link_down=1' not in v:
                v = v + ',link_down=1'

        if k in ("name", "hostname") and new_name:
            v = new_name

        lines.append(f"{k}: {v}")

    name_key = "hostname" if vm_type == "lxc" else "name"
    if new_name and not any(l.startswith(f"{name_key}:") for l in lines):
        lines.append(f"{name_key}: {new_name}")
    return "\n".join(lines) + "\n"


# ── Start ─────────────────────────────────────────────────────────────────────

def start_instant_recovery_job(job_id, params, username):
    t = threading.Thread(target=_run_instant_recovery, args=(job_id, params, username), daemon=True)
    t.start()
    _reg_register(job_id, t)


def _run_instant_recovery(job_id, params, username):
    db = get_db()
    jlog = JobLogger(job_id, db)

    snapshot_id = params["snapshot_id"]
    src_vmid    = int(params["src_vmid"])
    new_vmid    = int(params["new_vmid"])
    new_name    = params.get("new_name", "") or f"ir-{src_vmid}"
    # Always started disconnected by default (grilled decision) — avoids
    # IP/MAC conflicts with a source VM that may still be running.
    network_isolated = params.get("network_isolated", True)

    clone_vol_uuid  = ""
    clone_name      = ""
    temp_storage_id = ""
    pve_host = ""
    pve_user, pve_pass, pve_key = "root", "", ""
    mgr = None
    node = ""
    vm_type = "qemu"
    mapping = None

    try:
        snap = get_snapshot_record(db, snapshot_id)
        mapping = get_mapping(db, snap["mapping_id"])
        if mapping.get("storage_protocol", "nfs") != "nfs":
            raise RuntimeError("Instant Recovery currently supports NFS datastores only")
        endpoint = get_endpoint(db, mapping["endpoint_id"])
        client = build_ontap_client(endpoint)

        mgr, resolved_hid = pve_for_mapping(db, mapping)
        node = snap.get("node") or mgr.find_vm_node(src_vmid) or ""
        if not node:
            raise RuntimeError("Could not determine a target PVE node")
        pve_user, pve_pass, pve_key = get_ssh_creds(mgr)
        pve_host = _resolve_node_host(mgr, node)

        vm_types = json.loads(snap.get("vm_types_json") or "{}")
        vm_type = vm_types.get(str(src_vmid), "qemu")
        snap_name = snap["snap_name"]

        jlog.log("Reading manifest …")
        manifest = _load_manifest(snap, mapping, node, mgr, pve_host, pve_user, pve_pass, pve_key)
        vm_entry = _find_vm_in_manifest(manifest, src_vmid)
        if not vm_entry.get("disks"):
            raise RuntimeError(f"No disks in manifest for VM {src_vmid}")
        raw_conf = vm_entry.get("raw_config", {})

        # ── FlexClone the whole volume ─────────────────────────────────
        clone_name = f"nsir_{job_id[:8]}"
        junction   = f"/{clone_name}"
        jlog.log(f"Creating FlexClone '{clone_name}' from snapshot '{snap_name}' …")
        clone_vol_uuid, clone_job_uuid = client.create_flexclone(
            parent_vol_uuid=mapping["volume_uuid"], snap_name=snap_name,
            clone_name=clone_name, svm_name=mapping["svm_name"], junction_path=junction,
        )
        if clone_job_uuid:
            poll_cfg = load_plugin_config()
            client.poll_job(clone_job_uuid,
                            interval_s=poll_cfg.get("job_poll_interval_s", 3),
                            timeout_s=poll_cfg.get("job_poll_timeout_s", 300))
        jlog.log("FlexClone ready — no data copied yet.")
        _set_progress(db, job_id, 35)

        # ── Register the clone as its own temporary PVE storage ────────
        temp_storage_id = clone_name.replace("_", "-")
        nfs_ip = mapping.get("nfs_export_ip") or endpoint["host"]
        jlog.log(f"[{pve_host}] Registering temporary NFS storage '{temp_storage_id}' …")
        ssh_run(pve_host, pve_user, pve_pass,
               f"pvesm add nfs {shlex.quote(temp_storage_id)}"
               f" --server {shlex.quote(nfs_ip)} --export {shlex.quote(junction)}"
               f" --content images",
               key_material=pve_key, timeout=60)
        _set_progress(db, job_id, 55)

        # ── Build and write the instant-recovery VM config ─────────────
        jlog.log(f"Building VM config for {new_vmid} (name: {new_name!r}) …")
        conf_str = _build_instant_recovery_config(
            raw_conf, mapping["pve_storage_id"], temp_storage_id,
            new_name, vm_type, network_isolated=network_isolated,
        )
        conf_subdir = "qemu-server" if vm_type == "qemu" else "lxc"
        conf_path   = f"/etc/pve/{conf_subdir}/{new_vmid}.conf"
        ssh_run(pve_host, pve_user, pve_pass, f"cat > {shlex.quote(conf_path)}",
               stdin_data=conf_str.encode(), key_material=pve_key)
        jlog.log(f"Config written: {conf_path}")
        _set_progress(db, job_id, 75)

        ssh_run(pve_host, pve_user, pve_pass,
               f"qm rescan {new_vmid} 2>/dev/null || true" if vm_type == "qemu"
               else f"pct rescan {new_vmid} 2>/dev/null || true",
               key_material=pve_key)

        jlog.log(f"Starting {vm_type.upper()} {new_vmid} …")
        _vm_start(mgr, node, new_vmid, vm_type)
        _set_progress(db, job_id, 95)

        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO netapp_instant_recovery_sessions "
            "(id, mapping_id, snapshot_id, source_vmid, source_vm_name, new_vmid, new_name, vm_type, "
            "pve_cluster_id, node, temp_storage_id, clone_volume_uuid, clone_volume_name, "
            "junction_path, ad_hoc_snapshot, status, created_at, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, mapping["id"], snapshot_id, src_vmid, vm_entry.get("name", ""),
             new_vmid, new_name, vm_type, resolved_hid, node, temp_storage_id, clone_vol_uuid,
             clone_name, junction, 1 if params.get("ad_hoc_snapshot") else 0,
             "running", now, username),
        )

        _finish_job(db, job_id)
        jlog.log(f"Instant Recovery ready: {vm_type.upper()} {new_vmid} booted from clone '{clone_name}'.")

    except JobCancelledError:
        jlog.log("Job cancelled by user")
        _cancel_job(db, job_id)
        _teardown_raw(pve_host, pve_user, pve_pass, pve_key, temp_storage_id,
                     clone_vol_uuid, (mapping or {}).get("endpoint_id"), db, jlog)
    except Exception as exc:
        log.error(f"[netapp_storage] Instant Recovery job {job_id} failed: {exc}")
        jlog.log(f"ERROR: {exc}")
        _fail_job(db, job_id)
        # Clean up whatever got created before the failure — no orphans on a
        # failed *start* (unlike an active session, which is supposed to
        # persist until the user decides).
        _teardown_raw(pve_host, pve_user, pve_pass, pve_key, temp_storage_id,
                     clone_vol_uuid, (mapping or {}).get("endpoint_id"), db, jlog)
    finally:
        _reg_unregister(job_id)


def _teardown_raw(pve_host, pve_user, pve_pass, pve_key, temp_storage_id,
                  clone_vol_uuid, endpoint_id, db, jlog):
    """Best-effort cleanup with only the raw pieces (used when a session row
    was never created, i.e. the start job itself failed partway through)."""
    if temp_storage_id and pve_host:
        try:
            ssh_run(pve_host, pve_user, pve_pass,
                   f"pvesm remove {shlex.quote(temp_storage_id)} 2>/dev/null; true",
                   key_material=pve_key)
        except Exception as exc:
            log.warning(f"[netapp_storage] cleanup pvesm remove: {exc}")
    if clone_vol_uuid and endpoint_id:
        try:
            endpoint = get_endpoint(db, endpoint_id)
            client = build_ontap_client(endpoint)
            client.unmount_volume(clone_vol_uuid)
            del_job = client.delete_volume(clone_vol_uuid)
            if del_job:
                client.poll_job(del_job, timeout_s=120)
            if jlog:
                jlog.log("Partial FlexClone removed.")
        except Exception as exc:
            log.warning(f"[netapp_storage] cleanup delete_volume: {exc}")


def _teardown_session_clone(db, sess, jlog):
    """Tears down a tracked session's temp storage + FlexClone (+ its ad-hoc
    source snapshot, if one was created just for this session). Shared by
    the migrate-success path and the discard path."""
    mapping = get_mapping(db, sess["mapping_id"])
    endpoint = get_endpoint(db, mapping["endpoint_id"])
    client = build_ontap_client(endpoint)
    mgr, _ = pve_for_mapping(db, mapping)
    pve_host = _resolve_node_host(mgr, sess["node"])
    pve_user, pve_pass, pve_key = get_ssh_creds(mgr)

    try:
        jlog.log(f"[{pve_host}] Removing temporary storage '{sess['temp_storage_id']}' …")
        ssh_run(pve_host, pve_user, pve_pass,
               f"pvesm remove {shlex.quote(sess['temp_storage_id'])} 2>/dev/null; true",
               key_material=pve_key)
    except Exception as exc:
        jlog.log(f"WARNING: pvesm remove failed: {exc}")

    if sess.get("clone_volume_uuid"):
        try:
            jlog.log(f"Deleting FlexClone volume '{sess['clone_volume_name']}' …")
            client.unmount_volume(sess["clone_volume_uuid"])
            del_job = client.delete_volume(sess["clone_volume_uuid"])
            if del_job:
                client.poll_job(del_job, timeout_s=120)
            jlog.log("FlexClone removed.")
        except Exception as exc:
            jlog.log(f"WARNING: FlexClone delete failed: {exc}")

    if sess.get("ad_hoc_snapshot") and sess.get("snapshot_id"):
        try:
            snap = get_snapshot_record(db, sess["snapshot_id"])
            snap_uuid = snap.get("ontap_snap_uuid") or ""
            if not snap_uuid:
                snap_uuid = next(
                    (s["uuid"] for s in client.list_snapshots(mapping["volume_uuid"])
                     if s["name"] == snap["snap_name"]), "")
            if snap_uuid:
                client.delete_snapshot(mapping["volume_uuid"], snap_uuid, snap_name=snap["snap_name"])
            db.execute("DELETE FROM netapp_snapshots WHERE id=?", (sess["snapshot_id"],))
            jlog.log("Temporary source snapshot removed.")
        except Exception as exc:
            jlog.log(f"WARNING: temporary snapshot cleanup failed: {exc}")


# ── Commit: Storage Migrate ─────────────────────────────────────────────────

def start_instant_recovery_migrate_job(job_id, session_id, target_storage_id, username):
    t = threading.Thread(target=_run_instant_recovery_migrate,
                         args=(job_id, session_id, target_storage_id, username), daemon=True)
    t.start()


def _run_instant_recovery_migrate(job_id, session_id, target_storage_id, username):
    db = get_db()
    row = db.query_one("SELECT * FROM netapp_instant_recovery_sessions WHERE id=?", (session_id,))
    if not row:
        _fail_job(db, job_id)
        JobLogger(job_id, db).log("ERROR: Instant Recovery session not found")
        return
    sess = dict(row)
    db.execute("UPDATE netapp_instant_recovery_sessions SET status='migrating' WHERE id=?", (session_id,))

    mapping = get_mapping(db, sess["mapping_id"])
    mgr, _ = pve_for_mapping(db, mapping)
    # _migrate_one_vm manages this job_id's netapp_jobs row lifecycle itself
    # (register/finish/fail/unregister) — same code Bulk Migrate uses.
    _migrate_one_vm(job_id, sess["new_vmid"], sess["vm_type"], sess["node"],
                    sess["temp_storage_id"], target_storage_id, mgr)

    result = db.query_one("SELECT status FROM netapp_jobs WHERE id=?", (job_id,))
    jlog = JobLogger(job_id, db)
    if result and result["status"] == "done":
        jlog.log("All disks migrated — tearing down the FlexClone …")
        try:
            _teardown_session_clone(db, sess, jlog)
            db.execute("UPDATE netapp_instant_recovery_sessions SET status='done' WHERE id=?", (session_id,))
        except Exception as exc:
            jlog.log(f"WARNING: migrate succeeded but clone cleanup failed: {exc}")
            db.execute("UPDATE netapp_instant_recovery_sessions SET status='done', error=? WHERE id=?",
                      (str(exc), session_id))
    else:
        # Partial/failed migrate — session stays visible and retryable, not
        # silently stuck. The clone/temp storage are untouched.
        db.execute(
            "UPDATE netapp_instant_recovery_sessions SET status='running', error=? WHERE id=?",
            ("Storage Migrate did not complete — retry from the session list.", session_id),
        )


# ── Discard ──────────────────────────────────────────────────────────────────

def start_instant_recovery_discard_job(job_id, session_id, username):
    t = threading.Thread(target=_run_instant_recovery_discard,
                         args=(job_id, session_id, username), daemon=True)
    t.start()
    _reg_register(job_id, t)


def _run_instant_recovery_discard(job_id, session_id, username):
    db = get_db()
    jlog = JobLogger(job_id, db)
    try:
        row = db.query_one("SELECT * FROM netapp_instant_recovery_sessions WHERE id=?", (session_id,))
        if not row:
            raise RuntimeError("Instant Recovery session not found")
        sess = dict(row)
        db.execute("UPDATE netapp_instant_recovery_sessions SET status='discarding' WHERE id=?", (session_id,))

        mapping = get_mapping(db, sess["mapping_id"])
        mgr, _ = pve_for_mapping(db, mapping)
        pve_user, pve_pass, pve_key = get_ssh_creds(mgr)
        pve_host = _resolve_node_host(mgr, sess["node"])

        jlog.log(f"Stopping and destroying temporary VM {sess['new_vmid']} …")
        _vm_stop(mgr, sess["node"], sess["new_vmid"], sess["vm_type"])
        ssh_run(pve_host, pve_user, pve_pass,
               f"qm destroy {sess['new_vmid']} --purge 2>/dev/null "
               f"|| pct destroy {sess['new_vmid']} --purge 2>/dev/null || true",
               key_material=pve_key, timeout=60)

        _teardown_session_clone(db, sess, jlog)

        db.execute("UPDATE netapp_instant_recovery_sessions SET status='discarded' WHERE id=?", (session_id,))
        _finish_job(db, job_id)
        jlog.log("Instant Recovery session discarded — no lasting footprint.")
    except Exception as exc:
        log.error(f"[netapp_storage] Instant Recovery discard {session_id} failed: {exc}")
        jlog.log(f"ERROR: {exc}")
        _fail_job(db, job_id)
    finally:
        _reg_unregister(job_id)


# ── Clean up clone only (VM already migrated manually, e.g. in Proxmox) ────

def start_instant_recovery_cleanup_job(job_id, session_id, username):
    t = threading.Thread(target=_run_instant_recovery_cleanup,
                         args=(job_id, session_id, username), daemon=True)
    t.start()
    _reg_register(job_id, t)


def _run_instant_recovery_cleanup(job_id, session_id, username):
    """Tears down just the FlexClone/temp storage — never touches the VM.

    For the case where the user migrated the VM's disks themselves outside
    NaSnap (e.g. in Proxmox directly: disks live now, TPM device later once
    nobody's on the VM). The API layer has already confirmed the VM has no
    disk left on the temp storage before this job is started."""
    db = get_db()
    jlog = JobLogger(job_id, db)
    try:
        row = db.query_one("SELECT * FROM netapp_instant_recovery_sessions WHERE id=?", (session_id,))
        if not row:
            raise RuntimeError("Instant Recovery session not found")
        sess = dict(row)
        db.execute("UPDATE netapp_instant_recovery_sessions SET status='discarding' WHERE id=?", (session_id,))

        jlog.log(f"Cleaning up temporary clone for VM {sess['new_vmid']} — the VM itself is left untouched …")
        _teardown_session_clone(db, sess, jlog)

        db.execute("UPDATE netapp_instant_recovery_sessions SET status='done' WHERE id=?", (session_id,))
        _finish_job(db, job_id)
        jlog.log("Clone cleaned up — VM was not touched.")
    except Exception as exc:
        log.error(f"[netapp_storage] Instant Recovery cleanup {session_id} failed: {exc}")
        jlog.log(f"ERROR: {exc}")
        _fail_job(db, job_id)
        db.execute("UPDATE netapp_instant_recovery_sessions SET status='running', error=? WHERE id=?",
                  (str(exc), session_id))
    finally:
        _reg_unregister(job_id)


# ── Reminder for long-running sessions ──────────────────────────────────────

def check_stale_sessions(db, logger=None):
    """Logs (doesn't touch) any 'running' session older than _REMINDER_AFTER_DAYS
    that hasn't already been reminded about, so an abandoned test doesn't
    silently sit there forever. Called from the hourly scan job."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_REMINDER_AFTER_DAYS)).isoformat()
    rows = db.query(
        "SELECT * FROM netapp_instant_recovery_sessions "
        "WHERE status='running' AND created_at<? AND last_reminder_at=''",
        (cutoff,),
    ) or []
    for r in rows:
        r = dict(r)
        msg = (f"Instant Recovery session for VM {r['new_vmid']} ({r.get('source_vm_name','')}) "
               f"has been running for over {_REMINDER_AFTER_DAYS} days — commit (Storage Migrate) "
               f"or discard it to free the temporary clone.")
        log.warning(f"[netapp_storage] {msg}")
        if logger:
            logger.log(msg)
        db.execute(
            "UPDATE netapp_instant_recovery_sessions SET last_reminder_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), r["id"]),
        )
