"""
Schedule API for automatic snapshots

  schedules           GET   – list all schedules
  schedules/add       POST  – add schedule
  schedules/update    POST  – update schedule
  schedules/delete    POST  – delete schedule
  schedules/run-now   POST  – run schedule immediately
"""

import os
import re
import uuid
import json
import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import request
from nasnap_core.core.db import get_db
from nasnap_core.api.plugins import register_plugin_route

log = logging.getLogger(__name__)
from ..core._helpers import PLUGIN_ID  # noqa: F401

_scheduler_thread = None
_scheduler_stop = threading.Event()


def _require_admin():
    from flask import request
    if request.session.get("role") != "admin":
        return {"error": "Admin access required"}, 403
    return None


# ── Cron parser (minimal implementation, no external deps) ─────────────────────

def _cron_is_due(cron_expr, last_run_at):
    """Returns True if the schedule is due now.

    Supports: @hourly, @daily, @weekly, @monthly and classic
    5-field cron (minute hour day month weekday).
    Checks only whether the current minute matches the cron expression.
    """
    try:
        tz = ZoneInfo(os.environ.get("TZ", "UTC"))
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)

    aliases = {
        "@hourly":  "0 * * * *",
        "@daily":   "0 0 * * *",
        "@midnight":"0 0 * * *",
        "@weekly":  "0 0 * * 0",
        "@monthly": "0 0 1 * *",
    }
    expr = aliases.get(cron_expr.strip(), cron_expr.strip())

    try:
        parts = expr.split()
        if len(parts) != 5:
            return False
        m_expr, h_expr, dom_expr, mon_expr, dow_expr = parts

        def _match(val, expr):
            if expr == "*":
                return True
            for part in expr.split(","):
                if "/" in part:
                    base, step = part.split("/", 1)
                    start = 0 if base == "*" else int(base)
                    if val >= start and (val - start) % int(step) == 0:
                        return True
                elif "-" in part:
                    lo, hi = part.split("-", 1)
                    if int(lo) <= val <= int(hi):
                        return True
                else:
                    if val == int(part):
                        return True
            return False

        if not (_match(now.minute, m_expr) and _match(now.hour, h_expr) and
                _match(now.day, dom_expr) and _match(now.month, mon_expr) and
                _match(now.weekday(), dow_expr)):
            return False

        # Prevent a schedule from firing twice within the same minute
        if last_run_at:
            try:
                last = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - last).total_seconds() < 60:
                    return False
            except Exception:
                pass

        return True
    except Exception:
        return False


# ── VM sync helper ─────────────────────────────────────────────────────────────

def _sync_vmids_for_schedule(db, mapping_row):
    """Query PVE storage content API and return current vmid list for this datastore."""
    from ..core._helpers import build_pve_client
    storage_id = mapping_row["pve_storage_id"]
    volume_uuid = mapping_row["volume_uuid"]
    is_san = mapping_row.get("storage_protocol", "nfs") in ("iscsi", "nvme")

    host_rows = db.query(
        "SELECT DISTINCT pve_cluster_id FROM netapp_volume_mapping WHERE volume_uuid=?",
        (volume_uuid,),
    )
    host_ids = [r["pve_cluster_id"] for r in host_rows]
    if mapping_row["pve_cluster_id"] not in host_ids:
        host_ids.insert(0, mapping_row["pve_cluster_id"])

    storage_vmids = set()
    for hid in host_ids:
        try:
            pve = build_pve_client(db, hid)
            for node_name in list(pve.get_node_status().keys()):
                rc = pve._api_get(
                    f"{pve._base}/nodes/{node_name}/storage/{storage_id}/content"
                )
                if rc.ok:
                    for item in rc.json().get("data", []):
                        vmid = item.get("vmid")
                        if vmid:
                            storage_vmids.add(int(vmid))
                    if not is_san:
                        break  # NFS is cluster-shared — one node is enough
        except Exception as exc:
            log.warning(f"[netapp_storage] sync_vmids {storage_id} host {hid}: {exc}")
    return sorted(storage_vmids)


# ── Schedule execution ──────────────────────────────────────────────────────────

def _resolve_mapping_ids(schedule):
    """Return the list of mapping IDs for a schedule.

    New schedules store the list in mapping_ids (JSON). Legacy single-datastore
    schedules have only mapping_id — return that wrapped in a list.
    """
    raw = schedule.get("mapping_ids")
    if raw:
        try:
            ids = json.loads(raw)
            if ids:
                return ids
        except Exception:
            pass
    fallback = schedule.get("mapping_id")
    return [fallback] if fallback else []


def _execute_single_ds(schedule, mapping_id, sched_name_safe, suppress_notify=False):
    """Snapshot one datastore for a schedule.

    When suppress_notify=True the per-job notification is skipped; the caller
    is responsible for sending a consolidated notification (multi-DS schedules).

    Returns a dict: {job_id, status, pve_storage_id, snap_name}
    """
    from ..core.snapshot_engine import start_snapshot_job, run_snapshot_sync

    db = get_db()
    result = {"job_id": None, "status": "failed", "pve_storage_id": "", "snap_name": "",
              "vmids": [], "vm_list": [], "snapmirror_info": None}
    job_id = str(uuid.uuid4())
    result["job_id"] = job_id
    try:
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO netapp_jobs (id, job_type, vmid, node, status, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (job_id, "snapshot", None, "", "running", f"schedule:{schedule['name']}", now),
        )

        vmids = json.loads(schedule.get("vmids_json") or "[]")
        mapping_row = db.query_one("SELECT * FROM netapp_volume_mapping WHERE id=?", (mapping_id,))
        if not mapping_row:
            log.warning(f"[netapp_storage] Schedule '{schedule['name']}': mapping {mapping_id} not found, skipping")
            return result

        mapping_row = dict(mapping_row)
        result["pve_storage_id"] = mapping_row.get("pve_storage_id", "")

        if schedule.get("sync_vmids"):
            try:
                synced = _sync_vmids_for_schedule(db, mapping_row)
                if synced is not None:
                    vmids = synced
                    if not suppress_notify:
                        # Single-DS: persist synced VMIDs so they appear in the Protection view
                        db.execute(
                            "UPDATE netapp_snapshot_schedules SET vmids_json=? WHERE id=?",
                            (json.dumps(vmids), schedule["id"]),
                        )
            except Exception as exc:
                log.warning(f"[netapp_storage] Schedule '{schedule['name']}' vmid sync failed: {exc}")
        result["vmids"] = vmids

        node = ""
        cluster_id = mapping_row["pve_cluster_id"]
        try:
            from ..core._helpers import build_pve_client
            pve = build_pve_client(db, cluster_id)
            if vmids:
                found = pve.find_vm_node(vmids[0])
                if found:
                    node = found
            if not node:
                nodes = list(pve.get_node_status().keys())
                if nodes:
                    node = nodes[0]
        except Exception as exc:
            log.warning(f"[netapp_storage] Schedule node lookup failed: {exc}")

        data = {
            "cluster_id":            cluster_id,
            "node":                  node,
            "vmids":                 vmids,
            "mapping_id":            mapping_id,
            "pve_storage_id":        mapping_row["pve_storage_id"],
            "consistency":           schedule.get("consistency", "crash"),
            "label":                 schedule.get("label", ""),
            "pre_script":            schedule.get("pre_script",  "") or "",
            "post_script":           schedule.get("post_script", "") or "",
            "schedule_id":           schedule["id"],
            "schedule_name":         schedule.get("name", ""),
            "snap_name_suffix":      sched_name_safe,
            "retention_count":       int(schedule.get("retention_count") or 7),
            "snapmirror_update":     bool(schedule.get("snapmirror_update", 0)),
            "notify_enabled":        False if suppress_notify else bool(schedule.get("notify_enabled", 0)),
            "notify_on":             schedule.get("notify_on", "all") or "all",
            "notify_recipients":     schedule.get("notify_recipients", "") or "",
            "update_schedule_status": not suppress_notify,
            "tamperproof_enabled":   bool(schedule.get("tamperproof_enabled", 0)),
            "tamperproof_days":      int(schedule.get("tamperproof_days") or 0),
            "sm_tamperproof_enabled": bool(schedule.get("sm_tamperproof_enabled", 0)),
            "sm_tamperproof_days":   int(schedule.get("sm_tamperproof_days") or 0),
        }
        try:
            if suppress_notify:
                # Run synchronously so we can collect results and send one consolidated email
                run_snapshot_sync(job_id, data, f"schedule:{schedule['name']}")
            else:
                start_snapshot_job(job_id, data, f"schedule:{schedule['name']}")
        except Exception as exc:
            log.error(f"[netapp_storage] Schedule '{schedule['name']}' ds={mapping_id} failed: {exc}")

        # Read final job status and log from DB (meaningful only when run_snapshot_sync was used)
        job_row = db.query_one("SELECT status, log_json FROM netapp_jobs WHERE id=?", (job_id,))
        result["status"] = job_row["status"] if job_row else "failed"
        result["log_lines"] = json.loads(job_row["log_json"] or "[]") if job_row else []

        # Read snap_name, VM info, and SnapMirror info for consolidated notification
        snap_row = db.query_one(
            "SELECT snap_name, vmids_json, vm_types_json, manifest_json FROM netapp_snapshots "
            "WHERE id IN (SELECT snapshot_id FROM netapp_jobs WHERE id=?)",
            (job_id,),
        )
        if snap_row:
            result["snap_name"] = snap_row["snap_name"]
            if suppress_notify:
                # Multi-DS: job ran synchronously so manifest is fully written
                try:
                    manifest = json.loads(snap_row["manifest_json"] or "{}")
                    vms = manifest.get("vms", [])
                    result["vm_list"] = [
                        {"vmid": v.get("vmid"), "name": v.get("name", ""), "vm_type": v.get("vm_type", "qemu")}
                        for v in vms if v.get("vmid") is not None
                    ]
                    result["vmids"] = [v["vmid"] for v in result["vm_list"]]
                except Exception:
                    pass
                try:
                    sm_rows = db.query(
                        "SELECT * FROM netapp_snapmirror_relationships WHERE source_volume_uuid=?",
                        (mapping_row["volume_uuid"],),
                    )
                    if sm_rows:
                        rel = dict(sm_rows[0])
                        result["snapmirror_info"] = {
                            "exists":       True,
                            "dest_cluster": rel.get("dest_cluster_name", ""),
                            "dest_svm":     rel.get("dest_svm", ""),
                            "dest_volume":  rel.get("dest_volume", ""),
                            "state":        rel.get("state", ""),
                            "healthy":      bool(rel.get("healthy", 1)),
                            "lag_time":     rel.get("lag_time", ""),
                            "triggered":    bool(schedule.get("snapmirror_update")),
                        }
                    else:
                        result["snapmirror_info"] = {"exists": False}
                except Exception:
                    pass

    except Exception as exc:
        log.error(f"[netapp_storage] Schedule '{schedule['name']}' ds={mapping_id} setup error: {exc}")
    return result


def _execute_schedule(schedule):
    """Executes a schedule — one snapshot job per datastore, sequentially."""
    db = get_db()
    mapping_ids = _resolve_mapping_ids(schedule)
    if not mapping_ids:
        log.warning(f"[netapp_storage] Schedule '{schedule['name']}' has no datastores, skipping")
        return

    sched_name_safe = re.sub(r'[^a-zA-Z0-9_-]', '-', schedule.get("name", ""))[:30].strip('-')
    multi_ds = len(mapping_ids) > 1
    results = []
    for mid in mapping_ids:
        r = _execute_single_ds(schedule, mid, sched_name_safe, suppress_notify=multi_ds)
        results.append(r)

    try:
        if multi_ds:
            # All DS jobs ran synchronously — we have their real final statuses.
            statuses = [r["status"] for r in results]
            overall = "done" if all(s == "done" for s in statuses) else "failed"
            db.execute(
                "UPDATE netapp_snapshot_schedules SET last_run_at=?, last_run_status=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), overall, schedule["id"]),
            )
        else:
            # Single-DS: job runs async; status is still "running" here.
            # Only update last_run_at (to suppress the next scheduler tick).
            # The snapshot engine writes last_run_status when the job actually finishes.
            db.execute(
                "UPDATE netapp_snapshot_schedules SET last_run_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), schedule["id"]),
            )
    except Exception as exc:
        log.error(f"[netapp_storage] Schedule '{schedule['name']}' last_run update failed: {exc}")

    # For multi-DS plans: update vmids_json with the union of all snapshotted VMIDs
    if multi_ds and schedule.get("sync_vmids"):
        try:
            all_vmids = sorted(set(v for r in results for v in (r.get("vmids") or [])))
            db.execute(
                "UPDATE netapp_snapshot_schedules SET vmids_json=? WHERE id=?",
                (json.dumps(all_vmids), schedule["id"]),
            )
        except Exception as exc:
            log.warning(f"[netapp_storage] Schedule '{schedule['name']}' multi-DS vmids update failed: {exc}")

    # Send one consolidated notification for multi-DS schedules
    if multi_ds and schedule.get("notify_enabled") and schedule.get("notify_recipients"):
        try:
            from .settings import send_schedule_consolidated_notification
            send_schedule_consolidated_notification(
                schedule_name=schedule.get("name", ""),
                overall_status=overall,
                ds_results=results,
                recipients_csv=schedule.get("notify_recipients", ""),
                notify_on=schedule.get("notify_on", "all") or "all",
            )
        except Exception as exc:
            log.warning(f"[netapp_storage] Consolidated notification failed: {exc}")


_last_job_purge_day: int = -1   # tracks calendar day of last auto-purge


def _auto_purge_old_jobs(db):
    """Delete completed/failed jobs older than job_log_retention_days. Runs once per day."""
    global _last_job_purge_day
    today = datetime.now(timezone.utc).timetuple().tm_yday
    if today == _last_job_purge_day:
        return
    _last_job_purge_day = today
    try:
        cfg = db.query_one("SELECT job_log_retention_days FROM netapp_plugin_config WHERE id='default'")
        days = int((cfg or {}).get("job_log_retention_days") or 90)
        if days <= 0:
            return
        cutoff = datetime.now(timezone.utc).strftime(f"%Y-%m-%d")
        # Use date arithmetic via SQLite: created_at is ISO-8601
        result = db.query_one(
            "SELECT COUNT(*) AS cnt FROM netapp_jobs "
            "WHERE status IN ('done','failed') "
            "AND created_at < date(?, ?)",
            (cutoff, f"-{days} days"),
        )
        count = (result or {}).get("cnt", 0)
        if count:
            db.execute(
                "DELETE FROM netapp_jobs WHERE status IN ('done','failed') AND created_at < date(?, ?)",
                (cutoff, f"-{days} days"),
            )
            log.info(f"[netapp_storage] Auto-purged {count} job log(s) older than {days} days.")
    except Exception as exc:
        log.warning(f"[netapp_storage] Job auto-purge failed: {exc}")


def _scheduler_loop():
    """Runs as daemon thread; checks for due schedules every minute."""
    log.info("[netapp_storage] Schedule thread started")
    while not _scheduler_stop.wait(60):
        try:
            db = get_db()
            _auto_purge_old_jobs(db)
            schedules = db.query(
                "SELECT * FROM netapp_snapshot_schedules WHERE enabled=1"
            )
            for sched in (schedules or []):
                s = dict(sched)
                if _cron_is_due(s.get("cron_expr", ""), s.get("last_run_at", "")):
                    threading.Thread(
                        target=_execute_schedule, args=(s,), daemon=True
                    ).start()
        except Exception as exc:
            log.error(f"[netapp_storage] Schedule loop error: {exc}")
    log.info("[netapp_storage] Schedule thread stopped")


def start_scheduler():
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()


def stop_scheduler():
    _scheduler_stop.set()


# ── API handlers ───────────────────────────────────────────────────────────────

def _list_schedules():
    db = get_db()
    rows = db.query("SELECT * FROM netapp_snapshot_schedules ORDER BY name")
    # Build mapping lookup once
    all_mappings = {r["id"]: dict(r) for r in (db.query(
        "SELECT vm.*, ep.name AS endpoint_name "
        "FROM netapp_volume_mapping vm "
        "JOIN netapp_endpoints ep ON ep.id = vm.endpoint_id"
    ) or [])}

    result = []
    for r in rows:
        d = dict(r)
        d["vmids"] = json.loads(d.get("vmids_json") or "[]")

        # Resolve datastores list
        mid_list = _resolve_mapping_ids(d)
        datastores = []
        for mid in mid_list:
            vm = all_mappings.get(mid)
            if vm:
                datastores.append({
                    "mapping_id":    mid,
                    "pve_storage_id": vm.get("pve_storage_id", ""),
                    "volume_name":   vm.get("volume_name", ""),
                    "volume_uuid":   vm.get("volume_uuid", ""),
                    "endpoint_name": vm.get("endpoint_name", ""),
                })
        d["datastores"] = datastores
        # Legacy compat: keep flat fields from first datastore
        if datastores:
            d.setdefault("pve_storage_id", datastores[0]["pve_storage_id"])
            d.setdefault("volume_name",    datastores[0]["volume_name"])
            d.setdefault("volume_uuid",    datastores[0]["volume_uuid"])
            d.setdefault("endpoint_name",  datastores[0]["endpoint_name"])

        # SnapMirror: attach for all datastores, return first hit for legacy badge
        sm_found = False
        for ds in datastores:
            sm_rows = db.query(
                "SELECT dest_cluster_name, dest_svm, dest_volume, state, healthy, "
                "lag_time, last_transfer_time "
                "FROM netapp_snapmirror_relationships "
                "WHERE source_volume_uuid=? "
                "ORDER BY healthy DESC, last_transfer_time DESC LIMIT 1",
                (ds.get("volume_uuid", ""),),
            )
            if sm_rows and not sm_found:
                rel = dict(sm_rows[0])
                d["snapmirror"] = {
                    "exists":             True,
                    "dest_cluster":       rel.get("dest_cluster_name", ""),
                    "dest_svm":           rel.get("dest_svm", ""),
                    "dest_volume":        rel.get("dest_volume", ""),
                    "state":              rel.get("state", ""),
                    "healthy":            bool(rel.get("healthy", 0)),
                    "lag_time":           rel.get("lag_time", ""),
                    "last_transfer_time": rel.get("last_transfer_time", ""),
                }
                sm_found = True
        if not sm_found:
            d["snapmirror"] = {"exists": False}

        result.append(d)
    return result


def _add_schedule():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    if not data.get("name"):
        return {"error": "Required field missing: name"}, 400
    if not data.get("cron_expr"):
        return {"error": "Required field missing: cron_expr"}, 400

    # Accept mapping_ids (list) or legacy mapping_id (single)
    mapping_ids = data.get("mapping_ids") or []
    if isinstance(mapping_ids, str):
        mapping_ids = [mapping_ids]
    if not mapping_ids and data.get("mapping_id"):
        mapping_ids = [data["mapping_id"]]
    if not mapping_ids:
        return {"error": "At least one datastore (mapping_ids) is required"}, 400

    primary_mapping_id = mapping_ids[0]
    vmids = data.get("vmids", [])
    if not isinstance(vmids, list):
        vmids = []

    db = get_db()
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    username = request.session.get("user", "system")

    db.execute(
        "INSERT INTO netapp_snapshot_schedules "
        "(id, name, mapping_id, mapping_ids, vmids_json, cron_expr, retention_count, "
        "consistency, label, pre_script, post_script, snapmirror_update, "
        "notify_enabled, notify_on, notify_recipients, sync_vmids, "
        "tamperproof_enabled, tamperproof_days, "
        "sm_tamperproof_enabled, sm_tamperproof_days, enabled, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, data["name"], primary_mapping_id, json.dumps(mapping_ids),
         json.dumps(vmids), data["cron_expr"], int(data.get("retention_count", 7)),
         data.get("consistency", "crash"), data.get("label", ""),
         data.get("pre_script", ""), data.get("post_script", ""),
         1 if data.get("snapmirror_update") else 0,
         1 if data.get("notify_enabled") else 0,
         data.get("notify_on", "all"),
         data.get("notify_recipients", ""),
         1 if data.get("sync_vmids") else 0,
         1 if data.get("tamperproof_enabled") else 0,
         int(data.get("tamperproof_days") or 0),
         1 if data.get("sm_tamperproof_enabled") else 0,
         int(data.get("sm_tamperproof_days") or 0),
         1, username, now),
    )
    return {"success": True, "id": sid}


def _update_schedule():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    sid = data.get("id")
    if not sid:
        return {"error": "id required"}, 400

    db = get_db()
    fields = []
    values = []
    for col, val in [
        ("name", data.get("name")),
        ("cron_expr", data.get("cron_expr")),
        ("retention_count", data.get("retention_count")),
        ("consistency", data.get("consistency")),
        ("label", data.get("label")),
        ("pre_script",  data.get("pre_script")),
        ("post_script", data.get("post_script")),
        ("snapmirror_update", 1 if data.get("snapmirror_update") else (0 if "snapmirror_update" in data else None)),
        ("notify_enabled",    1 if data.get("notify_enabled") else (0 if "notify_enabled" in data else None)),
        ("notify_on",         data.get("notify_on")),
        ("notify_recipients", data.get("notify_recipients")),
        ("sync_vmids",        1 if data.get("sync_vmids") else (0 if "sync_vmids" in data else None)),
        ("tamperproof_enabled",    1 if data.get("tamperproof_enabled") else (0 if "tamperproof_enabled" in data else None)),
        ("tamperproof_days",       data.get("tamperproof_days")),
        ("sm_tamperproof_enabled", 1 if data.get("sm_tamperproof_enabled") else (0 if "sm_tamperproof_enabled" in data else None)),
        ("sm_tamperproof_days",    data.get("sm_tamperproof_days")),
        ("enabled", data.get("enabled")),
        ("vmids_json", json.dumps(data["vmids"]) if "vmids" in data else None),
        ("mapping_id", data.get("mapping_id")),
        ("mapping_ids", json.dumps(data["mapping_ids"]) if "mapping_ids" in data else None),
    ]:
        if val is not None:
            fields.append(f"{col}=?")
            values.append(int(val) if col in ("retention_count", "enabled", "tamperproof_days", "sm_tamperproof_days") else val)
    # Keep mapping_id in sync with first entry of mapping_ids
    if "mapping_ids" in data and data["mapping_ids"]:
        new_primary = data["mapping_ids"][0]
        if f"mapping_id=?" not in ",".join(fields):
            fields.append("mapping_id=?")
            values.append(new_primary)

    if not fields:
        return {"error": "No changes"}, 400

    values.append(sid)
    db.execute(f"UPDATE netapp_snapshot_schedules SET {','.join(fields)} WHERE id=?", values)
    return {"success": True}


def _delete_schedule():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    sid = data.get("id")
    if not sid:
        return {"error": "id required"}, 400
    db = get_db()
    db.execute("DELETE FROM netapp_snapshot_schedules WHERE id=?", (sid,))
    return {"success": True}


def _run_schedule_now():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    sid = data.get("id")
    if not sid:
        return {"error": "id required"}, 400

    db = get_db()
    row = db.query_one("SELECT * FROM netapp_snapshot_schedules WHERE id=?", (sid,))
    if not row:
        return {"error": "Schedule not found"}, 404

    threading.Thread(target=_execute_schedule, args=(dict(row),), daemon=True).start()
    return {"success": True, "message": "Schedule is being executed"}


# ── Route registration ──────────────────────────────────────────────────────────

def register_routes():
    register_plugin_route(PLUGIN_ID, "schedules", _list_schedules)
    register_plugin_route(PLUGIN_ID, "schedules/add", _add_schedule)
    register_plugin_route(PLUGIN_ID, "schedules/update", _update_schedule)
    register_plugin_route(PLUGIN_ID, "schedules/delete", _delete_schedule)
    register_plugin_route(PLUGIN_ID, "schedules/run-now", _run_schedule_now)
