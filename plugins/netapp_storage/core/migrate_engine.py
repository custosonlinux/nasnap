"""
NetApp Storage Plugin — Bulk VM Migration

Moves VM disks from one PVE storage (datastore) to another using Proxmox's
own move_disk (QEMU) / move_volume (LXC) task-based APIs, so a datastore can
be drained of all its VMs in one operation. Proxmox has no built-in bulk
version of this — each VM is migrated independently, several in parallel.
"""

import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from nasnap_core.core.db import get_db

from ._helpers import (
    get_mapping, pve_for_mapping, JobLogger, JobCancelledError, check_cancel,
)
from ._job_registry import register as _reg_register, unregister as _reg_unregister
from .restore_engine import _vm_start, _vm_stop

log = logging.getLogger(__name__)

_DISK_KEY_PREFIXES = ("scsi", "virtio", "ide", "sata", "efidisk", "tpmstate", "rootfs", "mp")
_LXC_KEYS = ("rootfs", "mp")   # these keys use move_volume; everything else uses move_disk


def _disks_on_storage(cfg: dict, storage_id: str) -> list:
    """Disk/mountpoint keys in a live PVE config whose value references storage_id."""
    disks = []
    for k, v in cfg.items():
        if not any(k.startswith(p) for p in _DISK_KEY_PREFIXES):
            continue
        if f"{storage_id}:" not in str(v):
            continue
        disks.append(k)
    return disks


def _vm_status(mgr, node, vmid, vm_type="qemu"):
    vt = "qemu" if vm_type == "qemu" else "lxc"
    r = mgr._api_get(f"{mgr._base}/nodes/{node}/{vt}/{vmid}/status/current")
    if r.ok:
        return r.json().get("data", {}).get("status", "")
    return ""


def vm_tpm_on_storage(mgr, node, vmid, vm_type, storage_id):
    """True if this VM has a tpmstate disk that currently lives on storage_id.

    TPM state can only be move_disk'd while the VM is stopped (PVE limitation) —
    used to warn the user up front, before a migrate that would otherwise fail
    partway through with disks already moved and the TPM device left behind.
    """
    vt_ep = "qemu" if vm_type == "qemu" else "lxc"
    r = mgr._api_get(f"{mgr._base}/nodes/{node}/{vt_ep}/{vmid}/config")
    if not r.ok:
        return False
    cfg = r.json().get("data", {})
    return any(k.startswith("tpmstate") for k in _disks_on_storage(cfg, storage_id))


_PVE_PCT_RE = re.compile(r"\(([\d.]+)\s*%\)")


def _read_pve_task_progress(mgr, node, upid):
    """Best-effort: parses the PVE task log for the most recent '(NN.NN%)' marker.

    move_disk/move_volume are QEMU block-jobs / storage copies whose progress is
    only exposed via free-text task log lines (e.g. 'drive-scsi0: transferred
    2.0 GiB of 20.0 GiB (10.00%)') — there is no dedicated percentage field on
    the task status endpoint itself. Returns a float in [0,1], or None if no
    percentage marker has been logged yet (older PVE / LXC volume copies don't
    always emit one).
    """
    try:
        r = mgr._api_get(f"{mgr._base}/nodes/{node}/tasks/{upid}/log?limit=200")
        if not r.ok:
            return None
        lines = r.json().get("data", [])
        for entry in reversed(lines):
            m = _PVE_PCT_RE.search(entry.get("t", ""))
            if m:
                return max(0.0, min(1.0, float(m.group(1)) / 100.0))
    except Exception:
        pass
    return None


def _poll_pve_task(mgr, node, upid, progress_cb=None, timeout_s=1800, interval_s=2):
    """Polls a PVE task (UPID) until it stops. Raises RuntimeError on failure/timeout.

    progress_cb(fraction), if given, is called with the best-known completion
    fraction (0..1) on every poll tick so callers can reflect live progress.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = mgr._api_get(f"{mgr._base}/nodes/{node}/tasks/{upid}/status")
        if r.ok:
            d = r.json().get("data", {})
            if d.get("status") == "stopped":
                exitstatus = d.get("exitstatus", "")
                if exitstatus == "OK":
                    if progress_cb:
                        progress_cb(1.0)
                    return
                raise RuntimeError(f"PVE task failed: {exitstatus}")
        if progress_cb:
            frac = _read_pve_task_progress(mgr, node, upid)
            if frac is not None:
                progress_cb(frac)
        time.sleep(interval_s)
    raise RuntimeError(f"PVE task {upid} timed out after {timeout_s}s")


def _set_progress(db, job_id, pct):
    db.execute("UPDATE netapp_jobs SET progress_pct=? WHERE id=?", (pct, job_id))


def _finish_job(db, job_id):
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE netapp_jobs SET status='done', progress_pct=100, completed_at=? WHERE id=?",
        (now, job_id),
    )


def _fail_job(db, job_id):
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE netapp_jobs SET status='failed', completed_at=? WHERE id=?",
        (now, job_id),
    )


def _cancel_job(db, job_id):
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE netapp_jobs SET status='cancelled', completed_at=? WHERE id=?",
        (now, job_id),
    )


def _fail_job_with_msg(db, job_id, msg):
    JobLogger(job_id, db).log(f"ERROR: {msg}")
    _fail_job(db, job_id)


def _migrate_one_vm(job_id, vmid, vm_type, node, source_storage_id, target_storage_id, mgr):
    """Moves every disk of one VM that lives on source_storage_id to target_storage_id.

    Runs inside a ThreadPoolExecutor worker thread — opens its own DB connection
    (SQLite connections aren't thread-safe) and registers itself with the job
    registry so the existing generic Cancel button works for this job too.
    """
    db = get_db()
    jlog = JobLogger(job_id, db)
    _reg_register(job_id, threading.current_thread())
    try:
        vt_ep = "qemu" if vm_type == "qemu" else "lxc"
        r = mgr._api_get(f"{mgr._base}/nodes/{node}/{vt_ep}/{vmid}/config")
        if not r.ok:
            raise RuntimeError(f"Could not read VM config: HTTP {r.status_code}")
        cfg = r.json().get("data", {})
        disk_keys = _disks_on_storage(cfg, source_storage_id)

        if not disk_keys:
            jlog.log("No disks found on source storage — nothing to migrate, skipping.")
            _finish_job(db, job_id)
            return

        # TPM state can only be move_disk'd while the VM is stopped (PVE limitation) —
        # migrate every other disk live first, then briefly stop the VM just for the
        # TPM device so a running VM with a TPM doesn't fail partway through with its
        # regular disks already moved and the TPM left stranded on the source storage.
        tpm_keys  = [k for k in disk_keys if k.startswith("tpmstate")]
        live_keys = [k for k in disk_keys if k not in tpm_keys]

        jlog.log(f"Found {len(disk_keys)} disk(s) to migrate: {', '.join(disk_keys)}")
        n = len(disk_keys)
        last_logged_pct = [-1]

        def _move_one(i, key):
            check_cancel(job_id)
            is_lxc_vol = key.startswith(_LXC_KEYS)
            action   = "move_volume" if is_lxc_vol else "move_disk"
            body_key = "volume" if is_lxc_vol else "disk"
            jlog.log(f"Moving {key} → {target_storage_id} …")
            r = mgr._api_post(
                f"{mgr._base}/nodes/{node}/{vt_ep}/{vmid}/{action}",
                {body_key: key, "storage": target_storage_id, "delete": 1},
            )
            if not r.ok:
                raise RuntimeError(f"{key}: HTTP {r.status_code}: {r.text[:200]}")
            upid = r.json().get("data")
            if not upid:
                raise RuntimeError(f"{key}: no task id returned by PVE")

            def _on_progress(frac, i=i, key=key):
                overall_pct = int((i + frac) / n * 100)
                _set_progress(db, job_id, overall_pct)
                # log a line every ~10% so the job log itself shows movement too
                bucket = int(frac * 10)
                if bucket != last_logged_pct[0] and frac < 1.0:
                    last_logged_pct[0] = bucket
                    jlog.log(f"  {key}: {int(frac*100)}% transferred …")

            _poll_pve_task(mgr, node, upid, progress_cb=_on_progress)
            last_logged_pct[0] = -1
            jlog.log(f"{key} moved successfully.")
            _set_progress(db, job_id, int((i + 1) / n * 100))

        for i, key in enumerate(live_keys):
            _move_one(i, key)

        if tpm_keys:
            was_running = _vm_status(mgr, node, vmid, vm_type) == "running"
            try:
                if was_running:
                    jlog.log("TPM device can only be moved while the VM is stopped — shutting down …")
                    _vm_stop(mgr, node, vmid, vm_type)
                    if _vm_status(mgr, node, vmid, vm_type) != "stopped":
                        raise RuntimeError(f"VM {vmid} did not shut down in time — TPM migration aborted")
                for i, key in enumerate(tpm_keys, start=len(live_keys)):
                    _move_one(i, key)
            finally:
                if was_running:
                    jlog.log("Restarting VM …")
                    _vm_start(mgr, node, vmid, vm_type)

        _finish_job(db, job_id)
        jlog.log(f"Migration completed: VM {vmid} → {target_storage_id}")

    except JobCancelledError:
        jlog.log("Job cancelled by user")
        _cancel_job(db, job_id)
    except Exception as exc:
        log.error(f"[netapp_storage] Bulk migrate job {job_id} (VM {vmid}) failed: {exc}")
        jlog.log(f"ERROR: {exc}")
        _fail_job(db, job_id)
    finally:
        _reg_unregister(job_id)


def start_bulk_migrate(source_mapping_id, target_mapping_id, vmids, max_parallel, username):
    """Inserts one netapp_jobs row per VM and launches parallel migration workers.

    Returns the list of job_ids immediately (non-blocking) — the pool runs in a
    daemon thread the caller doesn't wait on.
    """
    db = get_db()
    source = get_mapping(db, source_mapping_id)
    target = get_mapping(db, target_mapping_id)
    mgr, _hid = pve_for_mapping(db, source)
    source_storage_id = source["pve_storage_id"]
    target_storage_id = target["pve_storage_id"]

    res_by_vmid = {}
    r = mgr._api_get(f"{mgr._base}/cluster/resources?type=vm")
    if r.ok:
        for x in r.json().get("data", []):
            vid = x.get("vmid")
            if vid is not None:
                res_by_vmid[int(vid)] = x

    now = datetime.now(timezone.utc).isoformat()
    job_ids = []
    work = []
    for raw_vmid in vmids:
        vmid = int(raw_vmid)
        res = res_by_vmid.get(vmid, {})
        node = res.get("node", "")
        vm_type = res.get("type", "qemu")
        job_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO netapp_jobs (id, job_type, vmid, node, status, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (job_id, "migrate_vm", vmid, node, "running", username, now),
        )
        job_ids.append(job_id)
        if node:
            work.append((job_id, vmid, vm_type, node))
        else:
            _fail_job_with_msg(db, job_id, f"VM {vmid} not found in the PVE cluster")

    def _run_all():
        with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
            futures = [
                pool.submit(_migrate_one_vm, jid, vmid, vt, node,
                            source_storage_id, target_storage_id, mgr)
                for (jid, vmid, vt, node) in work
            ]
            for f in futures:
                f.result()  # exceptions are already caught+logged inside _migrate_one_vm

    threading.Thread(target=_run_all, daemon=True).start()
    return job_ids
