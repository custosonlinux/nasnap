"""
Disaster Recovery API  (single-instance model)

One NaSnap instance manages both the primary and DR side itself — it holds
credentials for both the production ONTAP endpoint(s) and the DR-site ONTAP
endpoint(s) + PVE host(s) as local configuration. There is no peer NaSnap
instance to coordinate with: if the instance managing DR is itself part of
the disaster, it must be restored/redeployed first (its config is included
in the regular DB backup/export), after which it can run failover exactly
as before.

Routes:
  dr/plans                        GET
  dr/plans/create                 POST
  dr/plans/detail                 GET  ?plan_id=
  dr/plans/update                 POST
  dr/plans/delete                 POST

  dr/plans/entries/add            POST
  dr/plans/entries/update         POST
  dr/plans/entries/delete         POST
  dr/plans/auto-detect            POST {plan_id}

  dr/plans/groups/create          POST
  dr/plans/groups/update          POST
  dr/plans/groups/delete          POST
  dr/plans/groups/reorder         POST

  dr/plans/groups/vms/add         POST
  dr/plans/groups/vms/delete      POST
  dr/plans/groups/vms/update      POST

  dr/plans/status                 GET  ?plan_id=
  dr/plans/precheck               GET  ?plan_id=
  dr/plans/failover               POST
  dr/plans/failover-jobs          GET  ?plan_id=
  dr/plans/snapshots              GET  ?plan_id=&entry_id=
"""

import json
import logging
import threading
import uuid
from datetime import datetime, timezone

from flask import request, jsonify
from nasnap_core.core.db import get_db
from nasnap_core.api.plugins import register_plugin_route

from ..core._helpers import PLUGIN_ID

log = logging.getLogger(__name__)


# ── Basic helpers ─────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).isoformat()


def _require_admin():
    from flask import request
    if request.session.get("role") != "admin":
        return {"error": "Admin access required"}, 403
    return None


def _json_field(val):
    try:
        return json.loads(val or "[]")
    except Exception:
        return []


def _body():
    return request.get_json(silent=True) or {}


# ── Job helpers ────────────────────────────────────────────────────────────────

def _dr_start_job(job_type, username, plan_id=""):
    db = get_db()
    job_id = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO netapp_jobs (id, job_type, snapshot_id, status, log_json, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, job_type, plan_id, "running", "[]", username, _now())
    )
    return job_id


def _dr_job_log(job_id, lines):
    db = get_db()
    db.execute("UPDATE netapp_jobs SET log_json=? WHERE id=?", (json.dumps(lines), job_id))


def _dr_job_finish(job_id, status, lines):
    db = get_db()
    db.execute(
        "UPDATE netapp_jobs SET status=?, log_json=?, completed_at=? WHERE id=?",
        (status, json.dumps(lines), _now(), job_id)
    )


# ── DR Plans ──────────────────────────────────────────────────────────────────

def _plan_summary(row, db):
    p = dict(row)
    entry_cnt = db.query_one("SELECT COUNT(*) as c FROM netapp_dr_plan_entries WHERE plan_id=?", (p["id"],))
    group_cnt = db.query_one("SELECT COUNT(*) as c FROM netapp_dr_vm_groups WHERE plan_id=?", (p["id"],))
    p["entry_count"] = entry_cnt["c"] if entry_cnt else 0
    p["group_count"] = group_cnt["c"] if group_cnt else 0
    return p


def _list_dr_plans():
    db = get_db()
    rows = db.query("SELECT * FROM netapp_dr_plans ORDER BY name") or []
    return jsonify([_plan_summary(r, db) for r in rows])


def _create_dr_plan():
    err = _require_admin()
    if err: return err
    data = _body()
    name = (data.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}, 400
    db = get_db()
    pid = str(uuid.uuid4())[:8]
    now = _now()
    username = request.session.get("user", "system")
    db.execute(
        "INSERT INTO netapp_dr_plans (id, name, dr_site_id, state, notes, created_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (pid, name, "", "standby", data.get("notes", ""), username, now, now)
    )
    core_id = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO netapp_dr_vm_groups "
        "(id, plan_id, name, group_type, sort_order, start_mode, startup_delay_sec, max_parallel, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (core_id, pid, "Core Infrastructure", "core", 0, "auto", 0, 2, now)
    )
    return jsonify({"id": pid, "message": "DR plan created"}), 201


def _get_dr_plan_detail():
    plan_id = request.args.get("plan_id") or (_body().get("plan_id") or "")
    db = get_db()
    row = db.query_one("SELECT * FROM netapp_dr_plans WHERE id=?", (plan_id,))
    if not row:
        return {"error": "DR plan not found"}, 404
    p = _plan_summary(row, db)
    entries = db.query(
        "SELECT * FROM netapp_dr_plan_entries WHERE plan_id=? ORDER BY sort_order", (plan_id,)
    ) or []
    p["entries"] = [_enrich_entry(dict(e), db) for e in entries]
    groups = db.query(
        "SELECT * FROM netapp_dr_vm_groups WHERE plan_id=? ORDER BY sort_order", (plan_id,)
    ) or []
    p["vm_groups"] = []
    for g in groups:
        grp = dict(g)
        vms = db.query(
            "SELECT * FROM netapp_dr_vm_assignments WHERE group_id=? ORDER BY start_order", (grp["id"],)
        ) or []
        grp["vms"] = [dict(v) for v in vms]
        p["vm_groups"].append(grp)
    return jsonify(p)


def _update_dr_plan():
    err = _require_admin()
    if err: return err
    data = _body()
    plan_id = (data.get("id") or "").strip()
    db = get_db()
    if not plan_id or not db.query_one("SELECT id FROM netapp_dr_plans WHERE id=?", (plan_id,)):
        return {"error": "DR plan not found"}, 404
    updates, params = [], []
    for k in ("name", "notes"):
        if k in data:
            updates.append(f"{k}=?"); params.append(data[k])
    if not updates:
        return {"error": "No fields to update"}, 400
    updates.append("updated_at=?"); params.extend([_now(), plan_id])
    db.execute(f"UPDATE netapp_dr_plans SET {', '.join(updates)} WHERE id=?", params)
    return jsonify({"message": "DR plan updated"})


def _delete_dr_plan():
    err = _require_admin()
    if err: return err
    data = _body()
    plan_id = (data.get("id") or "").strip()
    db = get_db()
    row = db.query_one("SELECT state FROM netapp_dr_plans WHERE id=?", (plan_id,))
    if not row:
        return {"error": "DR plan not found"}, 404
    if row["state"] not in ("standby",):
        return {"error": f"Cannot delete plan in state '{row['state']}'"}, 409
    db.execute("DELETE FROM netapp_dr_plans WHERE id=?", (plan_id,))
    return jsonify({"message": "DR plan deleted"})


# ── Plan Entries ──────────────────────────────────────────────────────────────

def _lookup_primary_storage_id(db, source_endpoint_id, source_svm, source_volume):
    row = db.query_one(
        "SELECT pve_storage_id FROM netapp_volume_mapping "
        "WHERE endpoint_id=? AND svm_name=? AND volume_name=? LIMIT 1",
        (source_endpoint_id, source_svm, source_volume)
    )
    if row and row["pve_storage_id"]:
        return row["pve_storage_id"]
    row = db.query_one(
        "SELECT pve_storage_id FROM netapp_provisioned_datastores "
        "WHERE endpoint_id=? AND svm_name=? AND volume_name=? LIMIT 1",
        (source_endpoint_id, source_svm, source_volume)
    )
    return row["pve_storage_id"] if row and row["pve_storage_id"] else ""


def _enrich_entry(entry, db):
    ep = db.query_one("SELECT name FROM netapp_endpoints WHERE id=?", (entry.get("source_endpoint_id", ""),))
    entry["source_endpoint_name"] = ep["name"] if ep else ""
    dr_ep = db.query_one("SELECT name FROM netapp_endpoints WHERE id=?", (entry.get("dr_endpoint_id", ""),))
    entry["dr_endpoint_name"] = dr_ep["name"] if dr_ep else ""
    entry["dr_pve_host_ids"] = _json_field(entry.get("dr_pve_host_ids"))

    primary_storage_id = _lookup_primary_storage_id(
        db, entry.get("source_endpoint_id", ""),
        entry.get("source_svm", ""), entry.get("source_volume", "")
    )
    entry["source_pve_storage_id"] = primary_storage_id
    if not entry.get("dr_pve_storage_id") and primary_storage_id:
        entry["dr_pve_storage_id"] = primary_storage_id
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                db.execute("UPDATE netapp_dr_plan_entries SET dr_pve_storage_id=? WHERE id=?",
                           (primary_storage_id, entry["id"]))
            except Exception:
                pass

    if entry.get("snapmirror_rel_uuid"):
        rel = db.query_one(
            "SELECT state, healthy, lag_time, last_transfer_time "
            "FROM netapp_snapmirror_relationships WHERE relationship_uuid=?",
            (entry["snapmirror_rel_uuid"],)
        )
        if rel:
            entry.update({
                "sm_state": rel["state"], "sm_healthy": bool(rel["healthy"]),
                "sm_lag_time": rel["lag_time"], "sm_last_transfer": rel["last_transfer_time"],
            })
        else:
            entry.update({"sm_state": "unknown", "sm_healthy": None, "sm_lag_time": "", "sm_last_transfer": ""})
    return entry


def _add_plan_entry():
    err = _require_admin()
    if err: return err
    data = _body()
    plan_id            = (data.get("plan_id") or "").strip()
    source_endpoint_id = (data.get("source_endpoint_id") or "").strip()
    source_svm         = (data.get("source_svm") or "").strip()
    source_volume      = (data.get("source_volume") or "").strip()
    if not plan_id or not source_endpoint_id or not source_svm or not source_volume:
        return {"error": "plan_id, source_endpoint_id, source_svm, source_volume required"}, 400
    db = get_db()
    if not db.query_one("SELECT id FROM netapp_dr_plans WHERE id=?", (plan_id,)):
        return {"error": "DR plan not found"}, 404

    snapmirror_rel_uuid = (data.get("snapmirror_rel_uuid") or "").strip()
    dr_endpoint_id      = (data.get("dr_endpoint_id") or "").strip()
    dr_svm              = (data.get("dr_svm") or "").strip()
    dr_volume           = (data.get("dr_volume") or "").strip()

    if not snapmirror_rel_uuid:
        rel = db.query_one(
            "SELECT relationship_uuid, dest_endpoint_id, dest_svm, dest_volume "
            "FROM netapp_snapmirror_relationships "
            "WHERE source_endpoint_id=? AND source_svm=? AND source_volume=? LIMIT 1",
            (source_endpoint_id, source_svm, source_volume)
        )
        if rel:
            snapmirror_rel_uuid = rel["relationship_uuid"]
            if not dr_endpoint_id: dr_endpoint_id = rel["dest_endpoint_id"] or ""
            if not dr_svm:         dr_svm         = rel["dest_svm"]         or ""
            if not dr_volume:      dr_volume      = rel["dest_volume"]      or ""

    max_ord = db.query_one("SELECT MAX(sort_order) as m FROM netapp_dr_plan_entries WHERE plan_id=?", (plan_id,))
    sort_order = (max_ord["m"] or 0) + 1
    eid = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO netapp_dr_plan_entries "
        "(id, plan_id, source_endpoint_id, source_svm, source_volume, "
        "snapmirror_rel_uuid, dr_endpoint_id, dr_svm, dr_volume, "
        "dr_pve_storage_id, dr_pve_host_ids, sort_order, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, plan_id, source_endpoint_id, source_svm, source_volume,
         snapmirror_rel_uuid, dr_endpoint_id, dr_svm, dr_volume,
         data.get("dr_pve_storage_id", ""),
         json.dumps(data.get("dr_pve_host_ids") or []),
         sort_order, _now())
    )
    db.execute("UPDATE netapp_dr_plans SET updated_at=? WHERE id=?", (_now(), plan_id))
    entry = dict(db.query_one("SELECT * FROM netapp_dr_plan_entries WHERE id=?", (eid,)))
    return jsonify(_enrich_entry(entry, db)), 201


def _update_plan_entry():
    err = _require_admin()
    if err: return err
    data = _body()
    plan_id  = (data.get("plan_id") or "").strip()
    entry_id = (data.get("entry_id") or "").strip()
    db = get_db()
    if not db.query_one("SELECT id FROM netapp_dr_plan_entries WHERE id=? AND plan_id=?", (entry_id, plan_id)):
        return {"error": "Entry not found"}, 404
    allowed = {"dr_endpoint_id", "dr_svm", "dr_volume", "dr_pve_storage_id", "dr_pve_host_ids", "snapmirror_rel_uuid"}
    updates, params = [], []
    for k in allowed:
        if k in data:
            val = json.dumps(data[k]) if k == "dr_pve_host_ids" else data[k]
            updates.append(f"{k}=?"); params.append(val)
    if not updates:
        return {"error": "No fields to update"}, 400
    params.append(entry_id)
    db.execute(f"UPDATE netapp_dr_plan_entries SET {', '.join(updates)} WHERE id=?", params)
    db.execute("UPDATE netapp_dr_plans SET updated_at=? WHERE id=?", (_now(), plan_id))
    entry = dict(db.query_one("SELECT * FROM netapp_dr_plan_entries WHERE id=?", (entry_id,)))
    return jsonify(_enrich_entry(entry, db))


def _delete_plan_entry():
    err = _require_admin()
    if err: return err
    data = _body()
    plan_id  = (data.get("plan_id") or "").strip()
    entry_id = (data.get("entry_id") or "").strip()
    db = get_db()
    if not db.query_one("SELECT id FROM netapp_dr_plan_entries WHERE id=? AND plan_id=?", (entry_id, plan_id)):
        return {"error": "Entry not found"}, 404
    db.execute("DELETE FROM netapp_dr_plan_entries WHERE id=?", (entry_id,))
    db.execute("UPDATE netapp_dr_plans SET updated_at=? WHERE id=?", (_now(), plan_id))
    return jsonify({"message": "Entry removed"})


def _auto_detect_entries():
    err = _require_admin()
    if err: return err
    data = _body()
    plan_id = (data.get("plan_id") or "").strip()
    db = get_db()
    if not plan_id or not db.query_one("SELECT id FROM netapp_dr_plans WHERE id=?", (plan_id,)):
        return {"error": "DR plan not found"}, 404
    rels = db.query("SELECT * FROM netapp_snapmirror_relationships") or []
    added = 0
    skipped = 0
    for rel in [dict(r) for r in rels]:
        existing = db.query_one(
            "SELECT id FROM netapp_dr_plan_entries WHERE plan_id=? AND source_svm=? AND source_volume=?",
            (plan_id, rel["source_svm"], rel["source_volume"])
        )
        if existing:
            skipped += 1; continue
        max_ord = db.query_one("SELECT MAX(sort_order) as m FROM netapp_dr_plan_entries WHERE plan_id=?", (plan_id,))
        sort_order = (max_ord["m"] or 0) + 1
        eid = str(uuid.uuid4())[:8]
        primary_storage_id = _lookup_primary_storage_id(
            db, rel["source_endpoint_id"], rel["source_svm"], rel["source_volume"]
        )
        db.execute(
            "INSERT INTO netapp_dr_plan_entries "
            "(id, plan_id, source_endpoint_id, source_svm, source_volume, "
            "snapmirror_rel_uuid, dr_endpoint_id, dr_svm, dr_volume, "
            "dr_pve_storage_id, dr_pve_host_ids, sort_order, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, plan_id,
             rel["source_endpoint_id"], rel["source_svm"], rel["source_volume"],
             rel["relationship_uuid"],
             rel.get("dest_endpoint_id", ""), rel["dest_svm"], rel["dest_volume"],
             primary_storage_id, "[]", sort_order, _now())
        )
        added += 1
    db.execute("UPDATE netapp_dr_plans SET updated_at=? WHERE id=?", (_now(), plan_id))
    return jsonify({"added": added, "skipped": skipped})


# ── VM Groups ─────────────────────────────────────────────────────────────────

def _create_vm_group():
    err = _require_admin()
    if err: return err
    data = _body()
    plan_id    = (data.get("plan_id") or "").strip()
    name       = (data.get("name") or "").strip()
    if not plan_id or not name:
        return {"error": "plan_id and name are required"}, 400
    db = get_db()
    if not db.query_one("SELECT id FROM netapp_dr_plans WHERE id=?", (plan_id,)):
        return {"error": "DR plan not found"}, 404
    max_ord = db.query_one("SELECT MAX(sort_order) as m FROM netapp_dr_vm_groups WHERE plan_id=?", (plan_id,))
    sort_order = (max_ord["m"] or 0) + 1
    gid = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO netapp_dr_vm_groups "
        "(id, plan_id, name, group_type, sort_order, start_mode, startup_delay_sec, max_parallel, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (gid, plan_id, name, "standard", sort_order,
         data.get("start_mode", "auto"),
         int(data.get("startup_delay_sec", 30)),
         int(data.get("max_parallel", 1)),
         _now())
    )
    return jsonify({"id": gid, "message": "VM group created"}), 201


def _update_vm_group():
    err = _require_admin()
    if err: return err
    data = _body()
    group_id = (data.get("id") or "").strip()
    db = get_db()
    grp = db.query_one("SELECT * FROM netapp_dr_vm_groups WHERE id=?", (group_id,))
    if not grp:
        return {"error": "VM group not found"}, 404
    updates, params = [], []
    for k in ("name", "start_mode", "startup_delay_sec", "max_parallel"):
        if k in data:
            updates.append(f"{k}=?"); params.append(data[k])
    if not updates:
        return {"error": "No fields to update"}, 400
    params.append(group_id)
    db.execute(f"UPDATE netapp_dr_vm_groups SET {', '.join(updates)} WHERE id=?", params)
    return jsonify({"message": "VM group updated"})


def _delete_vm_group():
    err = _require_admin()
    if err: return err
    data = _body()
    group_id = (data.get("id") or "").strip()
    db = get_db()
    grp = db.query_one("SELECT * FROM netapp_dr_vm_groups WHERE id=?", (group_id,))
    if not grp:
        return {"error": "VM group not found"}, 404
    if grp["group_type"] == "core":
        return {"error": "Cannot delete Core group"}, 409
    db.execute("DELETE FROM netapp_dr_vm_groups WHERE id=?", (group_id,))
    return jsonify({"message": "VM group deleted"})


def _reorder_vm_groups():
    err = _require_admin()
    if err: return err
    data = _body()
    order = data.get("order") or []
    db = get_db()
    for i, gid in enumerate(order):
        db.execute("UPDATE netapp_dr_vm_groups SET sort_order=? WHERE id=?", (i, gid))
    return jsonify({"message": "Groups reordered"})


# ── VM Assignments ────────────────────────────────────────────────────────────

def _add_vm_assignment():
    err = _require_admin()
    if err: return err
    data = _body()
    group_id = (data.get("group_id") or "").strip()
    vmid     = data.get("vmid")
    if not group_id or vmid is None:
        return {"error": "group_id and vmid are required"}, 400
    db = get_db()
    if not db.query_one("SELECT id FROM netapp_dr_vm_groups WHERE id=?", (group_id,)):
        return {"error": "VM group not found"}, 404
    max_ord = db.query_one("SELECT MAX(start_order) as m FROM netapp_dr_vm_assignments WHERE group_id=?", (group_id,))
    start_order = (max_ord["m"] or 0) + 1
    aid = str(uuid.uuid4())[:8]
    db.execute(
        "INSERT INTO netapp_dr_vm_assignments (id, group_id, vmid, vm_name, target_node, start_order, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (aid, group_id, int(vmid), data.get("vm_name", ""),
         data.get("target_node", ""), start_order, _now())
    )
    return jsonify({"id": aid, "message": "VM added to group"}), 201


def _remove_vm_assignment():
    err = _require_admin()
    if err: return err
    data = _body()
    assignment_id = (data.get("id") or "").strip()
    db = get_db()
    if not db.query_one("SELECT id FROM netapp_dr_vm_assignments WHERE id=?", (assignment_id,)):
        return {"error": "VM assignment not found"}, 404
    db.execute("DELETE FROM netapp_dr_vm_assignments WHERE id=?", (assignment_id,))
    return jsonify({"message": "VM removed from group"})


def _update_vm_assignment():
    err = _require_admin()
    if err: return err
    data = _body()
    assignment_id = (data.get("id") or "").strip()
    db = get_db()
    asn = db.query_one("SELECT * FROM netapp_dr_vm_assignments WHERE id=?", (assignment_id,))
    if not asn:
        return {"error": "VM assignment not found"}, 404
    updates, params = [], []
    for k in ("vm_name", "target_node", "start_order"):
        if k in data:
            updates.append(f"{k}=?"); params.append(data[k])
    # Move to different group
    if "group_id" in data:
        new_group = data["group_id"]
        if not db.query_one("SELECT id FROM netapp_dr_vm_groups WHERE id=?", (new_group,)):
            return {"error": "Target VM group not found"}, 404
        updates.append("group_id=?"); params.append(new_group)
    if not updates:
        return {"error": "No fields to update"}, 400
    params.append(assignment_id)
    db.execute(f"UPDATE netapp_dr_vm_assignments SET {', '.join(updates)} WHERE id=?", params)
    return jsonify({"message": "VM assignment updated"})


# ── Plan Status + Precheck ────────────────────────────────────────────────────

def _plan_status():
    plan_id = request.args.get("plan_id") or (_body().get("plan_id") or "")
    db = get_db()
    if not plan_id or not db.query_one("SELECT id FROM netapp_dr_plans WHERE id=?", (plan_id,)):
        return {"error": "DR plan not found"}, 404
    entries = db.query("SELECT * FROM netapp_dr_plan_entries WHERE plan_id=?", (plan_id,)) or []
    status_list = []
    overall_healthy = True
    for e in entries:
        item = {
            "entry_id": e["id"], "source_volume": e["source_volume"], "dr_volume": e["dr_volume"],
            "sm_state": "", "sm_healthy": None, "sm_lag_time": "", "sm_last_transfer": "",
        }
        if e["snapmirror_rel_uuid"]:
            rel = db.query_one(
                "SELECT state, healthy, lag_time, last_transfer_time, last_scanned_at "
                "FROM netapp_snapmirror_relationships WHERE relationship_uuid=?",
                (e["snapmirror_rel_uuid"],)
            )
            if rel:
                item.update({
                    "sm_state": rel["state"], "sm_healthy": bool(rel["healthy"]),
                    "sm_lag_time": rel["lag_time"], "sm_last_transfer": rel["last_transfer_time"],
                    "sm_last_scanned": rel["last_scanned_at"],
                })
                if not rel["healthy"]:
                    overall_healthy = False
            else:
                item["sm_state"] = "not_scanned"; overall_healthy = False
        else:
            item["sm_state"] = "no_relationship"; overall_healthy = False
        status_list.append(item)
    plan = db.query_one("SELECT state, last_test_at FROM netapp_dr_plans WHERE id=?", (plan_id,))
    return jsonify({
        "plan_id": plan_id,
        "plan_state": plan["state"] if plan else "",
        "overall_healthy": overall_healthy,
        "entries": status_list,
        "last_test_at": plan["last_test_at"] if plan else "",
    })


def _failover_precheck():
    plan_id = request.args.get("plan_id") or (_body().get("plan_id") or "")
    db = get_db()
    plan = db.query_one("SELECT * FROM netapp_dr_plans WHERE id=?", (plan_id,))
    if not plan:
        return {"error": "DR plan not found"}, 404
    checks = []

    def _chk(name, status, msg):
        checks.append({"name": name, "status": status, "message": msg})

    entries = db.query("SELECT * FROM netapp_dr_plan_entries WHERE plan_id=?", (plan_id,)) or []
    _chk("Plan entries", "ok" if entries else "error", f"{len(entries)} datastore(s) in plan")

    missing_storage = [e["source_volume"] for e in entries if not e.get("dr_pve_storage_id")]
    _chk("Storage IDs", "error" if missing_storage else "ok",
         f"Missing: {', '.join(missing_storage)}" if missing_storage else "All entries have storage IDs")

    missing_hosts = [e["source_volume"] for e in entries if not _json_field(e.get("dr_pve_host_ids"))]
    _chk("DR PVE hosts", "error" if missing_hosts else "ok",
         f"No host: {', '.join(missing_hosts)}" if missing_hosts else "All entries have DR host(s)")

    missing_rel = [e["source_volume"] for e in entries if not e.get("snapmirror_rel_uuid")]
    _chk("SnapMirror links", "error" if missing_rel else "ok",
         f"No relationship: {', '.join(missing_rel)}" if missing_rel else "All entries linked")

    unhealthy = [e["source_volume"] for e in entries if e.get("snapmirror_rel_uuid") and
                 (lambda r: r and not r["healthy"])(db.query_one(
                     "SELECT healthy FROM netapp_snapmirror_relationships WHERE relationship_uuid=?",
                     (e["snapmirror_rel_uuid"],)))]
    _chk("SnapMirror health", "warn" if unhealthy else "ok",
         f"Unhealthy: {', '.join(unhealthy)}" if unhealthy else "All relationships healthy")

    vm_groups = db.query(
        "SELECT g.name, g.group_type, (SELECT COUNT(*) FROM netapp_dr_vm_assignments a WHERE a.group_id=g.id) as vm_count "
        "FROM netapp_dr_vm_groups g WHERE g.plan_id=? ORDER BY g.sort_order", (plan_id,)
    ) or []
    total_vms = sum(g["vm_count"] for g in vm_groups)
    core = next((g for g in vm_groups if g["group_type"] == "core"), None)
    core_vms = core["vm_count"] if core else 0
    _chk("VM groups", "warn" if not vm_groups or core_vms == 0 else "ok",
         f"{len(vm_groups)} group(s), {total_vms} VM(s)" if vm_groups
         else "No VM groups — storage will be mounted, VMs must be started manually")

    overall = all(c["status"] in ("ok", "warn") for c in checks)
    return jsonify({"ok": overall, "checks": checks})


# ── Failover ──────────────────────────────────────────────────────────────────

def _start_failover():
    err = _require_admin()
    if err: return err
    data = _body()
    plan_id      = (data.get("plan_id") or "").strip()
    failover_type = (data.get("failover_type") or "planned").strip()
    if failover_type not in ("planned", "emergency"):
        return {"error": "failover_type must be 'planned' or 'emergency'"}, 400
    db = get_db()
    plan = db.query_one("SELECT * FROM netapp_dr_plans WHERE id=?", (plan_id,))
    if not plan:
        return {"error": "DR plan not found"}, 404
    if plan["state"] in ("failover_running", "failback_running"):
        return {"error": f"Plan is already in state '{plan['state']}'"}, 409
    entry_ids = data.get("entry_ids") or []
    snap_map  = data.get("snap_map") or {}
    username = request.session.get("user", "admin")
    job_id = _dr_start_job("dr_" + failover_type + "_failover", username, plan_id)
    db.execute("UPDATE netapp_dr_plans SET state='failover_running', updated_at=? WHERE id=?", (_now(), plan_id))
    threading.Thread(
        target=_execute_failover,
        args=(job_id, plan_id, failover_type, entry_ids, snap_map),
        daemon=True
    ).start()
    return jsonify({"job_id": job_id, "message": "Failover started"}), 202


def _ensure_provisioned_ds(db, entry, storage_id, pve_host_ids):
    """Finds or creates a netapp_provisioned_datastores row for a DR entry's
    target storage, so the failed-over datastore becomes a normally-tracked,
    visible datastore afterward (same as any other managed datastore) instead
    of only living inside the DR plan's own bookkeeping."""
    row = db.query_one("SELECT id FROM netapp_provisioned_datastores WHERE pve_storage_id=?", (storage_id,))
    if row:
        return row["id"]
    ds_id = str(uuid.uuid4())
    now = _now()
    db.execute(
        "INSERT INTO netapp_provisioned_datastores "
        "(id, name, endpoint_id, svm_name, volume_uuid, volume_name, protocol, "
        "lun_uuid, lun_path, igroup_uuid, igroup_name, ns_uuid, subsystem_uuid, subsystem_name, "
        "vg_name, lvm_type, lvm_pool_name, nfs_junction_path, pve_storage_id, pve_host_ids, "
        "size_bytes, status, error_message, imported_from, created_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ds_id, storage_id, entry.get("dr_endpoint_id", ""), entry.get("dr_svm", ""),
         "", entry.get("dr_volume", ""), "nfs",
         "", "", "", "", "", "", "",
         "", "linear", "", "",
         storage_id, json.dumps(pve_host_ids),
         0, "provisioning", "", "dr_failover",
         "system", now, now),
    )
    return ds_id


def _execute_failover(job_id, plan_id, failover_type, entry_ids=None, snap_map=None):
    """Runs the failover for a plan's entries: break SnapMirror, bind the DR
    volume onto the target PVE host(s), restore VM configs, start VM groups.

    Storage bind + config restore reuse the same core.recovery_engine
    primitives as the Bind Wizard / Recover-VMs flow (Datastores tab / DR tab)
    instead of duplicating pvesm/SSH commands inline — this also means the
    failed-over datastore shows up as a normal managed datastore afterward.
    NFS only for now (matches what this failover path has always supported —
    iSCSI/NVMe would need Plan Entries to collect target-specific fields
    like IQN/igroup that don't exist in the UI yet).
    """
    import time
    from ..core._helpers import build_ontap_client, build_pve_client, get_ssh_creds, get_endpoint
    from ..core.recovery_engine import _bind_nfs, restore_vm_configs, start_vms, list_nfs_manifests, read_nfs_manifest

    log_lines = []

    def _log(msg):
        log_lines.append({"ts": _now(), "msg": msg})
        db = get_db()
        db.execute("UPDATE netapp_jobs SET log_json=? WHERE id=?", (json.dumps(log_lines), job_id))

    class _LogAdapter:
        def log(self, msg):
            _log(msg)
    jlog = _LogAdapter()

    def _finish(state):
        plan_state = "failed_over" if state == "success" else "standby"
        status = "done" if state == "success" else "failed"
        db = get_db()
        db.execute(
            "UPDATE netapp_jobs SET status=?, completed_at=?, log_json=? WHERE id=?",
            (status, _now(), json.dumps(log_lines), job_id)
        )
        db.execute("UPDATE netapp_dr_plans SET state=?, last_failover_at=?, updated_at=? WHERE id=?",
                   (plan_state, _now(), _now(), plan_id))

    try:
        db = get_db()
        all_entries = db.query(
            "SELECT * FROM netapp_dr_plan_entries WHERE plan_id=? ORDER BY sort_order", (plan_id,)
        ) or []
        if not all_entries:
            _log("[ERR] No plan entries found"); _finish("failed"); return

        entries = [dict(e) for e in all_entries if not entry_ids or dict(e)["id"] in entry_ids]
        skipped_count = len(all_entries) - len(entries)
        if skipped_count:
            _log(f"[INFO] {skipped_count} datastore(s) skipped (not selected)")

        _log(f"[INFO] Starting {failover_type.upper()} FAILOVER — {len(entries)} datastore(s)")

        for entry in entries:
            dr_ep_id   = entry.get("dr_endpoint_id", "")
            dr_svm     = entry.get("dr_svm", "")
            dr_volume  = entry.get("dr_volume", "")
            rel_uuid   = entry.get("snapmirror_rel_uuid", "")
            storage_id = entry.get("dr_pve_storage_id", "")
            pve_host_ids = _json_field(entry.get("dr_pve_host_ids")) or []

            _log(f"[INFO] ── {entry['source_volume']} → {dr_volume} ──")
            if not rel_uuid:
                _log(f"[WARN] No SnapMirror relationship — skipping"); continue
            if not storage_id:
                _log(f"[WARN] No DR storage ID — skipping"); continue
            if not pve_host_ids:
                _log(f"[WARN] No DR PVE host — skipping"); continue

            try:
                dr_ep = get_endpoint(db, dr_ep_id)
                dr_client = build_ontap_client(dr_ep)
            except Exception as exc:
                _log(f"[ERR] Cannot connect to DR ONTAP: {exc}"); _finish("failed"); return

            if failover_type == "planned":
                _log("[INFO] Triggering final SnapMirror update…")
                try:
                    dr_client.trigger_snapmirror_transfer(rel_uuid)
                    time.sleep(5)
                    _log("[INFO] Final update triggered")
                except Exception as exc:
                    _log(f"[WARN] Final update failed (continuing): {exc}")

            try:
                vol = dr_client.get_volume_by_name(dr_svm, dr_volume)
                vol_uuid = vol.get("uuid", "")
            except Exception as exc:
                _log(f"[ERR] Volume lookup failed: {exc}"); _finish("failed"); return

            ds_id = _ensure_provisioned_ds(db, entry, storage_id, pve_host_ids)
            bind_params = {
                "endpoint_id": dr_ep_id, "svm_name": dr_svm,
                "volume_uuid": vol_uuid, "volume_name": dr_volume,
                "pve_storage_id": storage_id, "pve_host_ids": pve_host_ids,
                "snapmirror_break": True,
                "snapmirror_relationship_uuid": rel_uuid,
            }
            try:
                _bind_nfs(ds_id, bind_params, db, jlog)
            except Exception as exc:
                _log(f"[ERR] Storage bind failed: {exc}"); _finish("failed"); return

            try:
                mrow = db.query_one(
                    "SELECT nfs_mount_path FROM netapp_volume_mapping WHERE pve_storage_id=? AND pve_cluster_id=?",
                    (storage_id, pve_host_ids[0]),
                )
                mount_point = dict(mrow or {}).get("nfs_mount_path") or f"/mnt/pve/{storage_id}"
                pve = build_pve_client(db, pve_host_ids[0])
                su, sp, sk = get_ssh_creds(pve)

                chosen_snap = (snap_map or {}).get(entry["id"], "")
                if not chosen_snap:
                    manifests = list_nfs_manifests(pve.host, su, sp, sk, mount_point)
                    chosen_snap = manifests[0]["snap_name"] if manifests else ""

                if not chosen_snap:
                    _log("[INFO] No snapmanifest found — VM configs must be registered manually")
                else:
                    _log(f"[INFO] Using snapshot: {chosen_snap}")
                    manifest = read_nfs_manifest(pve.host, su, sp, sk, mount_point, chosen_snap)
                    vms_in_plan = db.query(
                        "SELECT a.vmid FROM netapp_dr_vm_assignments a "
                        "JOIN netapp_dr_vm_groups g ON g.id=a.group_id WHERE g.plan_id=?", (plan_id,)
                    ) or []
                    vmids_to_restore = {int(v["vmid"]) for v in vms_in_plan} if vms_in_plan else None
                    restored = restore_vm_configs(
                        manifest, pve_host_ids, 0, vmids_to_restore,
                        storage_id, storage_id, db, jlog, protocol="nfs",
                    )
                    _log(f"[INFO] {restored} VM config(s) restored from snapmanifest")
            except Exception as exc:
                _log(f"[WARN] VM config restore failed: {exc}")

        vm_groups = db.query(
            "SELECT * FROM netapp_dr_vm_groups WHERE plan_id=? ORDER BY sort_order", (plan_id,)
        ) or []
        if not vm_groups:
            _log("[INFO] No VM groups — storage is mounted and ready")
        else:
            _log(f"[INFO] Starting {len(vm_groups)} VM group(s)…")
            for group in vm_groups:
                group = dict(group)
                assignments = db.query(
                    "SELECT * FROM netapp_dr_vm_assignments WHERE group_id=? ORDER BY start_order",
                    (group["id"],)
                ) or []
                mode_label = "AUTO" if group["start_mode"] == "auto" else "MANUAL"
                _log(f"[INFO] Group '{group['name']}' [{group['group_type'].upper()} / {mode_label}] — {len(assignments)} VM(s)")
                if group["start_mode"] == "manual":
                    _log(f"[INFO]   → Skipped (manual — start via UI after confirming primary is down)")
                    continue

                first_host = next(
                    (h for e in entries for h in _json_field(e.get("dr_pve_host_ids"))), None
                )
                if not first_host:
                    _log(f"[WARN]   → No DR PVE host found — skipping group"); continue

                vm_list = [{"vmid": dict(a)["vmid"], "vm_name": dict(a).get("vm_name") or str(dict(a)["vmid"])}
                           for a in assignments]
                start_vms(first_host, vm_list, db, jlog, max_parallel=group.get("max_parallel", 1))

                delay = group.get("startup_delay_sec", 30)
                if delay > 0 and group != vm_groups[-1]:
                    _log(f"[INFO]   Waiting {delay}s before next group…")
                    time.sleep(delay)

        _log("[INFO] ✅ Failover complete")
        _finish("success")

    except Exception as exc:
        _log(f"[ERR] Unexpected error: {exc}")
        _finish("failed")


def _list_dr_snapshots():
    plan_id  = request.args.get("plan_id") or ""
    entry_id = request.args.get("entry_id") or ""
    db = get_db()
    entry = db.query_one(
        "SELECT * FROM netapp_dr_plan_entries WHERE id=? AND plan_id=?", (entry_id, plan_id)
    )
    if not entry:
        return {"error": "Entry not found"}, 404
    entry = dict(entry)
    try:
        from ..core._helpers import get_endpoint, build_ontap_client
        dr_ep = get_endpoint(db, entry["dr_endpoint_id"])
        client = build_ontap_client(dr_ep)
        vol = client.get_volume_by_name(entry["dr_svm"], entry["dr_volume"])
        vol_uuid = vol.get("uuid", "")
        snaps = client.list_snapshots(vol_uuid)
        result = [{"name": s.get("name", ""), "created": s.get("create_time", "")} for s in (snaps or [])]
        result.sort(key=lambda s: s["created"], reverse=True)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _get_failover_jobs():
    plan_id = request.args.get("plan_id") or (_body().get("plan_id") or "")
    db = get_db()
    if not plan_id:
        return {"error": "plan_id required"}, 400
    jobs = db.query(
        "SELECT id, job_type, status, log_json, created_at, completed_at, created_by "
        "FROM netapp_jobs WHERE snapshot_id=? AND job_type LIKE 'dr_%failover%' "
        "ORDER BY created_at DESC LIMIT 5",
        (plan_id,)
    ) or []
    result = []
    for j in jobs:
        row = dict(j)
        row["state"] = "success" if row.get("status") == "done" else row.get("status", "")
        try:
            row["log"] = json.loads(row.pop("log_json") or "[]")
        except Exception:
            row["log"] = []
        result.append(row)
    return jsonify(result)


# ── Route Registration ────────────────────────────────────────────────────────

def register_routes():
    rpr = register_plugin_route

    # DR Plans
    rpr(PLUGIN_ID, "dr/plans",               _list_dr_plans)
    rpr(PLUGIN_ID, "dr/plans/create",        _create_dr_plan)
    rpr(PLUGIN_ID, "dr/plans/detail",        _get_dr_plan_detail)
    rpr(PLUGIN_ID, "dr/plans/update",        _update_dr_plan)
    rpr(PLUGIN_ID, "dr/plans/delete",        _delete_dr_plan)

    # Plan Entries
    rpr(PLUGIN_ID, "dr/plans/entries/add",    _add_plan_entry)
    rpr(PLUGIN_ID, "dr/plans/entries/update", _update_plan_entry)
    rpr(PLUGIN_ID, "dr/plans/entries/delete", _delete_plan_entry)
    rpr(PLUGIN_ID, "dr/plans/auto-detect",    _auto_detect_entries)

    # VM Groups
    rpr(PLUGIN_ID, "dr/plans/groups/create",  _create_vm_group)
    rpr(PLUGIN_ID, "dr/plans/groups/update",  _update_vm_group)
    rpr(PLUGIN_ID, "dr/plans/groups/delete",  _delete_vm_group)
    rpr(PLUGIN_ID, "dr/plans/groups/reorder", _reorder_vm_groups)

    # VM Assignments
    rpr(PLUGIN_ID, "dr/plans/groups/vms/add",    _add_vm_assignment)
    rpr(PLUGIN_ID, "dr/plans/groups/vms/delete", _remove_vm_assignment)
    rpr(PLUGIN_ID, "dr/plans/groups/vms/update", _update_vm_assignment)

    # Status + Failover
    rpr(PLUGIN_ID, "dr/plans/status",         _plan_status)
    rpr(PLUGIN_ID, "dr/plans/precheck",        _failover_precheck)
    rpr(PLUGIN_ID, "dr/plans/failover",        _start_failover)
    rpr(PLUGIN_ID, "dr/plans/failover-jobs",   _get_failover_jobs)
    rpr(PLUGIN_ID, "dr/plans/snapshots",       _list_dr_snapshots)
