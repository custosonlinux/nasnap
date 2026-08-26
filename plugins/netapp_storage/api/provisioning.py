"""
NetApp ONTAP Provisioning API

End-to-end provisioning of iSCSI, NVMe-oF and NFS datastores:
  create / resize / add-host / remove-host / remove

All mutating operations run as background jobs (same pattern as restore/clone).
"""

import ipaddress
import json
import shlex
import threading
import time
import uuid as _uuid
import logging
from datetime import datetime, timezone

from nasnap_core.core.db import get_db

from ..core._helpers import (
    get_endpoint, build_ontap_client, build_pve_client,
    get_ssh_creds, JobLogger, ssh_run, load_plugin_config,
    find_datastore_conflict, validate_safe_name,
)
from ..core.ontap_client import OntapError
from ..core.san_helpers import (
    get_iscsi_initiator_iqn, find_device_by_serial, _iscsi_serial_to_mapper,
    get_nvme_host_nqn, nvme_connect_all, nvme_connect_to_subsystem,
    nvme_disconnect_by_vg, nvme_disconnect_by_subsystem_name,
    nvme_list_devices, find_new_nvme_device, find_nvme_device_for_subsystem_nqn,
    ensure_nvme_discovery_entries, snapmanifest_initialize,
)

log = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ontap_safe_name(name: str) -> str:
    """Replace characters not allowed in ONTAP volume/subsystem names with underscores."""
    import re
    return re.sub(r'[^a-zA-Z0-9_]', '_', name)


from ..core._helpers import PLUGIN_ID  # noqa: F401


def _require_admin():
    from flask import request
    if request.session.get("role") != "admin":
        return {"error": "Admin access required"}, 403
    return None


def _ds_to_dict(row):
    d = dict(row)
    try:
        d["pve_host_ids"] = json.loads(d.get("pve_host_ids") or "[]")
    except Exception:
        d["pve_host_ids"] = []
    return d


def _backfill_size_bytes(db, d):
    """Lazily fetches+persists size_bytes from ONTAP for an active datastore
    that doesn't have one yet (e.g. a Mount Existing bind whose post-bind size
    fetch failed). Mutates d in place. Called from every list endpoint that
    renders a datastore so it self-heals on the next load, wherever that is."""
    if d.get("size_bytes") or not d.get("volume_uuid") or d.get("status") != "active":
        return
    try:
        from ..core._helpers import get_endpoint, build_ontap_client
        endpoint   = get_endpoint(db, d["endpoint_id"])
        tmp_client = build_ontap_client(endpoint)
        vol_info   = tmp_client.get_volume(d["volume_uuid"])
        sz = (vol_info.get("space") or {}).get("size", 0)
        if sz:
            d["size_bytes"] = sz
            db.execute(
                "UPDATE netapp_provisioned_datastores SET size_bytes=? WHERE id=?",
                (sz, d["id"]),
            )
    except Exception as e:
        log.warning(f"[netapp_storage] size backfill failed for datastore {d.get('id')}: {e}")


# ── Route handlers ────────────────────────────────────────────────────────────

def _prov_datastores():
    from flask import request
    if request.method == "GET":
        err = _require_admin()
        if err:
            return err
        db   = get_db()
        rows = db.query(
            "SELECT * FROM netapp_provisioned_datastores ORDER BY created_at DESC")
        result = []
        for r in rows:
            d = _ds_to_dict(r)
            # Backfill vg_name from volume_mapping for auto-detected binds
            if not d.get("vg_name") and d.get("protocol") in ("iscsi", "nvme"):
                vm_row = db.query_one(
                    "SELECT lvm_vg_name FROM netapp_volume_mapping "
                    "WHERE pve_storage_id=? LIMIT 1",
                    (d.get("pve_storage_id", ""),),
                )
                vg = dict(vm_row or {}).get("lvm_vg_name", "")
                if vg:
                    d["vg_name"] = vg
                    db.execute(
                        "UPDATE netapp_provisioned_datastores SET vg_name=? WHERE id=?",
                        (vg, d["id"]),
                    )
            _backfill_size_bytes(db, d)
            result.append(d)
        return {"datastores": result}

    # POST — create
    err = _require_admin()
    if err:
        return err
    body = request.get_json(force=True) or {}
    required = ["name", "endpoint_id", "protocol", "pve_host_ids"]
    for f in required:
        if not body.get(f):
            return {"error": f"Required field missing: {f}"}, 400
    protocol = body["protocol"]
    if protocol not in ("iscsi", "nvme", "nfs"):
        return {"error": f"Unknown protocol: {protocol}"}, 400

    db = get_db()
    try:
        ds_name = validate_safe_name(body["name"], "Datastore name")
    except ValueError as exc:
        return {"error": str(exc)}, 400
    conflict = find_datastore_conflict(db, ds_name, body.get("pve_storage_id", ""))
    if conflict:
        return {
            "error": f"A datastore named '{conflict['name']}' (storage id "
                     f"'{conflict['pve_storage_id']}') already exists — choose a "
                     f"different name to avoid mounting two volumes on the same "
                     f"Proxmox mountpoint.",
        }, 409
    ds_id    = str(_uuid.uuid4())
    username = request.session.get("user", "unknown")
    now      = _now()
    db.execute(
        """INSERT INTO netapp_provisioned_datastores
           (id, name, endpoint_id, svm_name, volume_uuid, volume_name,
            protocol, lun_uuid, lun_path, igroup_uuid, igroup_name,
            ns_uuid, subsystem_uuid, subsystem_name,
            vg_name, lvm_type, lvm_pool_name,
            nfs_junction_path, pve_storage_id,
            pve_host_ids, size_bytes, status, error_message,
            created_by, created_at, updated_at,
            pve_content, nfs_nconnect)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ds_id, ds_name, body["endpoint_id"],
            body.get("svm_name", ""), body.get("volume_uuid", ""),
            body.get("volume_name", ""), protocol,
            body.get("lun_uuid", ""), body.get("lun_path", ""),
            body.get("igroup_uuid", ""), body.get("igroup_name", ""),
            body.get("ns_uuid", ""), body.get("subsystem_uuid", ""),
            body.get("subsystem_name", ""),
            body.get("vg_name", ""), body.get("lvm_type", "linear"),
            body.get("lvm_pool_name", ""), body.get("nfs_junction_path", ""),
            body.get("pve_storage_id", ""),
            json.dumps(body["pve_host_ids"]),
            int(body.get("size_bytes", 0)),
            "provisioning", "", username, now, now,
            body.get("pve_content", "images,rootdir") or "images,rootdir",
            int(body.get("nfs_nconnect", 0) or 0),
        ),
    )
    job_id = _start_job(db, "provision", ds_id, username)
    _run_provision_job_async(job_id, ds_id, body, username)
    return {"id": ds_id, "job_id": job_id}, 202


def _check_vms_on_storage(db, pve_host_id, vg_name):
    """Returns list of LV names in vg_name that look like VM disks (excludes system LVs)."""
    SYSTEM_LVS = {"netapp_snapmanifest", "data", "data_tmeta", "data_tdata"}
    try:
        pve = build_pve_client(db, pve_host_id)
        su, sp, sk = get_ssh_creds(pve)
        sh = pve.host
        out = ssh_run(
            sh, su, sp,
            f"lvs --noheadings -o lv_name {shlex.quote(vg_name)} 2>/dev/null",
            capture=True, key_material=sk, timeout=15)
        return [l.strip() for l in out.strip().splitlines()
                if l.strip() and l.strip() not in SYSTEM_LVS]
    except Exception:
        return []


def _prov_remove():
    err = _require_admin()
    if err:
        return err
    from flask import request
    body      = request.get_json(force=True) or {}
    ds_id     = body.get("id")
    if not ds_id:
        return {"error": "id required"}, 400
    delete_ontap = bool(body.get("delete_ontap_objects", False))
    force        = bool(body.get("force", False))
    snapmirror_action = body.get("snapmirror_action", "keep")

    db  = get_db()
    row = db.query_one(
        "SELECT * FROM netapp_provisioned_datastores WHERE id=?", (ds_id,))
    if not row:
        return {"error": "Datastore not found"}, 404
    ds = dict(row)

    # Safety check: abort if VMs still have disks in the VG
    if not force:
        vg_name      = ds.get("vg_name", "")
        pve_host_ids = json.loads(ds.get("pve_host_ids") or "[]")
        if pve_host_ids and vg_name:
            try:
                in_use = _check_vms_on_storage(db, pve_host_ids[0], vg_name)
                if in_use:
                    return {
                        "error": "Storage still in use — remove these VM disks first",
                        "volumes": in_use,
                    }, 409
            except Exception:
                pass  # SSH unreachable — let the job handle it

    username = request.session.get("user", "unknown")
    db.execute(
        "UPDATE netapp_provisioned_datastores SET status='removing', updated_at=? WHERE id=?",
        (_now(), ds_id),
    )
    job_id = _start_job(db, "provision_remove", ds_id, username)
    _run_remove_job_async(job_id, ds_id, delete_ontap, snapmirror_action, username)
    return {"job_id": job_id}, 202


def _prov_resize():
    err = _require_admin()
    if err:
        return err
    from flask import request
    body     = request.get_json(force=True) or {}
    ds_id    = body.get("id")
    new_size = body.get("size_bytes")
    if not ds_id or not new_size:
        return {"error": "id and size_bytes required"}, 400

    db  = get_db()
    row = db.query_one(
        "SELECT * FROM netapp_provisioned_datastores WHERE id=?", (ds_id,))
    if not row:
        return {"error": "Datastore not found"}, 404

    username = request.session.get("user", "unknown")
    job_id   = _start_job(db, "provision_resize", ds_id, username)
    _run_resize_job_async(job_id, ds_id, int(new_size), username)
    return {"job_id": job_id}, 202


def _prov_add_host():
    err = _require_admin()
    if err:
        return err
    from flask import request
    body    = request.get_json(force=True) or {}
    ds_id   = body.get("id")
    host_id = body.get("pve_host_id")
    if not ds_id or not host_id:
        return {"error": "id and pve_host_id required"}, 400

    db  = get_db()
    row = db.query_one(
        "SELECT * FROM netapp_provisioned_datastores WHERE id=?", (ds_id,))
    if not row:
        return {"error": "Datastore not found"}, 404

    protocol = (row["protocol"] or "").lower()
    host_ids = json.loads(row["pve_host_ids"] or "[]")
    if host_id in host_ids:
        # All protocols are idempotent on re-run:
        #  NFS   — pre-checked export rules, ON CONFLICT UPDATE mapping
        #  iSCSI — IQN pre-checked, login uses || true, pvscan idempotent
        #  NVMe  — NQN pre-checked, connect uses 2>/dev/null;true, discovery idempotent
        # Allow re-run so admins can repair (missing export rule, re-connect, re-activate VG).
        pass

    username = request.session.get("user", "unknown")
    job_id   = _start_job(db, "provision_add_host", ds_id, username)
    _run_add_host_job_async(job_id, ds_id, host_id, username)
    return {"job_id": job_id}, 202


def _prov_remove_host():
    err = _require_admin()
    if err:
        return err
    from flask import request
    body    = request.get_json(force=True) or {}
    ds_id   = body.get("id")
    host_id = body.get("host_id")
    if not ds_id or not host_id:
        return {"error": "id and host_id required"}, 400

    db  = get_db()
    row = db.query_one(
        "SELECT * FROM netapp_provisioned_datastores WHERE id=?", (ds_id,))
    if not row:
        return {"error": "Datastore not found"}, 404

    username = request.session.get("user", "unknown")
    job_id   = _start_job(db, "provision_remove_host", ds_id, username)
    _run_remove_host_job_async(job_id, ds_id, host_id, username)
    return {"job_id": job_id}, 202


def _prov_ontap_resources():
    err = _require_admin()
    if err:
        return err
    from flask import request
    endpoint_id = request.args.get("endpoint_id")
    svm_name    = request.args.get("svm_name", "")
    if not endpoint_id:
        return {"error": "endpoint_id required"}, 400

    db       = get_db()
    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)

    volumes = []
    try:
        # SAN-optimized (ASA) platforms reject NAS fields (e.g. junction path)
        # on this collection call — use the NAS-field-free variant only there,
        # so NFS-capable platforms still get junction_path for existing volumes.
        vols = client.get_volumes_san(svm_name=svm_name) if endpoint.get("san_optimized") else client.get_volumes(svm_name=svm_name)
        for v in vols:
            volumes.append({
                "uuid": v.get("uuid", ""),
                "name": v.get("name", ""),
                "svm":  (v.get("svm") or {}).get("name", ""),
                "junction_path": (v.get("nas") or {}).get("path", ""),
                "snapshot_locking_enabled": bool((v.get("snaplock") or {}).get("snapshot_locking_enabled", False)),
            })
    except Exception as exc:
        log.warning(f"[netapp_storage] prov ontap-resources volumes: {exc}")

    luns = []
    try:
        for lun in client.list_luns(svm_name=svm_name):
            luns.append({
                "uuid":        lun.get("uuid", ""),
                "name":        lun.get("name", ""),
                "volume_uuid": ((lun.get("location") or {}).get("volume") or {}).get("uuid", ""),
                "size_bytes":  (lun.get("space") or {}).get("size", 0),
                "serial":      lun.get("serial_number", ""),
            })
    except Exception as exc:
        log.warning(f"[netapp_storage] prov ontap-resources luns: {exc}")

    igroups = []
    try:
        for ig in client.list_igroups(svm_name=svm_name):
            igroups.append({
                "uuid":       ig.get("uuid", ""),
                "name":       ig.get("name", ""),
                "protocol":   ig.get("protocol", ""),
                "os_type":    ig.get("os_type", ""),
                "initiators": [i.get("name", "") for i in (ig.get("initiators") or [])],
            })
    except Exception as exc:
        log.warning(f"[netapp_storage] prov ontap-resources igroups: {exc}")

    nvme_subsystems = []
    try:
        for sub in client.list_nvme_subsystems(svm_name=svm_name):
            nvme_subsystems.append({
                "uuid":  sub.get("uuid", ""),
                "name":  sub.get("name", ""),
                "hosts": [h.get("nqn", "") for h in (sub.get("hosts") or [])],
            })
    except Exception as exc:
        log.warning(f"[netapp_storage] prov ontap-resources nvme_subsystems: {exc}")

    nvme_namespaces = []
    try:
        for ns in client.list_nvme_namespaces(svm_name=svm_name):
            loc = ns.get("location") or {}
            nvme_namespaces.append({
                "uuid":        ns.get("uuid", ""),
                "name":        ns.get("name", ""),
                "volume_uuid": (loc.get("volume") or {}).get("uuid", ""),
                "size_bytes":  (ns.get("space") or {}).get("size", 0),
            })
    except Exception as exc:
        log.warning(f"[netapp_storage] prov ontap-resources nvme_namespaces: {exc}")

    aggregates = []
    try:
        for ag in client.list_aggregates():
            aggregates.append({
                "name":            ag.get("name", ""),
                "uuid":            ag.get("uuid", ""),
                "available_bytes": ag.get("available_bytes", 0),
                "node":            ag.get("node", ""),
            })
    except Exception as exc:
        log.warning(f"[netapp_storage] prov ontap-resources aggregates: {exc}")

    nfs_lifs = []
    try:
        nfs_lifs = client.list_nfs_lifs(svm_name=svm_name) if svm_name else []
    except Exception as exc:
        log.warning(f"[netapp_storage] prov ontap-resources nfs_lifs: {exc}")

    export_policies = []
    try:
        if svm_name:
            for p in client.list_export_policies(svm_name=svm_name):
                export_policies.append({"id": p.get("id", ""), "name": p.get("name", "")})
    except Exception as exc:
        log.warning(f"[netapp_storage] prov ontap-resources export_policies: {exc}")

    return {
        "volumes": volumes, "luns": luns, "igroups": igroups,
        "nvme_subsystems": nvme_subsystems, "nvme_namespaces": nvme_namespaces,
        "aggregates": aggregates, "nfs_lifs": nfs_lifs, "export_policies": export_policies,
    }


def _prov_pve_hosts():
    err = _require_admin()
    if err:
        return err
    db   = get_db()
    rows = db.query("SELECT id, name, host FROM netapp_pve_hosts ORDER BY name")
    return {"hosts": [dict(r) for r in rows]}


def _prov_svms():
    err = _require_admin()
    if err:
        return err
    from flask import request
    endpoint_id = request.args.get("endpoint_id")
    if not endpoint_id:
        return {"error": "endpoint_id required"}, 400
    db       = get_db()
    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)
    try:
        svms = client.list_svms()
        return {"svms": [{"name": s.get("name",""), "uuid": s.get("uuid","")} for s in svms]}
    except Exception as exc:
        return {"error": str(exc)}, 500


def _prov_nfs_lifs():
    """Lightweight endpoint: returns NFS LIFs for a given endpoint + SVM."""
    err = _require_admin()
    if err:
        return err
    from flask import request
    endpoint_id = request.args.get("endpoint_id")
    svm_name    = request.args.get("svm_name", "")
    if not endpoint_id:
        return {"error": "endpoint_id required"}, 400
    db       = get_db()
    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)
    try:
        lifs = client.list_nfs_lifs(svm_name=svm_name)
        return {"lifs": lifs}
    except Exception as exc:
        return {"error": str(exc)}, 500


def _prov_import():
    """Register an existing (manually created) datastore in the Provisioning tab.

    Reads what it can from the volume mapping, queries ONTAP for missing UUIDs
    (iGroup for iSCSI, subsystem for NVMe), and inserts an active provisioning
    record — no job required because no resources need to be created.
    """
    err = _require_admin()
    if err:
        return err
    from flask import request
    body       = request.get_json(force=True) or {}
    mapping_id = body.get("mapping_id")
    name       = body.get("name", "")
    if not mapping_id:
        return {"error": "mapping_id required"}, 400

    db       = get_db()
    username = request.session.get("user", "unknown")
    now      = _now()

    mapping = db.query_one(
        "SELECT * FROM netapp_volume_mapping WHERE id=?", (mapping_id,))
    if not mapping:
        return {"error": "Mapping not found"}, 404
    mapping = dict(mapping)

    volume_uuid    = mapping.get("volume_uuid", "")
    pve_storage_id = mapping.get("pve_storage_id", "")
    protocol       = mapping.get("storage_protocol", "nfs")

    existing = db.query_one(
        "SELECT id FROM netapp_provisioned_datastores WHERE pve_storage_id=?",
        (pve_storage_id,))
    if existing:
        return {"error": f"'{pve_storage_id}' is already registered in Provisioning"}, 409

    # Collect all PVE host IDs that have this volume mapped
    all_mappings  = db.query(
        "SELECT pve_cluster_id FROM netapp_volume_mapping WHERE volume_uuid=?", (volume_uuid,))
    pve_host_ids  = list({dict(r)["pve_cluster_id"] for r in all_mappings})

    endpoint = get_endpoint(db, mapping["endpoint_id"])
    client   = build_ontap_client(endpoint)
    svm_name = mapping.get("svm_name", "")

    # Volume size
    size_bytes = 0
    try:
        vol_info   = client._get(
            f"storage/volumes/{volume_uuid}",
            params={"fields": "space.size"})
        size_bytes = (vol_info.get("space") or {}).get("size", 0)
    except Exception as exc:
        log.warning(f"[netapp_storage] import: volume size lookup failed: {exc}")

    lun_uuid       = mapping.get("lun_uuid", "")
    lun_path       = mapping.get("lun_path", "")
    igroup_uuid    = ""
    igroup_name    = ""
    ns_uuid        = ""
    subsystem_uuid = ""
    subsystem_name = ""
    vg_name        = mapping.get("lvm_vg_name", "")
    lvm_type       = mapping.get("lvm_type", "linear") or "linear"
    lvm_pool_name  = mapping.get("lvm_pool_name", "")
    nfs_jpath      = mapping.get("junction_path", "")

    if protocol == "iscsi":
        # Resolve iGroup via LUN-map lookup
        if lun_uuid:
            try:
                lun_info   = client.get_lun(lun_uuid)
                size_bytes = (lun_info.get("space") or {}).get("size", size_bytes)
            except Exception:
                pass
            try:
                maps = client.list_lun_maps(lun_uuid=lun_uuid)
                if maps:
                    ig          = maps[0].get("igroup") or {}
                    igroup_uuid = ig.get("uuid", "")
                    igroup_name = ig.get("name", "")
            except Exception as exc:
                log.warning(f"[netapp_storage] import: LUN-map lookup failed: {exc}")

    elif protocol == "nvme":
        # lun_uuid column stores the namespace UUID for NVMe mappings
        ns_uuid  = lun_uuid
        lun_uuid = ""
        lun_path = ""
        if ns_uuid:
            try:
                sub            = client.get_nvme_subsystem_for_namespace(ns_uuid, svm_name)
                subsystem_uuid = sub.get("uuid", "")
                subsystem_name = sub.get("name", "")
            except Exception as exc:
                log.warning(f"[netapp_storage] import: NVMe subsystem lookup failed: {exc}")

    ds_id   = str(_uuid.uuid4())
    ds_name = name or pve_storage_id
    try:
        ds_name = validate_safe_name(ds_name, "Datastore name")
    except ValueError as exc:
        return {"error": str(exc)}, 400
    conflict = find_datastore_conflict(db, ds_name, pve_storage_id)
    if conflict:
        return {
            "error": f"A datastore named '{conflict['name']}' (storage id "
                     f"'{conflict['pve_storage_id']}') is already registered.",
        }, 409

    # ── Detect existing snapmanifest before committing ────────────────────────
    # For SAN: SSH to any known PVE host and check if the LV already exists.
    # This avoids a misleading "not initialized" badge after +Manage.
    snapinfo_initialized = 0
    if protocol in ("iscsi", "nvme") and vg_name and pve_host_ids:
        try:
            pve_row = db.query_one(
                "SELECT * FROM netapp_pve_hosts WHERE id=?", (pve_host_ids[0],))
            if pve_row:
                ph = dict(pve_row)
                ph_host = ph["host"]
                ph_user = (ph.get("username") or "root@pam").split("@")[0]
                ph_pass = db._decrypt(ph.get("password_encrypted") or "")
                from ..core._helpers import ssh_run as _ssh_run
                chk = _ssh_run(
                    ph_host, ph_user, ph_pass,
                    f"lvs {vg_name}/netapp_snapmanifest 2>/dev/null && echo EXISTS || true",
                    capture=True, key_material="",
                )
                if "EXISTS" in chk:
                    snapinfo_initialized = 1
                    log.info(
                        f"[netapp_storage] import '{pve_storage_id}': "
                        "snapmanifest LV detected → snapinfo_initialized=1"
                    )
        except Exception as exc:
            log.warning(f"[netapp_storage] import: snapmanifest check failed: {exc}")

    db.execute(
        """INSERT INTO netapp_provisioned_datastores
           (id, name, endpoint_id, svm_name, volume_uuid, volume_name,
            protocol, lun_uuid, lun_path, igroup_uuid, igroup_name,
            ns_uuid, subsystem_uuid, subsystem_name,
            vg_name, lvm_type, lvm_pool_name, nfs_junction_path,
            pve_storage_id, pve_host_ids, size_bytes, status, error_message,
            created_by, created_at, updated_at, snapinfo_initialized)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ds_id, ds_name, mapping["endpoint_id"],
            svm_name, volume_uuid, mapping.get("volume_name", ""),
            protocol,
            lun_uuid, lun_path,
            igroup_uuid, igroup_name,
            ns_uuid, subsystem_uuid, subsystem_name,
            vg_name, lvm_type, lvm_pool_name, nfs_jpath,
            pve_storage_id, json.dumps(pve_host_ids),
            int(size_bytes), "active", "",
            username, now, now, snapinfo_initialized,
        ),
    )
    return {"success": True, "id": ds_id, "name": ds_name}


def _storage_unified():
    """
    GET  storage/unified
    Returns a merged, SnapMirror-enriched list of all known datastores:
      source = "provisioned"  — managed by this plugin (netapp_provisioned_datastores)
      source = "discovered"   — found by auto-discovery only (netapp_volume_mapping)
    Provisioned entries take precedence: a discovered mapping with the same
    pve_storage_id as a provisioned datastore is suppressed (already represented).
    """
    db = get_db()

    # ── 1. Provisioned datastores joined with SnapMirror ──────────────────────
    prov_rows = db.query("""
        SELECT p.*, ep.name AS endpoint_name,
               sm.id AS sm_id, sm.state AS sm_state, sm.lag_time AS sm_lag,
               sm.healthy AS sm_healthy, sm.dest_cluster_name AS sm_dest_cluster,
               sm.last_transfer_time AS sm_last_transfer,
               sm.dest_endpoint_id AS sm_dest_ep_id,
               sm.dest_svm AS sm_dest_svm, sm.dest_volume AS sm_dest_volume,
               sm.relationship_uuid AS sm_rel_uuid, sm.policy_name AS sm_policy_name
        FROM netapp_provisioned_datastores p
        LEFT JOIN netapp_endpoints ep ON ep.id = p.endpoint_id
        LEFT JOIN netapp_snapmirror_relationships sm
               ON sm.source_volume_uuid = p.volume_uuid
        ORDER BY p.created_at DESC
    """)

    items = []
    prov_storage_ids: set = set()

    # Pre-fetch valid host IDs to filter out stale entries in pve_host_ids
    _valid_host_ids: set = {
        row["id"] for row in (db.query("SELECT id FROM netapp_pve_hosts") or [])
    }
    # All configured hosts, ordered by name — used to compute connected/missing per DS
    _all_pve_hosts = [
        {"id": row["id"], "name": row["name"] or row["host"], "host": row["host"]}
        for row in (db.query("SELECT id, name, host FROM netapp_pve_hosts ORDER BY name") or [])
    ]

    for r in (prov_rows or []):
        d = _ds_to_dict(r)
        d["source"] = "provisioned"
        _backfill_size_bytes(db, d)
        # Filter out deleted PVE hosts so the host count stays accurate
        d["pve_host_ids"] = [h for h in (d.get("pve_host_ids") or []) if h in _valid_host_ids]
        _cids = set(d["pve_host_ids"])
        d["pve_host_details"] = {
            "connected": [h for h in _all_pve_hosts if h["id"] in _cids],
            "missing":   [h for h in _all_pve_hosts if h["id"] not in _cids],
        }
        # Backfill fields from volume_mapping (provisioned table doesn't have them)
        if d.get("protocol") in ("iscsi", "nvme"):
            vm_row = db.query_one(
                "SELECT id, lvm_vg_name, snapinfo_initialized FROM netapp_volume_mapping "
                "WHERE pve_storage_id=? AND storage_protocol=? LIMIT 1",
                (d.get("pve_storage_id", ""), d.get("protocol", "")),
            )
            if vm_row:
                vm = dict(vm_row)
                if not d.get("vg_name") and vm.get("lvm_vg_name"):
                    d["vg_name"] = vm["lvm_vg_name"]
                d["mapping_id"] = vm.get("id")
                d["snapinfo_initialized"] = bool(vm.get("snapinfo_initialized", 0))
            else:
                d.setdefault("mapping_id", None)
                d.setdefault("snapinfo_initialized", False)
        elif d.get("protocol") == "nfs":
            vm_row = db.query_one(
                "SELECT id FROM netapp_volume_mapping "
                "WHERE pve_storage_id=? AND storage_protocol='nfs' LIMIT 1",
                (d.get("pve_storage_id", ""),),
            )
            d["mapping_id"] = dict(vm_row)["id"] if vm_row else None
            d["snapinfo_initialized"] = False
        # SnapMirror sub-object (columns were added by LEFT JOIN)
        if d.get("sm_id"):
            d["snapmirror"] = {
                "id":                d.pop("sm_id"),
                "state":             d.pop("sm_state", None),
                "lag_time":          d.pop("sm_lag", None),
                "healthy":           bool(d.pop("sm_healthy", True)),
                "dest_cluster_name": d.pop("sm_dest_cluster", None),
                "last_transfer_time":d.pop("sm_last_transfer", None),
                "dest_endpoint_id":  d.pop("sm_dest_ep_id", None),
                "dest_svm":          d.pop("sm_dest_svm", None),
                "dest_volume":       d.pop("sm_dest_volume", None),
                "relationship_uuid": d.pop("sm_rel_uuid", None),
                "policy_name":       d.pop("sm_policy_name", None),
            }
        else:
            for k in ("sm_id", "sm_state", "sm_lag", "sm_healthy",
                      "sm_dest_cluster", "sm_last_transfer", "sm_dest_ep_id",
                      "sm_dest_svm", "sm_dest_volume", "sm_rel_uuid", "sm_policy_name"):
                d.pop(k, None)
            d["snapmirror"] = None
        prov_storage_ids.add(d.get("pve_storage_id", ""))
        items.append(d)

    # ── 2. Discovered mappings not already covered by provisioned entries ──────
    map_rows = db.query("""
        SELECT m.id AS mapping_id, m.pve_storage_id, m.svm_name, m.volume_uuid,
               m.volume_name, m.junction_path, m.storage_protocol,
               m.lvm_vg_name, m.snapinfo_initialized, m.endpoint_id, m.discovered_at,
               ep.name AS endpoint_name,
               sm.id AS sm_id, sm.state AS sm_state, sm.lag_time AS sm_lag,
               sm.healthy AS sm_healthy, sm.dest_cluster_name AS sm_dest_cluster,
               sm.last_transfer_time AS sm_last_transfer,
               sm.dest_endpoint_id AS sm_dest_ep_id,
               sm.dest_svm AS sm_dest_svm, sm.dest_volume AS sm_dest_volume,
               sm.relationship_uuid AS sm_rel_uuid, sm.policy_name AS sm_policy_name
        FROM netapp_volume_mapping m
        LEFT JOIN netapp_endpoints ep ON ep.id = m.endpoint_id
        LEFT JOIN netapp_snapmirror_relationships sm
               ON sm.source_volume_uuid = m.volume_uuid
        ORDER BY m.discovered_at DESC
    """)

    seen_discovered: set = set()
    for r in (map_rows or []):
        m = dict(r)
        storage_id = m.get("pve_storage_id", "")
        if storage_id in prov_storage_ids:
            continue  # already represented by a provisioned entry
        if storage_id in seen_discovered:
            continue  # deduplicate multi-host rows for the same volume
        seen_discovered.add(storage_id)

        # Collect all currently-configured hosts that have this volume discovered.
        # Use volume_uuid as the join key (more reliable than pve_storage_id, which can
        # differ per host) and INNER JOIN with netapp_pve_hosts to exclude stale entries
        # left behind from deleted hosts.
        volume_uuid = m.get("volume_uuid", "")
        if volume_uuid:
            host_rows = db.query(
                "SELECT DISTINCT m2.pve_cluster_id FROM netapp_volume_mapping m2 "
                "INNER JOIN netapp_pve_hosts h ON h.id = m2.pve_cluster_id "
                "WHERE m2.volume_uuid=?",
                (volume_uuid,),
            )
        else:
            host_rows = db.query(
                "SELECT DISTINCT m2.pve_cluster_id FROM netapp_volume_mapping m2 "
                "INNER JOIN netapp_pve_hosts h ON h.id = m2.pve_cluster_id "
                "WHERE m2.pve_storage_id=?",
                (storage_id,),
            )
        pve_host_ids = [dict(r)["pve_cluster_id"] for r in (host_rows or [])]

        item = {
            "id":               f"mapping-{m['mapping_id']}",
            "mapping_id":       m["mapping_id"],
            "source":           "discovered",
            "name":             storage_id,
            "pve_storage_id":   storage_id,
            "protocol":         m.get("storage_protocol", "nfs"),
            "endpoint_id":      m.get("endpoint_id"),
            "endpoint_name":    m.get("endpoint_name", ""),
            "svm_name":         m.get("svm_name", ""),
            "volume_name":      m.get("volume_name", ""),
            "volume_uuid":      m.get("volume_uuid", ""),
            "vg_name":          m.get("lvm_vg_name", ""),
            "nfs_junction_path":m.get("junction_path", ""),
            "size_bytes":       None,
            "status":           "active",
            "pve_host_ids":     pve_host_ids,
            "snapinfo_initialized": bool(m.get("snapinfo_initialized", 0)),
            "pve_host_details": {
                "connected": [h for h in _all_pve_hosts if h["id"] in set(pve_host_ids)],
                "missing":   [],
            },
        }
        if m.get("sm_id"):
            item["snapmirror"] = {
                "id":                m["sm_id"],
                "state":             m.get("sm_state"),
                "lag_time":          m.get("sm_lag"),
                "healthy":           bool(m.get("sm_healthy", True)),
                "dest_cluster_name": m.get("sm_dest_cluster"),
                "last_transfer_time":m.get("sm_last_transfer"),
                "dest_endpoint_id":  m.get("sm_dest_ep_id"),
                "dest_svm":          m.get("sm_dest_svm"),
                "dest_volume":       m.get("sm_dest_volume"),
                "relationship_uuid": m.get("sm_rel_uuid"),
                "policy_name":       m.get("sm_policy_name"),
            }
        else:
            item["snapmirror"] = None
        items.append(item)

    return {"items": items}


# ── Capacity cache ────────────────────────────────────────────────────────────
# storage/capacity calls PVE over the network and is slow (seconds per host).
# With WORKERS=1 this blocks every other request queued behind it.
# Solution: serve the last known result immediately; refresh in a background
# thread when the cached value is older than _CAP_TTL seconds.
_cap_cache: dict   = {"stats": {}}
_cap_ts:    float  = 0.0
_cap_lock          = threading.Lock()
_cap_loading       = False
_CAP_TTL           = 60  # seconds


def _fetch_capacity_bg():
    """Runs in a background thread; refreshes the capacity cache."""
    global _cap_cache, _cap_ts, _cap_loading
    try:
        result = _do_fetch_capacity()
        with _cap_lock:
            _cap_cache = result
            _cap_ts    = time.monotonic()
    except Exception as exc:
        log.debug(f"[netapp_storage] capacity background refresh failed: {exc}")
    finally:
        with _cap_lock:
            _cap_loading = False


def _do_fetch_capacity() -> dict:
    """Blocking fetch — never call from a request handler directly."""
    db = get_db()
    cap: dict = {}

    host_rows = db.query("SELECT * FROM netapp_pve_hosts")
    for hr in (host_rows or []):
        try:
            from ..core._helpers import build_pve_client
            pve = build_pve_client(db, dict(hr)["id"])

            r = pve._api_get(f"{pve._base}/cluster/resources?type=storage")
            if r.ok:
                for st in r.json().get("data", []):
                    sid   = st.get("storage")
                    used  = int(st.get("disk", 0))
                    total = int(st.get("maxdisk", 0))
                    if not sid or total == 0:
                        continue
                    if sid not in cap:
                        cap[sid] = {"used_bytes": used, "total_bytes": total, "vm_count": 0}
                    elif used > cap[sid]["used_bytes"]:
                        cap[sid]["used_bytes"] = used
                        cap[sid]["total_bytes"] = total

        except Exception as exc:
            log.debug(f"[netapp_storage] storage/capacity host: {exc}")

    # VM counts come from NaSnap's own snapshot history instead of a live,
    # per-node-per-storage PVE "storage content" scan (one API call per node
    # × per datastore — slow, and silently left vm_count at 0 whenever any of
    # those calls failed, which is what made the dashboard's VM column show
    # nothing even though VMs were clearly on the datastore).
    try:
        rows = db.query(
            "SELECT vm.pve_storage_id AS sid, s.vmids_json "
            "FROM netapp_snapshots s "
            "JOIN netapp_volume_mapping vm ON vm.id = s.mapping_id "
            "WHERE s.status='done'"
        )
        vmids_by_sid: dict = {}
        for r in (rows or []):
            r = dict(r)
            sid = r.get("sid") or ""
            if not sid:
                continue
            try:
                vmids = json.loads(r.get("vmids_json") or "[]")
            except Exception:
                vmids = []
            vmids_by_sid.setdefault(sid, set()).update(vmids)
        for sid, vmids in vmids_by_sid.items():
            cap.setdefault(sid, {"used_bytes": 0, "total_bytes": 0, "vm_count": 0})
            cap[sid]["vm_count"] = len(vmids)
    except Exception as exc:
        log.debug(f"[netapp_storage] storage/capacity vm counts: {exc}")

    return {"stats": cap}


def _storage_capacity():
    """
    Returns PVE-reported used/total bytes and VM count per storage_id.

    Always returns immediately from cache. A background thread refreshes the
    cache when it's older than _CAP_TTL seconds. With WORKERS=1 this ensures
    the slow PVE network calls never block other request handlers.
    """
    global _cap_loading
    now = time.monotonic()
    with _cap_lock:
        age       = now - _cap_ts
        loading   = _cap_loading
        cached    = _cap_cache

    if age > _CAP_TTL and not loading:
        with _cap_lock:
            _cap_loading = True
        threading.Thread(target=_fetch_capacity_bg, daemon=True).start()

    return cached


# ── Datastore index scan / reindex ───────────────────────────────────────────

def _ds_scan_creds(db, mapping):
    """Returns (pve_host, user, password, key) for an accessible PVE host.

    Tries the mapping's own pve_cluster_id first, then falls back to any
    configured PVE host.  The NFS mount is on the PVE node, so any host that
    has the datastore mounted will do.
    """
    candidate_ids = []
    primary = (mapping or {}).get("pve_cluster_id", "")
    if primary:
        candidate_ids.append(primary)
    other_rows = db.query(
        "SELECT id FROM netapp_pve_hosts WHERE id!=? ORDER BY id LIMIT 10",
        (primary,),
    )
    for r in (other_rows or []):
        candidate_ids.append(r["id"])

    last_err = None
    for host_id in candidate_ids:
        try:
            pve = build_pve_client(db, host_id)
            pu, pp, pk = get_ssh_creds(pve)
            return pve.host, pu, pp, pk
        except Exception as exc:
            last_err = exc

    raise RuntimeError(
        f"No accessible PVE host found for index scan "
        f"(tried {len(candidate_ids)}): {last_err}"
    )


def _reconcile_index_into_db(db, mapping_id, mapping, index):
    """Inserts index snapshots missing from the local DB.

    Returns (imported_count, skipped_count, vm_count).
    """
    from ..core.datastore_index import _now_iso
    imported = skipped = vm_count = 0
    pve_cluster_id = mapping.get("pve_cluster_id", "")

    for snap in (index.get("snapshots") or []):
        snap_name = snap.get("ontap_snapshot_name", "")
        if not snap_name:
            continue
        # Skip if this snapshot already exists under this or any sibling mapping
        # (same ONTAP volume → same volume_uuid → different mapping_id per PVE host)
        existing = db.query_one(
            """SELECT s.id FROM netapp_snapshots s
               JOIN netapp_volume_mapping vm ON vm.id = s.mapping_id
               WHERE s.snap_name=? AND vm.volume_uuid=(
                   SELECT volume_uuid FROM netapp_volume_mapping WHERE id=? LIMIT 1)""",
            (snap_name, mapping_id),
        )
        if existing:
            skipped += 1
            continue

        vms = snap.get("vms") or []
        vmids = [int(v["vmid"]) for v in vms if v.get("vmid") is not None]
        vm_count += len(vms)
        taken_at = snap.get("taken_at") or _now_iso()
        consistency = "app" if snap.get("consistent") else "crash"
        manifest = json.dumps({
            "schema": "nasnap-index-v1",
            "snapshot_name": snap_name,
            "created_at": taken_at,
            "consistency": consistency,
            "vms": [{"vmid": v["vmid"], "name": v.get("name", "")} for v in vms],
        })
        db.execute(
            """INSERT INTO netapp_snapshots
               (id, mapping_id, snap_name, consistency, pve_cluster_id, node,
                vmids_json, vm_types_json, manifest_path, manifest_json,
                status, source, created_at, completed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,'done','index_import',?,?)""",
            (
                str(_uuid.uuid4()), mapping_id, snap_name,
                consistency, pve_cluster_id, "",
                json.dumps(vmids), "{}",
                f"index_import:{mapping_id}", manifest,
                taken_at, taken_at,
            ),
        )
        imported += 1

    return imported, skipped, vm_count


def _prov_ds_index():
    """GET: return the raw .nasnap/index.json for a mapping.

    Accepts ?mapping_id=<id> or ?pve_storage_id=<sid> as lookup key.
    """
    from flask import request
    err = _require_admin()
    if err:
        return err
    mapping_id     = request.args.get("mapping_id") or (request.get_json(force=True) or {}).get("mapping_id")
    pve_storage_id = request.args.get("pve_storage_id")
    db = get_db()
    if mapping_id:
        mapping = db.query_one("SELECT * FROM netapp_volume_mapping WHERE id=?", (mapping_id,))
    elif pve_storage_id:
        mapping = db.query_one(
            "SELECT * FROM netapp_volume_mapping WHERE pve_storage_id=? LIMIT 1",
            (pve_storage_id,))
    else:
        return {"error": "mapping_id or pve_storage_id required"}, 400
    if not mapping:
        return {"error": "Mapping not found"}, 404
    mapping = dict(mapping)
    protocol = mapping.get("storage_protocol", "nfs")
    ph, pu, pp, pk = _ds_scan_creds(db, mapping)
    if protocol in ("iscsi", "nvme"):
        vg = mapping.get("lvm_vg_name")
        if not vg:
            return {"error": "No VG name for SAN mapping"}, 400
        if not mapping.get("snapinfo_initialized"):
            return {"error": "snapmanifest LV not initialized — run Setup snapmanifest first"}, 400
        from ..core.san_helpers import snapmanifest_read_index
        idx = snapmanifest_read_index(ph, pu, pp, pk, vg)
    else:
        if not mapping.get("nfs_mount_path"):
            return {"error": "No NFS mount path for mapping"}, 400
        from ..core.datastore_index import DatastoreIndex
        idx = DatastoreIndex(ph, pu, pp, pk).read(mapping["nfs_mount_path"])
    if idx is None:
        return {"index": None, "exists": False}
    return {"index": idx, "exists": True}


def _prov_ds_scan():
    """POST: read .nasnap/index.json from a datastore and reconcile missing snapshots into DB."""
    from flask import request
    err = _require_admin()
    if err:
        return err
    body = request.get_json(force=True) or {}
    mapping_id     = body.get("mapping_id")
    pve_storage_id = body.get("pve_storage_id")
    db = get_db()
    if mapping_id:
        mapping = db.query_one("SELECT * FROM netapp_volume_mapping WHERE id=?", (mapping_id,))
    elif pve_storage_id:
        mapping = db.query_one(
            "SELECT * FROM netapp_volume_mapping "
            "WHERE pve_storage_id=? AND storage_protocol='nfs' LIMIT 1",
            (pve_storage_id,))
    else:
        return {"error": "mapping_id or pve_storage_id required"}, 400
    if not mapping:
        return {"error": "Mapping not found"}, 404
    mapping_id = dict(mapping)["id"]
    mapping = dict(mapping)
    protocol = mapping.get("storage_protocol", "nfs")
    ph, pu, pp, pk = _ds_scan_creds(db, mapping)

    if protocol in ("iscsi", "nvme"):
        vg = mapping.get("lvm_vg_name")
        if not vg:
            return {"error": "No VG name for SAN mapping"}, 400
        if not mapping.get("snapinfo_initialized"):
            return {"error": "snapmanifest LV not initialized — run Setup snapmanifest first"}, 400
    elif not mapping.get("nfs_mount_path"):
        return {"error": "No NFS mount path for mapping"}, 400

    from ..core._helpers import run_as_job
    username = request.session.get("user", "system")

    def _do_scan(logger):
        logger.log(f"Scanning index for '{mapping.get('pve_storage_id','')}' ({protocol})")
        if protocol in ("iscsi", "nvme"):
            from ..core.san_helpers import snapmanifest_read_index
            index = snapmanifest_read_index(ph, pu, pp, pk, mapping["lvm_vg_name"])
            if index is None:
                logger.log("No snapmanifest index found on the datastore")
                return {"imported": 0, "skipped": 0, "vms_found": 0,
                        "index_exists": False, "discovered_vms": []}
        else:
            from ..core.datastore_index import DatastoreIndex
            ds_idx = DatastoreIndex(ph, pu, pp, pk)
            mount = mapping["nfs_mount_path"]
            index = ds_idx.read(mount)
            if index is None:
                vms = ds_idx.discover_vms(mount)
                logger.log(f"No .nasnap/index.json found — {len(vms)} VM config file(s) discovered via filesystem scan")
                return {"imported": 0, "skipped": 0, "vms_found": len(vms),
                        "index_exists": False, "discovered_vms": vms}

        imported, skipped, vm_count = _reconcile_index_into_db(db, mapping_id, mapping, index)
        logger.log(f"Imported {imported} snapshot(s), {skipped} already known, {vm_count} VM(s) found")
        return {
            "imported": imported,
            "skipped": skipped,
            "vms_found": vm_count,
            "index_exists": True,
            "snapshots_in_index": len(index.get("snapshots") or []),
        }

    try:
        job_id, result = run_as_job(db, "scan_index", username, _do_scan)
    except Exception as exc:
        return {"error": str(exc)}, 500
    result["job_id"] = job_id
    return result


def _prov_ds_scan_all():
    """POST: scan all datastores (NFS and SAN) and reconcile their indexes into the DB."""
    err = _require_admin()
    if err:
        return err
    db = get_db()
    from ..core._helpers import run_as_job
    username = request.session.get("user", "system")

    def _do(logger):
        return _scan_all_datastores(db, logger)

    job_id, result = run_as_job(db, "scan_index", username, _do)
    result["job_id"] = job_id
    return result


def _scan_all_datastores(db, logger=None):
    """Core of scan-all: reconciles every NFS/SAN datastore's index into the DB.
    Shared by the manual 'scan-all' route, the boot-time auto-scan, and the
    hourly datastore-scan job — logger is optional so callers without a job
    context (e.g. boot) can pass None."""
    nfs_rows = db.query(
        "SELECT * FROM netapp_volume_mapping "
        "WHERE storage_protocol='nfs' AND nfs_mount_path != ''") or []
    san_rows = db.query(
        "SELECT * FROM netapp_volume_mapping "
        "WHERE storage_protocol IN ('iscsi','nvme') AND snapinfo_initialized=1 AND lvm_vg_name != ''") or []
    results = []
    from ..core.datastore_index import DatastoreIndex
    from ..core.san_helpers import snapmanifest_read_index
    seen: set = set()
    for row in list(nfs_rows) + list(san_rows):
        m = dict(row)
        mid = m["id"]
        sid = m.get("pve_storage_id", "")
        if sid in seen:
            continue
        seen.add(sid)
        protocol = m.get("storage_protocol", "nfs")
        try:
            ph, pu, pp, pk = _ds_scan_creds(db, m)
            if protocol in ("iscsi", "nvme"):
                index = snapmanifest_read_index(ph, pu, pp, pk, m["lvm_vg_name"])
            else:
                index = DatastoreIndex(ph, pu, pp, pk).read(m["nfs_mount_path"])
            if index is None:
                if logger: logger.log(f"'{sid}': no index found")
                results.append({"mapping_id": mid, "pve_storage_id": sid,
                                 "protocol": protocol, "index_exists": False, "imported": 0})
                continue
            imported, skipped, vm_count = _reconcile_index_into_db(db, mid, m, index)
            if logger: logger.log(f"'{sid}': imported {imported}, {skipped} already known, {vm_count} VM(s)")
            results.append({
                "mapping_id": mid,
                "pve_storage_id": sid,
                "protocol": protocol,
                "index_exists": True,
                "imported": imported,
                "skipped": skipped,
                "vms_found": vm_count,
            })
        except Exception as exc:
            if logger: logger.log(f"'{sid}': ERROR {exc}")
            results.append({"mapping_id": mid, "pve_storage_id": sid,
                             "protocol": protocol, "error": str(exc)})
    return {"results": results, "total_datastores": len(results)}


def _prov_plugin_settings():
    """GET/POST plugin-level settings stored in netapp_plugin_config."""
    from flask import request
    err = _require_admin()
    if err:
        return err
    db = get_db()
    if request.method == 'GET':
        cfg = db.query_one(
            "SELECT auto_scan_on_startup FROM netapp_plugin_config WHERE id='default'")
        return {"auto_scan_on_startup": bool(cfg["auto_scan_on_startup"]) if cfg else False}
    body = request.get_json(force=True) or {}
    val = 1 if body.get("auto_scan_on_startup") else 0
    existing = db.query_one("SELECT id FROM netapp_plugin_config WHERE id='default'")
    if existing:
        db.execute(
            "UPDATE netapp_plugin_config SET auto_scan_on_startup=? WHERE id='default'", (val,))
    else:
        db.execute(
            "INSERT INTO netapp_plugin_config (id, auto_scan_on_startup) VALUES ('default', ?)", (val,))
    return {"auto_scan_on_startup": bool(val)}


def _run_startup_scan(created_by="startup"):
    """Scan all NFS and SAN datastores for index and reconcile DB. Runs in background."""
    from ..core._helpers import run_as_job
    db = get_db()

    def _do(logger):
        logger.log("Startup auto-scan: reconciling all datastore indexes …")
        result = _scan_all_datastores(db, logger)
        n_imported = sum(r.get("imported", 0) for r in result["results"])
        logger.log(f"Startup auto-scan complete: {result['total_datastores']} datastore(s) scanned, "
                    f"{n_imported} snapshot(s) imported")
        return result

    try:
        run_as_job(db, "scan_index", created_by, _do)
    except Exception as exc:
        log.warning(f"[netapp_storage] Startup auto-scan failed: {exc}")


def _run_detect_and_scan(created_by):
    """Combines the three actions the 'Detect & Scan' button used to fire off
    separately (discovery, SnapMirror scan, index reconciliation) into one
    logged job, so the user can see exactly what happened in the Activity Log
    instead of only in the server log file. Shared by the manual button and
    the hourly scan scheduler."""
    from ..core._helpers import run_as_job
    from ..core.discovery import run_discovery
    from ..core.snapmirror import scan_relationships
    db = get_db()

    def _do(logger):
        logger.log("Running datastore discovery …")
        mappings, debug_info = run_discovery()
        logger.log(f"Discovery: {len(mappings)} mapping(s) found")
        for reason in (debug_info.get("no_match_reasons") or [])[:10]:
            logger.log(f"Discovery note: {reason}")

        logger.log("Scanning SnapMirror relationships …")
        found, errors = scan_relationships(db)
        logger.log(f"SnapMirror scan: {found} relationship(s) found")
        for err in (errors or [])[:10]:
            logger.log(f"SnapMirror scan error: {err}")

        logger.log("Reconciling datastore indexes (ONTAP-native snapshots) …")
        scan_result = _scan_all_datastores(db, logger)
        n_imported = sum(r.get("imported", 0) for r in scan_result["results"])
        logger.log(f"Index reconciliation: {scan_result['total_datastores']} datastore(s) scanned, "
                    f"{n_imported} snapshot(s) imported")

        logger.log("Checking for snapshot records no longer present on ONTAP …")
        from ..core.gc import gc_stale_snapshots
        gc_result = gc_stale_snapshots(db, logger)

        logger.log("Checking for datastores no longer present on Proxmox …")
        from ..core.prune import prune_missing_datastores
        prune_result = prune_missing_datastores(db, logger)

        from ..core.instant_recovery_engine import check_stale_sessions
        check_stale_sessions(db, logger)

        return {
            "mappings_found":       len(mappings),
            "snapmirror_found":     found,
            "datastores_scanned":   scan_result["total_datastores"],
            "snapshots_imported":   n_imported,
            "snapshots_removed":    gc_result["removed"],
            "datastores_removed":   prune_result["datastores_removed"],
            "mappings_removed":     prune_result["mappings_removed"],
        }

    return run_as_job(db, "discover_scan", created_by, _do)


def _prov_detect_and_scan():
    """POST: runs discovery + SnapMirror scan + index reconciliation as one logged job."""
    from flask import request
    err = _require_admin()
    if err:
        return err
    username = request.session.get("user", "system")
    try:
        job_id, result = _run_detect_and_scan(username)
    except Exception as exc:
        return {"error": str(exc)}, 500
    result["job_id"] = job_id
    return result


def start_datastore_scan_scheduler():
    """Runs the same actions as 'Detect & Scan' once an hour in the background,
    so ONTAP-side changes (volumes, snapshots taken outside NaSnap) are picked
    up without a manual click."""
    def _loop():
        while True:
            try:
                _run_detect_and_scan("scheduler")
            except Exception as exc:
                log.warning(f"[netapp_storage] Hourly datastore scan failed: {exc}")
            time.sleep(3600)
    threading.Thread(target=_loop, daemon=True, name="nasnap-ds-scan").start()
    log.info("[netapp_storage] Hourly datastore scan scheduler started")


def _prov_ds_reindex():
    """POST: rebuild .nasnap/index.json from DB snapshot records for a mapping."""
    from flask import request
    err = _require_admin()
    if err:
        return err
    body = request.get_json(force=True) or {}
    mapping_id = body.get("mapping_id")
    if not mapping_id:
        return {"error": "mapping_id required"}, 400
    db = get_db()
    mapping = db.query_one("SELECT * FROM netapp_volume_mapping WHERE id=?", (mapping_id,))
    if not mapping:
        return {"error": "Mapping not found"}, 404
    mapping = dict(mapping)
    if mapping.get("storage_protocol") in ("iscsi", "nvme") or not mapping.get("nfs_mount_path"):
        return {"error": "Reindex only supported for NFS datastores"}, 400

    rows = db.query(
        "SELECT snap_name, consistency, vmids_json, manifest_json, created_at "
        "FROM netapp_snapshots WHERE mapping_id=? AND status='done' "
        "ORDER BY created_at DESC",
        (mapping_id,),
    )
    from ..core.datastore_index import DatastoreIndex, build_datastore_info, _new_index, _now_iso
    snapshots = []
    for row in rows:
        r = dict(row)
        vms = []
        try:
            mf = json.loads(r.get("manifest_json") or "{}")
            for v in mf.get("vms", []):
                vmid = v.get("vmid")
                if vmid is not None:
                    vms.append({
                        "vmid": vmid,
                        "name": v.get("name", ""),
                        "config_path": f"{vmid}/{vmid}.conf",
                        "config_sha256": "",
                    })
        except Exception:
            for vmid in (json.loads(r.get("vmids_json") or "[]") or []):
                vms.append({"vmid": vmid, "name": "", "config_path": f"{vmid}/{vmid}.conf",
                            "config_sha256": ""})
        snapshots.append({
            "ontap_snapshot_name": r["snap_name"],
            "taken_at": r.get("created_at", ""),
            "schedule_name": "",
            "consistent": r.get("consistency") in ("app", "suspend"),
            "locked": False,
            "lock_expires": None,
            "vms": vms,
        })

    ds_info = {
        "datastore_name": mapping.get("pve_storage_id", ""),
        "datastore_type": "nfs",
        "ontap_svm": mapping.get("svm_name", ""),
        "ontap_volume": mapping.get("volume_name", ""),
        "ontap_volume_uuid": mapping.get("volume_uuid", ""),
    }
    index = _new_index(ds_info)
    index["snapshots"] = snapshots
    index["last_updated"] = _now_iso()

    ph, pu, pp, pk = _ds_scan_creds(db, mapping)
    DatastoreIndex(ph, pu, pp, pk).write(mapping["nfs_mount_path"], index)
    return {"written": len(snapshots), "mapping_id": mapping_id}


# ── SnapMirror / SnapVault replication ────────────────────────────────────────

def _prov_replication_peering_status():
    """GET: live cluster+SVM peer status between a source and target endpoint/SVM."""
    err = _require_admin()
    if err:
        return err
    from flask import request
    source_ep_id = request.args.get("source_endpoint_id")
    target_ep_id = request.args.get("target_endpoint_id")
    source_svm   = request.args.get("source_svm", "")
    target_svm   = request.args.get("target_svm", "")
    if not (source_ep_id and target_ep_id and source_svm and target_svm):
        return {"error": "source_endpoint_id, target_endpoint_id, source_svm, target_svm required"}, 400
    db = get_db()
    try:
        source_client = build_ontap_client(get_endpoint(db, source_ep_id))
        target_client = build_ontap_client(get_endpoint(db, target_ep_id))
        source_cluster_name, _, _ = source_client.test_connection()
        target_cluster_name, _, _ = target_client.test_connection()
        cluster_peered = bool(source_client.get_cluster_peer_status(target_cluster_name))
        svm_peer = source_client.get_svm_peer_status(source_svm, target_svm, target_cluster_name)
        svm_peered = bool(svm_peer and svm_peer.get("state") == "peered")
        return {
            "source_cluster_name": source_cluster_name,
            "target_cluster_name": target_cluster_name,
            "cluster_peered": cluster_peered,
            "svm_peered": svm_peered,
        }
    except Exception as exc:
        return {"error": str(exc)}, 500


def _prov_replication_policies():
    """GET: list SnapMirror policies visible on an endpoint's SVM."""
    err = _require_admin()
    if err:
        return err
    from flask import request
    endpoint_id = request.args.get("endpoint_id")
    svm_name    = request.args.get("svm_name", "")
    if not (endpoint_id and svm_name):
        return {"error": "endpoint_id and svm_name required"}, 400
    db = get_db()
    try:
        client = build_ontap_client(get_endpoint(db, endpoint_id))
        policies = client.list_snapmirror_policies(svm_name)
        return {"policies": [{"name": p.get("name", ""), "type": p.get("type", "")} for p in policies]}
    except Exception as exc:
        return {"error": str(exc)}, 500


def _prov_replication_setup():
    """POST: set up (or retrofit) SnapMirror replication for an existing datastore."""
    err = _require_admin()
    if err:
        return err
    from flask import request
    body  = request.get_json(force=True) or {}
    ds_id = body.get("ds_id")
    target_endpoint_id = body.get("target_endpoint_id")
    target_svm         = body.get("target_svm")
    policy              = body.get("policy") or {}
    if not (ds_id and target_endpoint_id and target_svm):
        return {"error": "ds_id, target_endpoint_id, target_svm required"}, 400
    db  = get_db()
    row = db.query_one(
        "SELECT endpoint_id, svm_name, volume_name FROM netapp_provisioned_datastores WHERE id=?",
        (ds_id,))
    if not row:
        return {"error": "Datastore not found"}, 404
    ds = dict(row)
    username = request.session.get("user", "unknown")
    job_id = _start_job(db, "snapmirror_setup", ds_id, username)

    import threading
    def _run():
        jdb = get_db()
        jlog = JobLogger(job_id, jdb)
        try:
            from ..core.replication_engine import setup_replication
            setup_replication(
                ds["endpoint_id"], ds["svm_name"], ds["volume_name"],
                target_endpoint_id, target_svm, policy, jdb, jlog,
            )
            _finish_job(jdb, job_id)
        except Exception as exc:
            jlog.log(f"ERROR: {exc}")
            _fail_job(jdb, job_id)
    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id}, 202


def _prov_replication_update_policy():
    """POST: change the SnapMirror policy on an existing relationship."""
    err = _require_admin()
    if err:
        return err
    from flask import request
    body = request.get_json(force=True) or {}
    relationship_id = body.get("relationship_id")
    policy_name      = body.get("policy_name")
    if not (relationship_id and policy_name):
        return {"error": "relationship_id and policy_name required"}, 400
    db  = get_db()
    row = db.query_one(
        "SELECT source_endpoint_id, relationship_uuid FROM netapp_snapmirror_relationships WHERE id=?",
        (relationship_id,))
    if not row:
        return {"error": "Relationship not found"}, 404
    rel = dict(row)
    try:
        client = build_ontap_client(get_endpoint(db, rel["source_endpoint_id"]))
        client.update_snapmirror_policy(rel["relationship_uuid"], policy_name)
        db.execute(
            "UPDATE netapp_snapmirror_relationships SET policy_name=? WHERE id=?",
            (policy_name, relationship_id),
        )
        return {"success": True}
    except Exception as exc:
        return {"error": str(exc)}, 500


def _prov_replication_remove():
    """POST: break and remove a SnapMirror relationship (without deleting the datastore)."""
    err = _require_admin()
    if err:
        return err
    from flask import request
    body = request.get_json(force=True) or {}
    relationship_id   = body.get("relationship_id")
    delete_destination = bool(body.get("delete_destination", False))
    if not relationship_id:
        return {"error": "relationship_id required"}, 400
    db = get_db()
    username = request.session.get("user", "unknown")
    job_id = _start_job(db, "snapmirror_teardown", relationship_id, username)

    import threading
    def _run():
        jdb = get_db()
        jlog = JobLogger(job_id, jdb)
        try:
            from ..core.replication_engine import teardown_replication
            teardown_replication(relationship_id, delete_destination, jdb, jlog)
            _finish_job(jdb, job_id)
        except Exception as exc:
            jlog.log(f"ERROR: {exc}")
            _fail_job(jdb, job_id)
    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id}, 202


def register_routes():
    from nasnap_core.api.plugins import register_plugin_route
    register_plugin_route(PLUGIN_ID, "storage/unified",                    _storage_unified)
    register_plugin_route(PLUGIN_ID, "storage/capacity",                   _storage_capacity)
    register_plugin_route(PLUGIN_ID, "provisioning/datastores",            _prov_datastores)
    register_plugin_route(PLUGIN_ID, "provisioning/datastores/import",     _prov_import)
    register_plugin_route(PLUGIN_ID, "provisioning/datastores/remove",     _prov_remove)
    register_plugin_route(PLUGIN_ID, "provisioning/datastores/resize",     _prov_resize)
    register_plugin_route(PLUGIN_ID, "provisioning/datastores/add-host",   _prov_add_host)
    register_plugin_route(PLUGIN_ID, "provisioning/datastores/remove-host", _prov_remove_host)
    register_plugin_route(PLUGIN_ID, "provisioning/ontap-resources",       _prov_ontap_resources)
    register_plugin_route(PLUGIN_ID, "provisioning/pve-hosts",             _prov_pve_hosts)
    register_plugin_route(PLUGIN_ID, "provisioning/svms",                  _prov_svms)
    register_plugin_route(PLUGIN_ID, "provisioning/nfs-lifs",              _prov_nfs_lifs)
    register_plugin_route(PLUGIN_ID, "provisioning/check-nfs-lif",        _prov_check_nfs_lif)
    register_plugin_route(PLUGIN_ID, "provisioning/datastores/index",      _prov_ds_index)
    register_plugin_route(PLUGIN_ID, "provisioning/datastores/scan",       _prov_ds_scan)
    register_plugin_route(PLUGIN_ID, "provisioning/datastores/scan-all",   _prov_ds_scan_all)
    register_plugin_route(PLUGIN_ID, "provisioning/datastores/reindex",    _prov_ds_reindex)
    register_plugin_route(PLUGIN_ID, "discover-and-scan",                  _prov_detect_and_scan)
    register_plugin_route(PLUGIN_ID, "provisioning/plugin-settings",       _prov_plugin_settings)
    register_plugin_route(PLUGIN_ID, "provisioning/replication/peering-status", _prov_replication_peering_status)
    register_plugin_route(PLUGIN_ID, "provisioning/replication/policies",       _prov_replication_policies)
    register_plugin_route(PLUGIN_ID, "provisioning/replication/setup",         _prov_replication_setup)
    register_plugin_route(PLUGIN_ID, "provisioning/replication/update-policy", _prov_replication_update_policy)
    register_plugin_route(PLUGIN_ID, "provisioning/replication/remove",        _prov_replication_remove)


# ── Job helpers ───────────────────────────────────────────────────────────────

def _start_job(db, job_type, ds_id, username):
    job_id = str(_uuid.uuid4())
    db.execute(
        """INSERT INTO netapp_jobs
           (id, job_type, status, progress_pct, log_json, created_by, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (job_id, job_type, "running", 0, "[]", username, _now()),
    )
    return job_id


def _finish_job(db, job_id):
    db.execute(
        "UPDATE netapp_jobs SET status='done', progress_pct=100, completed_at=? WHERE id=?",
        (_now(), job_id),
    )


def _fail_job(db, job_id):
    db.execute(
        "UPDATE netapp_jobs SET status='failed', completed_at=? WHERE id=?",
        (_now(), job_id),
    )


def _set_ds_status(db, ds_id, status, error=""):
    db.execute(
        "UPDATE netapp_provisioned_datastores SET status=?, error_message=?, updated_at=? WHERE id=?",
        (status, error, _now(), ds_id),
    )


# ── Async job launchers (stubs — logic implemented per-protocol later) ────────

def _run_provision_job_async(job_id, ds_id, params, username):
    import threading
    t = threading.Thread(
        target=_run_provision, args=(job_id, ds_id, params, username), daemon=True)
    t.start()


def _run_resize_job_async(job_id, ds_id, new_size_bytes, username):
    import threading
    t = threading.Thread(
        target=_run_resize, args=(job_id, ds_id, new_size_bytes, username), daemon=True)
    t.start()


def _run_add_host_job_async(job_id, ds_id, host_id, username):
    import threading
    t = threading.Thread(
        target=_run_add_host, args=(job_id, ds_id, host_id, username), daemon=True)
    t.start()


def _run_remove_host_job_async(job_id, ds_id, host_id, username):
    import threading
    t = threading.Thread(
        target=_run_remove_host, args=(job_id, ds_id, host_id, username), daemon=True)
    t.start()


def _run_remove_job_async(job_id, ds_id, delete_ontap, snapmirror_action, username):
    import threading
    t = threading.Thread(
        target=_run_remove, args=(job_id, ds_id, delete_ontap, snapmirror_action, username), daemon=True)
    t.start()


# ── Job implementations ───────────────────────────────────────────────────────

def _run_provision(job_id, ds_id, params, username):
    db = get_db()
    jlog = JobLogger(job_id, db)
    try:
        protocol = params.get("protocol", "")
        jlog.log(f"Provisioning {protocol} datastore '{params.get('name')}' …")
        if protocol == "iscsi":
            _provision_iscsi(ds_id, params, db, jlog)
        elif protocol == "nvme":
            _provision_nvme(ds_id, params, db, jlog)
        elif protocol == "nfs":
            _provision_nfs(ds_id, params, db, jlog)
        else:
            jlog.log(f"Protocol '{protocol}' not yet implemented.")
            _set_ds_status(db, ds_id, "error", f"Protocol not implemented: {protocol}")
            _fail_job(db, job_id)
            return

        replication = params.get("replication") or {}
        if replication.get("enabled"):
            jlog.log("Setting up SnapMirror/SnapVault replication …")
            try:
                from ..core.replication_engine import setup_replication
                ds_row = dict(db.query_one(
                    "SELECT endpoint_id, svm_name, volume_name FROM netapp_provisioned_datastores WHERE id=?",
                    (ds_id,)))
                setup_replication(
                    ds_row["endpoint_id"], ds_row["svm_name"], ds_row["volume_name"],
                    replication["target_endpoint_id"], replication["target_svm"],
                    replication.get("policy") or {}, db, jlog,
                )
            except Exception as exc:
                # Replication failure does not roll back the already-created datastore —
                # it can be retried later via the "SnapMirror / Vault" action.
                jlog.log(f"WARNING: replication setup failed: {exc}")
                jlog.log("The datastore itself was created successfully; retry replication "
                         "from the datastore's SnapMirror / Vault action.")

        _finish_job(db, job_id)
    except Exception as exc:
        log.error(f"[netapp_storage] provision job {job_id}: {exc}")
        jlog.log(f"ERROR: {exc}")
        _set_ds_status(db, ds_id, "error", str(exc))
        _fail_job(db, job_id)


def _provision_iscsi(ds_id, params, db, jlog):
    endpoint_id    = params["endpoint_id"]
    svm_name       = params.get("svm_name", "")
    name           = params.get("name", ds_id)
    vg_name        = params.get("vg_name", "")
    lvm_type       = params.get("lvm_type", "linear")
    pve_storage_id = params.get("pve_storage_id", "")
    pve_host_ids   = params["pve_host_ids"]
    size_bytes     = int(params.get("size_bytes", 0))
    aggregate_name = params.get("aggregate_name", "") or None

    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)

    # ── ONTAP: Volume ────────────────────────────────────────────────────────
    volume_uuid = params.get("volume_uuid", "")
    volume_name = _ontap_safe_name(params.get("volume_name", ""))

    _cfg = load_plugin_config()
    _vol_multiplier = float(_cfg.get("san_volume_multiplier", 2.5))
    vol_size_bytes = int(size_bytes * _vol_multiplier)

    asa_mode = False
    snap_locking_enabled = 0
    if not volume_uuid:
        ag_info = f" on aggregate '{aggregate_name}'" if aggregate_name else " (auto-placement)"
        jlog.log(f"Creating ONTAP volume '{volume_name}' ({vol_size_bytes} bytes, "
                 f"{_vol_multiplier}× LUN size){ag_info} …")
        try:
            volume_uuid = client.create_volume_san(svm_name, volume_name, vol_size_bytes, aggregate_name=aggregate_name)
            jlog.log(f"Volume created: {volume_uuid}")
            try:
                client.enable_inline_compression(volume_uuid)
                jlog.log("Inline compression enabled.")
            except Exception as _ce:
                jlog.log(f"NOTE: inline compression not set ({_ce}) — AFF/ASA enable it by default.")
            if params.get("enable_snapshot_locking"):
                try:
                    client.enable_snapshot_locking(volume_uuid)
                    snap_locking_enabled = 1
                    jlog.log("Snapshot locking enabled on volume (Tamperproof-ready).")
                except Exception as _se:
                    jlog.log(f"WARNING: could not enable snapshot locking: {_se}")
        except OntapError as exc:
            if exc.status_code == 405:
                jlog.log("ASA platform detected — volume will be auto-provisioned with LUN.")
                asa_mode = True
            else:
                raise
    else:
        vol_info    = client.get_volume(volume_uuid)
        volume_name = vol_info.get("name", volume_name)
        snap_locking_enabled = 1 if (vol_info.get("snaplock") or {}).get("snapshot_locking_enabled") else 0
        jlog.log(f"Using existing volume: {volume_name}"
                 + (" [snapshot locking enabled]" if snap_locking_enabled else ""))

    # ── ONTAP: LUN ───────────────────────────────────────────────────────────
    lun_uuid = params.get("lun_uuid", "")
    lun_name = params.get("lun_name", "")
    lun_path = ""
    serial   = ""

    if not lun_uuid:
        jlog.log(f"Creating LUN '{lun_name}' ({size_bytes} bytes) …")
        lun_uuid, serial = client.create_lun(svm_name, volume_name, lun_name, size_bytes,
                                             auto_provision_as_flexvol=asa_mode)
        lun_path = f"/vol/{volume_name}/{lun_name}"
        jlog.log(f"LUN created: {lun_uuid}  serial={serial}")
        # On ASA, resolve auto-provisioned volume UUID
        if asa_mode and not volume_uuid:
            try:
                lun_info = client.get_lun(lun_uuid)
                loc_vol = (lun_info.get("location") or {}).get("volume") or {}
                volume_uuid = loc_vol.get("uuid", "")
                volume_name = loc_vol.get("name", volume_name)
                jlog.log(f"Auto-provisioned volume: {volume_name} ({volume_uuid})")
            except Exception as exc:
                jlog.log(f"WARNING: could not resolve auto-provisioned volume: {exc}")
    else:
        lun_info = client.get_lun(lun_uuid)
        serial   = lun_info.get("serial_number", "")
        lun_path = lun_info.get("name", "")
        jlog.log(f"Using existing LUN: {lun_path}  serial={serial}")

    if not serial:
        raise RuntimeError("LUN serial number is empty — cannot identify multipath device")

    # ── Collect IQNs from all selected PVE hosts ──────────────────────────────
    jlog.log("Collecting iSCSI initiator IQNs from PVE hosts …")
    host_meta = {}
    for hid in pve_host_ids:
        try:
            pve = build_pve_client(db, hid)
            su, sp, sk = get_ssh_creds(pve)
            iqn = get_iscsi_initiator_iqn(pve.host, su, sp, sk)
            if iqn:
                host_meta[hid] = {"host": pve.host, "user": su, "pass": sp, "key": sk, "iqn": iqn}
                jlog.log(f"  {pve.host}: {iqn}")
            else:
                jlog.log(f"  WARNING: no IQN from {pve.host} — host skipped")
        except Exception as exc:
            jlog.log(f"  WARNING: cannot connect to host {hid}: {exc}")

    if not host_meta:
        raise RuntimeError("No iSCSI IQNs collected from any host")

    # ── ONTAP: iGroup ────────────────────────────────────────────────────────
    igroup_uuid = params.get("igroup_uuid", "")
    igroup_name = params.get("igroup_name", "") or f"igr-{name.replace(' ', '-').lower()}"

    if not igroup_uuid:
        jlog.log(f"Creating iGroup '{igroup_name}' …")
        igroup_uuid = client.create_igroup(svm_name, igroup_name, protocol="iscsi", os_type="linux")
        jlog.log(f"iGroup created: {igroup_uuid}")
        for m in host_meta.values():
            client.add_igroup_initiator(igroup_uuid, m["iqn"])
            jlog.log(f"  Added initiator: {m['iqn']}")
    else:
        existing_igs  = client.list_igroups(svm_name=svm_name)
        existing_ig   = next((g for g in existing_igs if g.get("uuid") == igroup_uuid), None)
        existing_inits = {i.get("name", "") for i in (existing_ig.get("initiators") or [])} if existing_ig else set()
        jlog.log(f"Using existing iGroup, adding missing initiators …")
        for m in host_meta.values():
            if m["iqn"] not in existing_inits:
                client.add_igroup_initiator(igroup_uuid, m["iqn"])
                jlog.log(f"  Added initiator: {m['iqn']}")

    # ── ONTAP: Map LUN ────────────────────────────────────────────────────────
    jlog.log("Mapping LUN to iGroup …")
    try:
        client.map_lun(lun_uuid, igroup_uuid, svm_name=svm_name)
        jlog.log("LUN mapped.")
    except Exception as exc:
        if "already" in str(exc).lower() or "409" in str(exc):
            jlog.log("LUN already mapped — continuing.")
        else:
            raise

    # Persist ONTAP object IDs before touching hosts (so remove can clean up on retry)
    db.execute(
        """UPDATE netapp_provisioned_datastores
           SET volume_uuid=?, volume_name=?, lun_uuid=?, lun_path=?,
               igroup_uuid=?, igroup_name=?, updated_at=?
           WHERE id=?""",
        (volume_uuid, volume_name, lun_uuid, lun_path,
         igroup_uuid, igroup_name, _now(), ds_id),
    )

    # ── Get iSCSI target info ─────────────────────────────────────────────────
    portal_ip  = client.get_iscsi_lif_for_svm(svm_name)
    target_iqn = client.get_iscsi_target_iqn(svm_name)
    if not portal_ip:
        raise RuntimeError(f"No active iSCSI LIF found for SVM '{svm_name}'")
    if not target_iqn:
        raise RuntimeError(f"No iSCSI target IQN found for SVM '{svm_name}'")
    jlog.log(f"iSCSI target: {target_iqn} @ {portal_ip}")

    # ── Per host: discover → login → wait for device ──────────────────────────
    ordered_hosts = [hid for hid in pve_host_ids if hid in host_meta]
    for i, hid in enumerate(ordered_hosts):
        m  = host_meta[hid]
        sh, su, sp, sk = m["host"], m["user"], m["pass"], m["key"]

        jlog.log(f"[{sh}] Discovering iSCSI target …")
        ssh_run(sh, su, sp,
                f"iscsiadm -m discovery -t sendtargets -p {shlex.quote(portal_ip)} 2>&1 || true",
                key_material=sk, timeout=30)

        jlog.log(f"[{sh}] Logging into target …")
        ssh_run(sh, su, sp,
                f"iscsiadm -m node -T {shlex.quote(target_iqn)} -p {shlex.quote(portal_ip)}"
                f" --login 2>&1 || true",
                key_material=sk, timeout=30)

        jlog.log(f"[{sh}] Waiting for multipath device (serial={serial}) …")
        ssh_run(sh, su, sp,
                "sleep 3; udevadm settle --timeout=15 2>/dev/null; "
                "multipath 2>/dev/null; sleep 2; true",
                key_material=sk, timeout=40)
        device = find_device_by_serial(sh, su, sp, sk, serial, timeout_s=60)
        jlog.log(f"[{sh}] Device ready: {device}")

        if i == 0:
            vg_q  = shlex.quote(vg_name)
            dev_q = shlex.quote(device)
            out   = ssh_run(sh, su, sp,
                            f"vgs {vg_q} 2>/dev/null && echo EXISTS || echo MISSING",
                            capture=True, key_material=sk)
            if "EXISTS" not in out:
                jlog.log(f"[{sh}] Creating PV + VG '{vg_name}' …")
                ssh_run(sh, su, sp, f"pvcreate {dev_q}", key_material=sk)
                ssh_run(sh, su, sp, f"vgcreate {vg_q} {dev_q}", key_material=sk)
                jlog.log(f"[{sh}] VG '{vg_name}' created.")

                if lvm_type == "thin":
                    pool_name = params.get("lvm_pool_name") or "data"
                    pool_q = shlex.quote(pool_name)
                    ssh_run(sh, su, sp,
                            f"lvcreate -l 95%VG --thin {vg_q}/{pool_q}",
                            key_material=sk, timeout=30)
                    jlog.log(f"[{sh}] Thin pool '{pool_name}' created.")
                    db.execute(
                        "UPDATE netapp_provisioned_datastores SET lvm_pool_name=?, updated_at=? WHERE id=?",
                        (pool_name, _now(), ds_id),
                    )
            else:
                jlog.log(f"[{sh}] VG '{vg_name}' already exists.")

            # Create snapmanifest LV (idempotent) so snapshots work on this datastore
            from ..core.san_helpers import snapmanifest_initialize
            jlog.log(f"[{sh}] Initializing snapmanifest LV …")
            try:
                snapmanifest_initialize(sh, su, sp, sk, vg_name)
                jlog.log(f"[{sh}] snapmanifest LV ready.")
            except Exception as exc:
                jlog.log(f"[{sh}] WARNING: snapmanifest init failed: {exc}")

    # ── All hosts: pvscan --cache -aay ────────────────────────────────────────
    mapper_dev = _iscsi_serial_to_mapper(serial)
    jlog.log("Activating VG on all hosts via pvscan …")
    for hid in ordered_hosts:
        m  = host_meta[hid]
        sh, su, sp, sk = m["host"], m["user"], m["pass"], m["key"]
        try:
            ssh_run(sh, su, sp,
                    f"pvscan --cache -aay {shlex.quote(mapper_dev)} 2>/dev/null; true",
                    key_material=sk, timeout=30)
            jlog.log(f"[{sh}] pvscan done.")
        except Exception as exc:
            jlog.log(f"[{sh}] WARNING: pvscan failed: {exc}")

    # ── PVE: register storage on every host ──────────────────────────────────
    # Each standalone PVE host keeps its own storage.cfg — pvesm add must run
    # on each host individually (unlike a cluster where one node propagates).
    vg_q  = shlex.quote(vg_name)
    sid_q = shlex.quote(pve_storage_id)
    lvm_pool_name_final = params.get("lvm_pool_name") or (
        dict(db.query_one("SELECT lvm_pool_name FROM netapp_provisioned_datastores WHERE id=?",
                          (ds_id,)) or {})).get("lvm_pool_name", "") or "data"
    if lvm_type == "thin":
        pvesm_cmd = (f"pvesm add lvmthin {sid_q} --vgname {vg_q}"
                     f" --thinpool {shlex.quote(lvm_pool_name_final)}"
                     f" --shared 1 --content images,rootdir")
    else:
        pvesm_cmd = (f"pvesm add lvm {sid_q} --vgname {vg_q}"
                     f" --shared 1 --content images,rootdir")

    for hid in ordered_hosts:
        m  = host_meta[hid]
        sh, su, sp, sk = m["host"], m["user"], m["pass"], m["key"]
        jlog.log(f"[{sh}] Registering PVE storage '{pve_storage_id}' …")
        try:
            check = ssh_run(sh, su, sp,
                            f"pvesm status {shlex.quote(pve_storage_id)} 2>/dev/null"
                            f" && echo EXISTS || echo MISSING",
                            capture=True, key_material=sk)
            if "EXISTS" not in check:
                try:
                    ssh_run(sh, su, sp, pvesm_cmd, key_material=sk, timeout=30)
                    jlog.log(f"[{sh}] PVE storage registered.")
                except Exception as exc:
                    if "already defined" in str(exc).lower():
                        jlog.log(f"[{sh}] PVE storage already propagated from cluster.")
                    else:
                        jlog.log(f"[{sh}] WARNING: pvesm add failed: {exc}")
            else:
                jlog.log(f"[{sh}] PVE storage already exists.")
        except Exception as exc:
            jlog.log(f"[{sh}] WARNING: pvesm add failed: {exc}")

    _set_ds_status(db, ds_id, "active")

    # ── Register volume_mapping for each host (enables Snapshots tab) ──────────
    lvm_pool_name = params.get("lvm_pool_name") or (
        dict(db.query_one("SELECT lvm_pool_name FROM netapp_provisioned_datastores WHERE id=?",
                          (ds_id,)) or {})).get("lvm_pool_name", "")
    now = _now()
    for hid in ordered_hosts:
        mid = str(_uuid.uuid4())
        try:
            db.execute(
                """INSERT INTO netapp_volume_mapping
                   (id, endpoint_id, pve_cluster_id, pve_storage_id, svm_name,
                    volume_uuid, volume_name, junction_path, nfs_export_ip,
                    nfs_mount_path, discovered_at,
                    storage_protocol, lun_uuid, lun_path,
                    lvm_vg_name, lvm_type, lvm_pool_name,
                    snapinfo_initialized, snapinfo_lv_name,
                    snapshot_locking_enabled, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(pve_cluster_id, pve_storage_id) DO UPDATE SET
                   endpoint_id=excluded.endpoint_id, svm_name=excluded.svm_name,
                   volume_uuid=excluded.volume_uuid, volume_name=excluded.volume_name,
                   lun_uuid=excluded.lun_uuid, lun_path=excluded.lun_path,
                   lvm_vg_name=excluded.lvm_vg_name, lvm_type=excluded.lvm_type,
                   lvm_pool_name=excluded.lvm_pool_name,
                   storage_protocol=excluded.storage_protocol,
                   snapinfo_initialized=excluded.snapinfo_initialized,
                   snapshot_locking_enabled=excluded.snapshot_locking_enabled,
                   discovered_at=excluded.discovered_at""",
                (mid, endpoint_id, hid, pve_storage_id, svm_name,
                 volume_uuid, volume_name, "", "", "", now,
                 "iscsi", lun_uuid, lun_path,
                 vg_name, lvm_type, lvm_pool_name,
                 1, "netapp_snapmanifest", snap_locking_enabled, _now()),
            )
            jlog.log(f"Volume mapping registered for host {hid}.")
        except Exception as exc:
            jlog.log(f"WARNING: could not register volume mapping for host {hid}: {exc}")

    jlog.log(f"Provisioning complete. Datastore '{name}' is active.")


def _run_resize(job_id, ds_id, new_size_bytes, username):
    db = get_db()
    jlog = JobLogger(job_id, db)
    try:
        row = db.query_one(
            "SELECT * FROM netapp_provisioned_datastores WHERE id=?", (ds_id,))
        if not row:
            raise RuntimeError(f"Datastore {ds_id} not found")
        ds       = dict(row)
        protocol = ds.get("protocol", "")
        old_size = ds.get("size_bytes", 0)
        jlog.log(f"Resizing {protocol} datastore '{ds.get('name')}' "
                 f"from {old_size} to {new_size_bytes} bytes …")

        endpoint = get_endpoint(db, ds["endpoint_id"])
        client   = build_ontap_client(endpoint)
        vol_uuid = ds.get("volume_uuid", "")

        # The ONTAP volume must be substantially larger than the LUN/namespace
        # to leave headroom for snapshots.  Default multiplier is 2.5×
        # (configurable via san_volume_multiplier in config.json).
        _vol_multiplier = float(load_plugin_config().get("san_volume_multiplier", 2.5))
        _SAN_VOL_SIZE = int(new_size_bytes * _vol_multiplier)

        if protocol == "nfs":
            # NFS: only ONTAP resize needed (PVE mounts adjust automatically)
            jlog.log("Resizing ONTAP volume …")
            client.resize_volume(vol_uuid, new_size_bytes)
            jlog.log("Volume resized.")

        elif protocol == "iscsi":
            lun_uuid = ds.get("lun_uuid", "")
            vg_name  = ds.get("vg_name", "")
            pve_host_ids = json.loads(ds.get("pve_host_ids") or "[]")

            jlog.log("Resizing ONTAP volume …")
            client.resize_volume(vol_uuid, _SAN_VOL_SIZE)
            jlog.log("Resizing LUN …")
            client.resize_lun(lun_uuid, new_size_bytes)
            jlog.log("ONTAP objects resized.")

            # Host-side: pvresize so LVM sees the new size
            lun_serial = ""
            try:
                lun_serial = client.get_lun_serial(lun_uuid)
            except Exception as exc:
                jlog.log(f"WARNING: cannot fetch LUN serial: {exc}")

            for hid in pve_host_ids:
                try:
                    pve = build_pve_client(db, hid)
                    su, sp, sk = get_ssh_creds(pve)
                    sh = pve.host
                    jlog.log(f"[{sh}] Rescanning SCSI bus …")
                    ssh_run(sh, su, sp,
                            "for d in /sys/class/scsi_device/*/device/rescan; do "
                            "  echo 1 > $d 2>/dev/null; done; "
                            "udevadm settle --timeout=10 2>/dev/null; "
                            "multipath 2>/dev/null; true",
                            key_material=sk, timeout=30)
                    if lun_serial and vg_name:
                        mapper = _iscsi_serial_to_mapper(lun_serial)
                        jlog.log(f"[{sh}] Resizing PV {mapper} …")
                        ssh_run(sh, su, sp,
                                f"multipathd resize map $(basename {shlex.quote(mapper)}) 2>/dev/null; "
                                f"pvresize {shlex.quote(mapper)} 2>/dev/null; "
                                f"pvscan --cache 2>/dev/null; true",
                                key_material=sk, timeout=30)
                        jlog.log(f"[{sh}] PV resized.")
                except Exception as exc:
                    jlog.log(f"[{sh}] WARNING: host resize: {exc}")

        elif protocol == "nvme":
            ns_uuid  = ds.get("ns_uuid", "")
            vg_name  = ds.get("vg_name", "")
            pve_host_ids = json.loads(ds.get("pve_host_ids") or "[]")

            jlog.log("Resizing ONTAP volume …")
            client.resize_volume(vol_uuid, _SAN_VOL_SIZE)
            jlog.log("Resizing NVMe namespace …")
            client.resize_namespace(ns_uuid, new_size_bytes)
            jlog.log("ONTAP objects resized.")

            for hid in pve_host_ids:
                try:
                    pve = build_pve_client(db, hid)
                    su, sp, sk = get_ssh_creds(pve)
                    sh = pve.host
                    jlog.log(f"[{sh}] Rescanning NVMe namespaces …")
                    from ..core.san_helpers import nvme_ns_rescan
                    nvme_ns_rescan(sh, su, sp, sk)
                    if vg_name:
                        out = ssh_run(sh, su, sp,
                                      f"pvs --noheadings -o pv_name --select 'vgname={vg_name}' 2>/dev/null",
                                      capture=True, key_material=sk, timeout=15)
                        for pv in (l.strip() for l in out.splitlines() if l.strip()):
                            jlog.log(f"[{sh}] Resizing PV {pv} …")
                            ssh_run(sh, su, sp,
                                    f"pvresize {shlex.quote(pv)} 2>/dev/null; "
                                    f"pvscan --cache 2>/dev/null; true",
                                    key_material=sk, timeout=30)
                        jlog.log(f"[{sh}] PV resized.")
                except Exception as exc:
                    jlog.log(f"[{sh}] WARNING: host resize: {exc}")

        else:
            raise RuntimeError(f"Resize not supported for protocol '{protocol}'")

        db.execute(
            "UPDATE netapp_provisioned_datastores SET size_bytes=?, updated_at=? WHERE id=?",
            (new_size_bytes, _now(), ds_id),
        )
        _finish_job(db, job_id)
        jlog.log("Resize complete.")
    except Exception as exc:
        log.error(f"[netapp_storage] resize job {job_id}: {exc}")
        jlog.log(f"ERROR: {exc}")
        _fail_job(db, job_id)


def _run_add_host(job_id, ds_id, host_id, username):
    db = get_db()
    jlog = JobLogger(job_id, db)
    try:
        row = db.query_one("SELECT * FROM netapp_provisioned_datastores WHERE id=?", (ds_id,))
        if not row:
            raise RuntimeError(f"Datastore {ds_id} not found")
        ds       = dict(row)
        protocol = ds.get("protocol", "")
        jlog.log(f"Adding host to {protocol} datastore '{ds.get('name')}' …")

        if protocol == "iscsi":
            _add_host_iscsi(ds_id, ds, host_id, db, jlog)
        elif protocol == "nvme":
            _add_host_nvme(ds_id, ds, host_id, db, jlog)
        elif protocol == "nfs":
            _add_host_nfs(ds_id, ds, host_id, db, jlog)
        else:
            raise RuntimeError(f"Add-host not implemented for protocol '{protocol}'")

        _finish_job(db, job_id)
        jlog.log("Host added successfully.")
    except Exception as exc:
        log.error(f"[netapp_storage] add_host job {job_id}: {exc}")
        jlog.log(f"ERROR: {exc}")
        _fail_job(db, job_id)


def _add_host_iscsi(ds_id, ds, host_id, db, jlog):
    from ..core.san_helpers import (get_iscsi_initiator_iqn, find_device_by_serial,
                                    _iscsi_serial_to_mapper)

    endpoint_id    = ds["endpoint_id"]
    svm_name       = ds.get("svm_name", "")
    igroup_uuid    = ds.get("igroup_uuid", "")
    lun_uuid       = ds.get("lun_uuid", "")
    vg_name        = ds.get("vg_name", "")
    lvm_type       = ds.get("lvm_type", "linear")
    lvm_pool_name  = ds.get("lvm_pool_name", "") or "data"
    pve_storage_id = ds.get("pve_storage_id", "")
    volume_uuid    = ds.get("volume_uuid", "")
    volume_name    = ds.get("volume_name", "")
    lun_path       = ds.get("lun_path", "")

    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)

    lun_serial = ""
    if lun_uuid:
        try:
            lun_serial = client.get_lun_serial(lun_uuid)
        except Exception as exc:
            raise RuntimeError(f"Cannot fetch LUN serial: {exc}")
    if not lun_serial:
        raise RuntimeError("LUN serial not available — cannot identify multipath device")

    portal_ip  = client.get_iscsi_lif_for_svm(svm_name)
    target_iqn = client.get_iscsi_target_iqn(svm_name)
    if not portal_ip or not target_iqn:
        raise RuntimeError(f"Cannot determine iSCSI target for SVM '{svm_name}'")
    jlog.log(f"iSCSI target: {target_iqn} @ {portal_ip}")

    pve = build_pve_client(db, host_id)
    su, sp, sk = get_ssh_creds(pve)
    sh = pve.host

    # 1. Collect IQN
    jlog.log(f"[{sh}] Collecting iSCSI IQN …")
    iqn = get_iscsi_initiator_iqn(sh, su, sp, sk)
    if not iqn:
        raise RuntimeError(f"No IQN on host {sh}")
    jlog.log(f"[{sh}] IQN: {iqn}")

    # 2. Add IQN to ONTAP iGroup (idempotent)
    if igroup_uuid:
        jlog.log(f"Adding IQN to iGroup …")
        try:
            existing_ig = next(
                (g for g in client.list_igroups(svm_name=svm_name) if g.get("uuid") == igroup_uuid),
                None)
            existing_inits = {i.get("name", "") for i in (existing_ig.get("initiators") or [])} \
                if existing_ig else set()
            if iqn not in existing_inits:
                client.add_igroup_initiator(igroup_uuid, iqn)
                jlog.log(f"IQN added to iGroup.")
            else:
                jlog.log(f"IQN already in iGroup.")
        except Exception as exc:
            jlog.log(f"WARNING: add initiator: {exc}")

    # 3. iSCSI discovery + login
    jlog.log(f"[{sh}] iSCSI discovery …")
    ssh_run(sh, su, sp,
            f"iscsiadm -m discovery -t sendtargets -p {shlex.quote(portal_ip)} 2>&1 || true",
            key_material=sk, timeout=30)
    jlog.log(f"[{sh}] iSCSI login …")
    ssh_run(sh, su, sp,
            f"iscsiadm -m node -T {shlex.quote(target_iqn)} -p {shlex.quote(portal_ip)}"
            f" --login 2>&1 || true",
            key_material=sk, timeout=30)

    # 4. Wait for multipath device
    jlog.log(f"[{sh}] Waiting for multipath device (serial={lun_serial}) …")
    ssh_run(sh, su, sp,
            "sleep 3; udevadm settle --timeout=15 2>/dev/null; "
            "multipath 2>/dev/null; sleep 2; true",
            key_material=sk, timeout=40)
    device = find_device_by_serial(sh, su, sp, sk, lun_serial, timeout_s=60)
    jlog.log(f"[{sh}] Device ready: {device}")

    # 5. pvscan --cache to activate the existing VG on this host
    mapper_dev = _iscsi_serial_to_mapper(lun_serial)
    jlog.log(f"[{sh}] Activating VG via pvscan …")
    try:
        ssh_run(sh, su, sp,
                f"pvscan --cache -aay {shlex.quote(mapper_dev)} 2>/dev/null; true",
                key_material=sk, timeout=30)
        jlog.log(f"[{sh}] pvscan done.")
    except Exception as exc:
        jlog.log(f"[{sh}] WARNING: pvscan: {exc}")

    # 6. pvesm: cluster nodes inherit storage.cfg automatically; standalone needs explicit add
    if pve_storage_id:
        vg_q  = shlex.quote(vg_name)
        sid_q = shlex.quote(pve_storage_id)
        try:
            check = ssh_run(sh, su, sp,
                            f"pvesm status {shlex.quote(pve_storage_id)} 2>/dev/null"
                            f" && echo EXISTS || echo MISSING",
                            capture=True, key_material=sk)
            if "EXISTS" not in check:
                jlog.log(f"[{sh}] Storage not in PVE config — registering …")
                if lvm_type == "thin":
                    pvesm_cmd = (f"pvesm add lvmthin {sid_q} --vgname {vg_q}"
                                 f" --thinpool {shlex.quote(lvm_pool_name)}"
                                 f" --shared 1 --content images,rootdir")
                else:
                    pvesm_cmd = (f"pvesm add lvm {sid_q} --vgname {vg_q}"
                                 f" --shared 1 --content images,rootdir")
                ssh_run(sh, su, sp, pvesm_cmd, key_material=sk, timeout=30)
                jlog.log(f"[{sh}] PVE storage registered.")
            else:
                jlog.log(f"[{sh}] PVE storage already in cluster config.")
        except Exception as exc:
            jlog.log(f"[{sh}] WARNING: pvesm: {exc}")

    # 7. Register volume_mapping so this host appears in Snapshots/Volume Discovery
    existing_map = db.query_one(
        "SELECT snapshot_locking_enabled FROM netapp_volume_mapping WHERE volume_uuid=? LIMIT 1",
        (volume_uuid,))
    _snap_lock = (dict(existing_map) if existing_map else {}).get("snapshot_locking_enabled", 0)
    mid = str(_uuid.uuid4())
    try:
        db.execute(
            """INSERT INTO netapp_volume_mapping
               (id, endpoint_id, pve_cluster_id, pve_storage_id, svm_name,
                volume_uuid, volume_name, junction_path, nfs_export_ip,
                nfs_mount_path, discovered_at,
                storage_protocol, lun_uuid, lun_path,
                lvm_vg_name, lvm_type, lvm_pool_name,
                snapinfo_initialized, snapinfo_lv_name,
                snapshot_locking_enabled, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(pve_cluster_id, pve_storage_id) DO UPDATE SET
               endpoint_id=excluded.endpoint_id, svm_name=excluded.svm_name,
               volume_uuid=excluded.volume_uuid, volume_name=excluded.volume_name,
               lun_uuid=excluded.lun_uuid, lun_path=excluded.lun_path,
               lvm_vg_name=excluded.lvm_vg_name, lvm_type=excluded.lvm_type,
               lvm_pool_name=excluded.lvm_pool_name,
               storage_protocol=excluded.storage_protocol,
               snapinfo_initialized=excluded.snapinfo_initialized,
               snapshot_locking_enabled=excluded.snapshot_locking_enabled,
               discovered_at=excluded.discovered_at""",
            (mid, endpoint_id, host_id, pve_storage_id, svm_name,
             volume_uuid, volume_name, "", "", "", _now(),
             "iscsi", lun_uuid, lun_path,
             vg_name, lvm_type, lvm_pool_name,
             1, "netapp_snapmanifest", _snap_lock, _now()),
        )
        jlog.log(f"Volume mapping registered.")
    except Exception as exc:
        jlog.log(f"WARNING: volume mapping: {exc}")

    # 8. Update pve_host_ids
    current_ids = json.loads(ds.get("pve_host_ids") or "[]")
    if host_id not in current_ids:
        current_ids.append(host_id)
        db.execute(
            "UPDATE netapp_provisioned_datastores SET pve_host_ids=?, updated_at=? WHERE id=?",
            (json.dumps(current_ids), _now(), ds_id),
        )
        jlog.log(f"Host {host_id} added to datastore record.")

    # ── Access test ───────────────────────────────────────────────────────────────
    jlog.log(f"[{sh}] Running iSCSI access test …")
    _at_cmds = [
        f"iscsiadm -m session 2>/dev/null | grep -q {shlex.quote(target_iqn)}"
        f" || {{ echo 'iSCSI session not found'; exit 1; }}",
    ]
    if vg_name:
        _at_cmds.append(
            f"vgs --noheadings {shlex.quote(vg_name)} >/dev/null 2>&1"
            f" || {{ echo 'VG not active'; exit 1; }}"
        )
    _at_cmds.append("echo OK")
    _at_out = ssh_run(sh, su, sp, "; ".join(_at_cmds), capture=True, key_material=sk, timeout=20)
    if "OK" not in _at_out:
        raise RuntimeError(
            f"iSCSI access test failed on {sh} — session or VG not active: {_at_out.strip()}"
        )
    jlog.log(f"[{sh}] iSCSI access test passed.")


def _run_remove_host(job_id, ds_id, host_id, username):
    db = get_db()
    jlog = JobLogger(job_id, db)
    try:
        row = db.query_one(
            "SELECT * FROM netapp_provisioned_datastores WHERE id=?", (ds_id,))
        if not row:
            raise RuntimeError(f"Datastore {ds_id} not found")
        ds       = dict(row)
        protocol = ds.get("protocol", "")
        jlog.log(f"Removing host {host_id} from {protocol} datastore '{ds.get('name')}' …")

        if protocol == "iscsi":
            _remove_host_iscsi(ds_id, ds, host_id, db, jlog)
        elif protocol == "nvme":
            _remove_host_nvme(ds_id, ds, host_id, db, jlog)
        elif protocol == "nfs":
            _remove_host_nfs(ds_id, ds, host_id, db, jlog)
        else:
            raise RuntimeError(f"Remove-host not implemented for protocol '{protocol}'")

        _finish_job(db, job_id)
        jlog.log("Host removed.")
    except Exception as exc:
        log.error(f"[netapp_storage] remove_host job {job_id}: {exc}")
        jlog.log(f"ERROR: {exc}")
        _fail_job(db, job_id)


def _run_remove(job_id, ds_id, delete_ontap, snapmirror_action, username):
    db = get_db()
    jlog = JobLogger(job_id, db)
    try:
        row = db.query_one("SELECT * FROM netapp_provisioned_datastores WHERE id=?", (ds_id,))
        if not row:
            raise RuntimeError(f"Datastore {ds_id} not found")
        ds       = dict(row)
        protocol = ds.get("protocol", "")
        jlog.log(f"Removing {protocol} datastore '{ds.get('name')}' (delete_ontap={delete_ontap}) …")

        # SnapMirror relationships must be broken/released before ONTAP allows the
        # source volume to be deleted — tear this down first, regardless of protocol.
        sm_row = db.query_one(
            "SELECT id FROM netapp_snapmirror_relationships WHERE source_volume_uuid=? "
            "OR (source_endpoint_id=? AND source_svm=? AND source_volume=?)",
            (ds.get("volume_uuid", ""), ds.get("endpoint_id", ""),
             ds.get("svm_name", ""), ds.get("volume_name", "")),
        )
        if sm_row:
            jlog.log("SnapMirror relationship detected on this datastore — tearing it down first …")
            try:
                from ..core.replication_engine import teardown_replication
                teardown_replication(
                    dict(sm_row)["id"], snapmirror_action == "delete", db, jlog)
            except Exception as exc:
                jlog.log(f"WARNING: SnapMirror teardown failed: {exc}")

        if protocol == "iscsi":
            _remove_iscsi(ds_id, ds, delete_ontap, db, jlog)
        elif protocol == "nvme":
            _remove_nvme(ds_id, ds, delete_ontap, db, jlog)
        elif protocol == "nfs":
            _remove_nfs(ds_id, ds, delete_ontap, db, jlog)
        else:
            jlog.log(f"Remove not implemented for protocol '{protocol}'.")
            _set_ds_status(db, ds_id, "error", "Remove not implemented for this protocol")
            _fail_job(db, job_id)
            return

        # Remove volume_mapping rows (ON DELETE CASCADE would cascade snapshots too)
        pve_storage_id = ds.get("pve_storage_id", "")
        pve_host_ids_raw = json.loads(ds.get("pve_host_ids") or "[]")
        if pve_storage_id and pve_host_ids_raw:
            for hid in pve_host_ids_raw:
                db.execute(
                    "DELETE FROM netapp_volume_mapping WHERE pve_cluster_id=? AND pve_storage_id=?",
                    (hid, pve_storage_id),
                )
        db.execute("DELETE FROM netapp_provisioned_datastores WHERE id=?", (ds_id,))
        _finish_job(db, job_id)
        jlog.log("Datastore removed.")
    except Exception as exc:
        log.error(f"[netapp_storage] remove job {job_id}: {exc}")
        jlog.log(f"ERROR: {exc}")
        _set_ds_status(db, ds_id, "error", str(exc))
        _fail_job(db, job_id)


def _remove_iscsi(ds_id, ds, delete_ontap, db, jlog):
    from ..core.san_helpers import flush_iscsi_clone_device

    endpoint_id    = ds["endpoint_id"]
    svm_name       = ds.get("svm_name", "")
    vg_name        = ds.get("vg_name", "")
    pve_storage_id = ds.get("pve_storage_id", "")
    pve_host_ids   = json.loads(ds.get("pve_host_ids") or "[]")
    lun_uuid       = ds.get("lun_uuid", "")
    volume_uuid    = ds.get("volume_uuid", "")
    igroup_uuid    = ds.get("igroup_uuid", "")

    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)

    target_iqn = client.get_iscsi_target_iqn(svm_name) if svm_name else ""

    # Fetch LUN serial — needed to flush the multipath device on each host
    lun_serial = ""
    if lun_uuid:
        try:
            lun_serial = client.get_lun_serial(lun_uuid)
            jlog.log(f"LUN serial for multipath flush: {lun_serial}")
        except Exception as exc:
            jlog.log(f"WARNING: could not fetch LUN serial: {exc}")

    # ── Per-host teardown ─────────────────────────────────────────────────────
    # Correct order: pvesm → VG deactivate/remove → iSCSI logout → multipath flush
    # Multipath flush must come AFTER iSCSI logout; flushing while sessions are up
    # causes multipathd to immediately recreate the DM device, leaving a zombie
    # with queue_if_no_path that hangs subsequent pvs/vgs calls on the host.
    for hid in pve_host_ids:
        try:
            pve = build_pve_client(db, hid)
            su, sp, sk = get_ssh_creds(pve)
            sh = pve.host

            # 1. Remove PVE storage entry (each standalone host keeps its own config)
            if pve_storage_id:
                jlog.log(f"[{sh}] Removing PVE storage '{pve_storage_id}' …")
                try:
                    ssh_run(sh, su, sp,
                            f"pvesm remove {shlex.quote(pve_storage_id)} 2>/dev/null; true",
                            key_material=sk, timeout=30)
                    jlog.log(f"[{sh}] PVE storage removed.")
                except Exception as exc:
                    jlog.log(f"[{sh}] WARNING: pvesm remove: {exc}")

            # 2. Deactivate VG — releases DM device so multipath can be flushed
            if vg_name:
                vg_q = shlex.quote(vg_name)
                jlog.log(f"[{sh}] Deactivating VG '{vg_name}' …")
                try:
                    ssh_run(sh, su, sp,
                            f"vgchange -an {vg_q} 2>/dev/null; "
                            f"udevadm settle --timeout=5 2>/dev/null; "
                            f"vgremove -f {vg_q} 2>/dev/null; "
                            f"true",
                            key_material=sk, timeout=45)
                    jlog.log(f"[{sh}] VG deactivated and removed.")
                except Exception as exc:
                    jlog.log(f"[{sh}] WARNING: VG teardown: {exc}")

            # 3. iSCSI logout + delete persistent node entry (must precede multipath flush)
            if target_iqn:
                iqn_q = shlex.quote(target_iqn)
                jlog.log(f"[{sh}] iSCSI logout …")
                try:
                    ssh_run(sh, su, sp,
                            f"iscsiadm -m node -T {iqn_q} --logout 2>/dev/null; "
                            f"iscsiadm -m node -T {iqn_q} -o delete 2>/dev/null; "
                            f"true",
                            key_material=sk, timeout=30)
                    jlog.log(f"[{sh}] iSCSI logged out.")
                except Exception as exc:
                    jlog.log(f"[{sh}] WARNING: iSCSI logout: {exc}")

            # 4. Flush multipath device + remove sdX paths (sessions are gone at this point)
            if lun_serial:
                jlog.log(f"[{sh}] Flushing multipath device …")
                flush_iscsi_clone_device(sh, su, sp, sk, lun_serial)
                jlog.log(f"[{sh}] Multipath flushed.")

        except Exception as exc:
            jlog.log(f"WARNING: cannot reach host {hid}: {exc}")

    # ── ONTAP cleanup ─────────────────────────────────────────────────────────
    if lun_uuid and igroup_uuid:
        jlog.log("Unmapping LUN from iGroup …")
        try:
            client.unmap_lun(lun_uuid, igroup_uuid)
            jlog.log("LUN unmapped.")
        except Exception as exc:
            jlog.log(f"WARNING: unmap (may already be unmapped): {exc}")

    if delete_ontap:
        if lun_uuid:
            jlog.log(f"Deleting LUN {lun_uuid} …")
            try:
                client.delete_lun(lun_uuid)
                jlog.log("LUN deleted.")
            except Exception as exc:
                jlog.log(f"WARNING: LUN delete: {exc}")
        if volume_uuid:
            jlog.log(f"Deleting volume {volume_uuid} …")
            try:
                client.delete_volume(volume_uuid)
                jlog.log("Volume deleted.")
            except Exception as exc:
                jlog.log(f"WARNING: volume delete: {exc}")
        if igroup_uuid:
            jlog.log(f"Deleting iGroup {igroup_uuid} …")
            try:
                client.delete_igroup(igroup_uuid)
                jlog.log("iGroup deleted.")
            except Exception as exc:
                jlog.log(f"WARNING: iGroup delete: {exc}")


# ── iSCSI: remove single host ─────────────────────────────────────────────────

def _remove_host_iscsi(ds_id, ds, host_id, db, jlog):
    from ..core.san_helpers import flush_iscsi_clone_device

    endpoint_id    = ds["endpoint_id"]
    svm_name       = ds.get("svm_name", "")
    vg_name        = ds.get("vg_name", "")
    pve_storage_id = ds.get("pve_storage_id", "")
    lun_uuid       = ds.get("lun_uuid", "")
    igroup_uuid    = ds.get("igroup_uuid", "")

    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)

    target_iqn = client.get_iscsi_target_iqn(svm_name) if svm_name else ""
    lun_serial = ""
    if lun_uuid:
        try:
            lun_serial = client.get_lun_serial(lun_uuid)
        except Exception as exc:
            jlog.log(f"WARNING: cannot fetch LUN serial: {exc}")

    try:
        pve = build_pve_client(db, host_id)
        su, sp, sk = get_ssh_creds(pve)
        sh = pve.host

        if pve_storage_id:
            jlog.log(f"[{sh}] Removing PVE storage entry …")
            ssh_run(sh, su, sp,
                    f"pvesm remove {shlex.quote(pve_storage_id)} 2>/dev/null; true",
                    key_material=sk, timeout=30)

        if vg_name:
            vg_q = shlex.quote(vg_name)
            jlog.log(f"[{sh}] Deactivating VG '{vg_name}' …")
            ssh_run(sh, su, sp,
                    f"vgchange -an {vg_q} 2>/dev/null; udevadm settle --timeout=5 2>/dev/null; true",
                    key_material=sk, timeout=30)

        if lun_serial:
            jlog.log(f"[{sh}] Flushing multipath device …")
            flush_iscsi_clone_device(sh, su, sp, sk, lun_serial)

        if target_iqn:
            iqn_q = shlex.quote(target_iqn)
            jlog.log(f"[{sh}] iSCSI logout …")
            ssh_run(sh, su, sp,
                    f"iscsiadm -m node -T {iqn_q} --logout 2>/dev/null; "
                    f"iscsiadm -m node -T {iqn_q} -o delete 2>/dev/null; true",
                    key_material=sk, timeout=30)
    except Exception as exc:
        jlog.log(f"WARNING: cannot reach host {host_id}: {exc}")

    # Remove IQN from iGroup
    if igroup_uuid:
        try:
            pve = build_pve_client(db, host_id)
            su, sp, sk = get_ssh_creds(pve)
            iqn = get_iscsi_initiator_iqn(pve.host, su, sp, sk)
            if iqn:
                client.remove_igroup_initiator(igroup_uuid, iqn)
                jlog.log(f"Removed IQN {iqn} from iGroup.")
        except Exception as exc:
            jlog.log(f"WARNING: remove IQN from iGroup: {exc}")

    _remove_host_from_ds_record(ds_id, host_id, pve_storage_id, db, jlog)


# ── NVMe: provision ───────────────────────────────────────────────────────────

def _provision_nvme(ds_id, params, db, jlog):
    endpoint_id    = params["endpoint_id"]
    svm_name       = params.get("svm_name", "")
    name           = params.get("name", ds_id)
    vg_name        = params.get("vg_name", "")
    lvm_type       = params.get("lvm_type", "linear")
    pve_storage_id = params.get("pve_storage_id", "")
    pve_host_ids   = params["pve_host_ids"]
    size_bytes     = int(params.get("size_bytes", 0))
    aggregate_name = params.get("aggregate_name", "") or None

    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)

    # Resume from a previous failed run: inject persisted ONTAP UUIDs into params
    # so creation steps are skipped if the objects already exist on ONTAP.
    _saved = dict(db.query_one(
        "SELECT ns_uuid, volume_uuid, volume_name, subsystem_uuid, subsystem_name "
        "FROM netapp_provisioned_datastores WHERE id=?", (ds_id,)) or {})
    for _k in ("ns_uuid", "volume_uuid", "volume_name", "subsystem_uuid", "subsystem_name"):
        if _saved.get(_k) and not params.get(_k):
            params[_k] = _saved[_k]
    if _saved.get("ns_uuid") or _saved.get("subsystem_uuid"):
        jlog.log("Resuming provisioning — reusing existing ONTAP objects.")

    # ── ONTAP: Volume ────────────────────────────────────────────────────────
    volume_uuid = params.get("volume_uuid", "")
    volume_name = _ontap_safe_name(params.get("volume_name", ""))
    _cfg = load_plugin_config()
    _vol_multiplier = float(_cfg.get("san_volume_multiplier", 2.5))
    vol_size_bytes = int(size_bytes * _vol_multiplier)

    asa_mode = False
    snap_locking_enabled = 0
    if not volume_uuid:
        ag_info = f" on aggregate '{aggregate_name}'" if aggregate_name else " (auto-placement)"
        jlog.log(f"Creating ONTAP volume '{volume_name}' ({vol_size_bytes} bytes, "
                 f"{_vol_multiplier}× namespace size){ag_info} …")
        try:
            volume_uuid = client.create_volume_san(svm_name, volume_name, vol_size_bytes,
                                                   aggregate_name=aggregate_name)
            jlog.log(f"Volume created: {volume_uuid}")
            try:
                client.enable_inline_compression(volume_uuid)
                jlog.log("Inline compression enabled.")
            except Exception as _ce:
                jlog.log(f"NOTE: inline compression not set ({_ce}) — AFF/ASA enable it by default.")
            if params.get("enable_snapshot_locking"):
                try:
                    client.enable_snapshot_locking(volume_uuid)
                    snap_locking_enabled = 1
                    jlog.log("Snapshot locking enabled on volume (Tamperproof-ready).")
                except Exception as _se:
                    jlog.log(f"WARNING: could not enable snapshot locking: {_se}")
        except OntapError as exc:
            if exc.status_code == 405:
                jlog.log("ASA platform detected — volume will be auto-provisioned with namespace.")
                asa_mode = True
            else:
                raise
    else:
        vol_info    = client.get_volume(volume_uuid)
        volume_name = vol_info.get("name", volume_name)
        snap_locking_enabled = 1 if (vol_info.get("snaplock") or {}).get("snapshot_locking_enabled") else 0
        jlog.log(f"Using existing volume: {volume_name}"
                 + (" [snapshot locking enabled]" if snap_locking_enabled else ""))

    # ── ONTAP: Namespace ──────────────────────────────────────────────────────
    ns_uuid  = params.get("ns_uuid", "")
    ns_name  = params.get("ns_name", "") or f"ns-{name.replace(' ', '-').lower()}"
    if not ns_uuid:
        jlog.log(f"Creating NVMe namespace '{ns_name}' ({size_bytes} bytes) …")
        ns_uuid = client.create_namespace(svm_name, volume_name, ns_name, size_bytes,
                                          aggregate_name=aggregate_name if asa_mode else None)
        jlog.log(f"Namespace created: {ns_uuid}")
        # On ASA, the volume was auto-created — resolve its UUID and name
        if asa_mode and not volume_uuid:
            try:
                ns_info = client.get_namespace(ns_uuid)
                loc_vol = (ns_info.get("location") or {}).get("volume") or {}
                volume_uuid = loc_vol.get("uuid", "")
                volume_name = loc_vol.get("name", volume_name)
                jlog.log(f"Auto-provisioned volume: {volume_name} ({volume_uuid})")
            except Exception:
                # get_namespace may also 404 on ASA R2 — look up volume by name
                try:
                    vols = client._get_all_records(
                        "storage/volumes",
                        params={"svm.name": svm_name, "name": volume_name,
                                "fields": "uuid,name", "max_records": 5},
                    )
                    if vols:
                        volume_uuid = vols[0].get("uuid", "")
                        volume_name = vols[0].get("name", volume_name)
                        jlog.log(f"Auto-provisioned volume (by name): {volume_name} ({volume_uuid})")
                except Exception as exc2:
                    jlog.log(f"WARNING: could not resolve auto-provisioned volume: {exc2}")
        # Volume auto-created via storage/namespaces — apply snapshot/ARP settings now
        if volume_uuid:
            try:
                client.disable_volume_snapshots_and_arp(volume_uuid)
                jlog.log("Snapshot policy and ARP disabled on volume.")
            except Exception as exc:
                jlog.log(f"WARNING: could not apply volume policies: {exc}")
    else:
        jlog.log(f"Using existing namespace: {ns_uuid}")

    # ── Collect Host NQNs ─────────────────────────────────────────────────────
    jlog.log("Collecting NVMe host NQNs …")
    host_meta = {}
    for hid in pve_host_ids:
        try:
            pve = build_pve_client(db, hid)
            su, sp, sk = get_ssh_creds(pve)
            nqn = get_nvme_host_nqn(pve.host, su, sp, sk)
            if nqn:
                host_meta[hid] = {"host": pve.host, "user": su, "pass": sp, "key": sk, "nqn": nqn}
                jlog.log(f"  {pve.host}: {nqn}")
            else:
                jlog.log(f"  WARNING: no NQN from {pve.host} — host skipped")
        except Exception as exc:
            jlog.log(f"  WARNING: cannot connect to host {hid}: {exc}")

    if not host_meta:
        raise RuntimeError("No NVMe host NQNs collected from any host")

    # ── ONTAP: Subsystem ──────────────────────────────────────────────────────
    subsystem_uuid = params.get("subsystem_uuid", "")
    subsystem_name = params.get("subsystem_name", "") or f"sub-{name.replace(' ', '-').lower()}"
    if not subsystem_uuid:
        jlog.log(f"Creating NVMe subsystem '{subsystem_name}' …")
        subsystem_uuid = client.create_nvme_subsystem(svm_name, subsystem_name)
        jlog.log(f"Subsystem created: {subsystem_uuid}")
        for m in host_meta.values():
            client.add_nvme_host_to_subsystem(subsystem_uuid, m["nqn"])
            jlog.log(f"  Added host NQN: {m['nqn']}")
    else:
        existing_sub  = client.get_nvme_subsystem(subsystem_uuid)
        existing_nqns = {h.get("nqn", "") for h in (existing_sub.get("hosts") or [])}
        jlog.log("Using existing subsystem, adding missing host NQNs …")
        for m in host_meta.values():
            if m["nqn"] not in existing_nqns:
                client.add_nvme_host_to_subsystem(subsystem_uuid, m["nqn"])
                jlog.log(f"  Added host NQN: {m['nqn']}")

    # ── ONTAP: Map namespace to subsystem ─────────────────────────────────────
    jlog.log("Mapping namespace to subsystem …")
    try:
        client.add_nvme_namespace_to_subsystem(subsystem_uuid, ns_uuid, svm_name=svm_name)
        jlog.log("Namespace mapped.")
    except Exception as exc:
        if "already" in str(exc).lower() or "409" in str(exc):
            jlog.log("Namespace already mapped — continuing.")
        else:
            raise

    # Persist ONTAP IDs before host-side work
    db.execute(
        """UPDATE netapp_provisioned_datastores
           SET volume_uuid=?, volume_name=?, ns_uuid=?,
               subsystem_uuid=?, subsystem_name=?, updated_at=?
           WHERE id=?""",
        (volume_uuid, volume_name, ns_uuid, subsystem_uuid, subsystem_name, _now(), ds_id),
    )

    # ── Get NVMe/TCP LIF IPs and subsystem NQN ───────────────────────────────
    lif_ips = client.get_nvme_lifs_for_svm(svm_name)
    jlog.log(f"NVMe/TCP LIFs: {lif_ips}")

    subsystem_nqn = ""
    try:
        sub_info = client.get_nvme_subsystem(subsystem_uuid)
        subsystem_nqn = sub_info.get("target_nqn", "")
    except Exception:
        pass
    if subsystem_nqn:
        jlog.log(f"Subsystem NQN: {subsystem_nqn}")
    else:
        jlog.log("WARNING: could not retrieve subsystem NQN — falling back to connect-all")

    # ── Per host: connect → wait for device ───────────────────────────────────
    ordered_hosts = [hid for hid in pve_host_ids if hid in host_meta]
    for i, hid in enumerate(ordered_hosts):
        m  = host_meta[hid]
        sh, su, sp, sk = m["host"], m["user"], m["pass"], m["key"]

        # Ensure discovery.conf has entries for these LIFs (persistent after reboot)
        if lif_ips:
            added = ensure_nvme_discovery_entries(sh, su, sp, sk, lif_ips)
            if added:
                jlog.log(f"[{sh}] Added {added} LIF(s) to /etc/nvme/discovery.conf")

        jlog.log(f"[{sh}] Capturing NVMe device baseline …")
        devices_before = nvme_list_devices(sh, su, sp, sk)

        if subsystem_nqn and lif_ips:
            jlog.log(f"[{sh}] Connecting NVMe (direct per-LIF) …")
            nvme_connect_to_subsystem(sh, su, sp, sk, lif_ips, subsystem_nqn)
        else:
            jlog.log(f"[{sh}] Connecting NVMe (connect-all fallback) …")
            nvme_connect_all(sh, su, sp, sk)

        jlog.log(f"[{sh}] Waiting for NVMe namespace device …")
        if subsystem_nqn:
            device = find_nvme_device_for_subsystem_nqn(sh, su, sp, sk, subsystem_nqn, timeout_s=90)
        else:
            device = find_new_nvme_device(sh, su, sp, sk, devices_before, timeout_s=90)
        jlog.log(f"[{sh}] Device ready: {device}")
        host_meta[hid]["device"] = device

        if i == 0:
            vg_q  = shlex.quote(vg_name)
            dev_q = shlex.quote(device)
            out   = ssh_run(sh, su, sp,
                            f"vgs {vg_q} 2>/dev/null && echo EXISTS || echo MISSING",
                            capture=True, key_material=sk)
            if "EXISTS" not in out:
                jlog.log(f"[{sh}] Creating PV + VG '{vg_name}' …")
                ssh_run(sh, su, sp, f"pvcreate {dev_q}", key_material=sk)
                ssh_run(sh, su, sp, f"vgcreate {vg_q} {dev_q}", key_material=sk)
                jlog.log(f"[{sh}] VG '{vg_name}' created.")

                if lvm_type == "thin":
                    pool_name = params.get("lvm_pool_name") or "data"
                    pool_q = shlex.quote(pool_name)
                    ssh_run(sh, su, sp,
                            f"lvcreate -l 95%VG --thin {vg_q}/{pool_q}",
                            key_material=sk, timeout=30)
                    jlog.log(f"[{sh}] Thin pool '{pool_name}' created.")
                    db.execute(
                        "UPDATE netapp_provisioned_datastores SET lvm_pool_name=?, updated_at=? WHERE id=?",
                        (pool_name, _now(), ds_id),
                    )
            else:
                jlog.log(f"[{sh}] VG '{vg_name}' already exists.")

            jlog.log(f"[{sh}] Initializing snapmanifest LV …")
            try:
                snapmanifest_initialize(sh, su, sp, sk, vg_name)
                jlog.log(f"[{sh}] snapmanifest LV ready.")
            except Exception as exc:
                jlog.log(f"[{sh}] WARNING: snapmanifest init failed: {exc}")

    # ── All hosts: pvscan --cache -aay ────────────────────────────────────────
    # Use the device path directly — pvs --select 'vgname=...' returns empty on
    # secondary hosts because LVM hasn't cached the VG metadata yet (VG was
    # created on the first host).  Scanning the block device directly is reliable.
    jlog.log("Activating VG on all hosts via pvscan …")
    for hid in ordered_hosts:
        m  = host_meta[hid]
        sh, su, sp, sk = m["host"], m["user"], m["pass"], m["key"]
        dev = m.get("device", "")
        try:
            if dev:
                ssh_run(sh, su, sp,
                        f"pvscan --cache -aay {shlex.quote(dev)} 2>/dev/null; true",
                        key_material=sk, timeout=30)
            else:
                # fallback: scan all (slower but safe)
                ssh_run(sh, su, sp,
                        f"vgchange -ay {shlex.quote(vg_name)} 2>/dev/null; true",
                        key_material=sk, timeout=30)
            jlog.log(f"[{sh}] pvscan done.")
        except Exception as exc:
            jlog.log(f"[{sh}] WARNING: pvscan failed: {exc}")

    # ── PVE: register storage on every host ───────────────────────────────────
    vg_q  = shlex.quote(vg_name)
    sid_q = shlex.quote(pve_storage_id)
    lvm_pool_name_final = params.get("lvm_pool_name") or (
        dict(db.query_one("SELECT lvm_pool_name FROM netapp_provisioned_datastores WHERE id=?",
                          (ds_id,)) or {})).get("lvm_pool_name", "") or "data"
    if lvm_type == "thin":
        pvesm_cmd = (f"pvesm add lvmthin {sid_q} --vgname {vg_q}"
                     f" --thinpool {shlex.quote(lvm_pool_name_final)}"
                     f" --shared 1 --content images,rootdir")
    else:
        pvesm_cmd = (f"pvesm add lvm {sid_q} --vgname {vg_q}"
                     f" --shared 1 --content images,rootdir")

    for hid in ordered_hosts:
        m  = host_meta[hid]
        sh, su, sp, sk = m["host"], m["user"], m["pass"], m["key"]
        jlog.log(f"[{sh}] Registering PVE storage '{pve_storage_id}' …")
        try:
            check = ssh_run(sh, su, sp,
                            f"pvesm status {shlex.quote(pve_storage_id)} 2>/dev/null"
                            f" && echo EXISTS || echo MISSING",
                            capture=True, key_material=sk)
            if "EXISTS" not in check:
                try:
                    ssh_run(sh, su, sp, pvesm_cmd, key_material=sk, timeout=30)
                    jlog.log(f"[{sh}] PVE storage registered.")
                except Exception as exc:
                    if "already defined" in str(exc).lower():
                        jlog.log(f"[{sh}] PVE storage already propagated from cluster.")
                    else:
                        jlog.log(f"[{sh}] WARNING: pvesm add failed: {exc}")
            else:
                jlog.log(f"[{sh}] PVE storage already exists.")
        except Exception as exc:
            jlog.log(f"[{sh}] WARNING: pvesm add failed: {exc}")

    _set_ds_status(db, ds_id, "active")

    # ── Register volume_mapping for each host ─────────────────────────────────
    lvm_pool_name = params.get("lvm_pool_name") or (
        dict(db.query_one("SELECT lvm_pool_name FROM netapp_provisioned_datastores WHERE id=?",
                          (ds_id,)) or {})).get("lvm_pool_name", "")
    now = _now()
    for hid in ordered_hosts:
        mid = str(_uuid.uuid4())
        try:
            db.execute(
                """INSERT INTO netapp_volume_mapping
                   (id, endpoint_id, pve_cluster_id, pve_storage_id, svm_name,
                    volume_uuid, volume_name, junction_path, nfs_export_ip,
                    nfs_mount_path, discovered_at,
                    storage_protocol, lun_uuid, lun_path,
                    lvm_vg_name, lvm_type, lvm_pool_name,
                    snapinfo_initialized, snapinfo_lv_name,
                    snapshot_locking_enabled, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(pve_cluster_id, pve_storage_id) DO UPDATE SET
                   endpoint_id=excluded.endpoint_id, svm_name=excluded.svm_name,
                   volume_uuid=excluded.volume_uuid, volume_name=excluded.volume_name,
                   lun_uuid=excluded.lun_uuid, lun_path=excluded.lun_path,
                   lvm_vg_name=excluded.lvm_vg_name, lvm_type=excluded.lvm_type,
                   lvm_pool_name=excluded.lvm_pool_name,
                   storage_protocol=excluded.storage_protocol,
                   snapinfo_initialized=excluded.snapinfo_initialized,
                   snapshot_locking_enabled=excluded.snapshot_locking_enabled,
                   discovered_at=excluded.discovered_at""",
                (mid, endpoint_id, hid, pve_storage_id, svm_name,
                 volume_uuid, volume_name, "", "", "", now,
                 "nvme", ns_uuid, f"/vol/{volume_name}/{ns_name}",
                 vg_name, lvm_type, lvm_pool_name,
                 1, "netapp_snapmanifest", snap_locking_enabled, _now()),
            )
            jlog.log(f"Volume mapping registered for host {hid}.")
        except Exception as exc:
            jlog.log(f"WARNING: could not register volume mapping for host {hid}: {exc}")

    jlog.log(f"NVMe provisioning complete. Datastore '{name}' is active.")


# ── NVMe: remove ─────────────────────────────────────────────────────────────

def _remove_nvme(ds_id, ds, delete_ontap, db, jlog):
    endpoint_id    = ds["endpoint_id"]
    svm_name       = ds.get("svm_name", "")
    vg_name        = ds.get("vg_name", "")
    pve_storage_id = ds.get("pve_storage_id", "")
    pve_host_ids   = json.loads(ds.get("pve_host_ids") or "[]")
    ns_uuid        = ds.get("ns_uuid", "")
    volume_uuid    = ds.get("volume_uuid", "")
    subsystem_uuid = ds.get("subsystem_uuid", "")
    subsystem_name = ds.get("subsystem_name", "")

    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)

    # ── Per-host teardown ─────────────────────────────────────────────────────
    for hid in pve_host_ids:
        try:
            pve = build_pve_client(db, hid)
            su, sp, sk = get_ssh_creds(pve)
            sh = pve.host

            if pve_storage_id:
                jlog.log(f"[{sh}] Removing PVE storage '{pve_storage_id}' …")
                ssh_run(sh, su, sp,
                        f"pvesm remove {shlex.quote(pve_storage_id)} 2>/dev/null; true",
                        key_material=sk, timeout=30)
                jlog.log(f"[{sh}] PVE storage removed.")

            if vg_name:
                vg_q = shlex.quote(vg_name)
                jlog.log(f"[{sh}] Deactivating VG '{vg_name}' …")
                ssh_run(sh, su, sp,
                        f"vgchange -an {vg_q} 2>/dev/null; "
                        f"udevadm settle --timeout=5 2>/dev/null; true",
                        key_material=sk, timeout=30)
                jlog.log(f"[{sh}] VG deactivated.")

            # Disconnect while VG is still registered so pvs can find the device.
            # If the VG doesn't exist, fall back to disconnect-by-subsystem-NQN.
            jlog.log(f"[{sh}] Disconnecting NVMe controller …")
            nvme_disconnect_by_vg(sh, su, sp, sk, vg_name)
            nvme_disconnect_by_subsystem_name(sh, su, sp, sk, subsystem_name)
            jlog.log(f"[{sh}] NVMe disconnected.")

            if vg_name:
                vg_q = shlex.quote(vg_name)
                ssh_run(sh, su, sp,
                        f"vgremove -f {vg_q} 2>/dev/null; true",
                        key_material=sk, timeout=15)
                jlog.log(f"[{sh}] VG removed.")

        except Exception as exc:
            jlog.log(f"WARNING: cannot reach host {hid}: {exc}")

    # ── ONTAP cleanup ─────────────────────────────────────────────────────────
    if ns_uuid and subsystem_uuid:
        jlog.log("Unmapping namespace from subsystem …")
        try:
            client.remove_nvme_namespace_from_subsystem(subsystem_uuid, ns_uuid)
            jlog.log("Namespace unmapped.")
        except Exception as exc:
            jlog.log(f"WARNING: unmap namespace: {exc}")

    if delete_ontap:
        if ns_uuid:
            jlog.log(f"Deleting NVMe namespace {ns_uuid} …")
            try:
                client.delete_namespace(ns_uuid)
                jlog.log("Namespace deleted.")
            except Exception as exc:
                jlog.log(f"WARNING: namespace delete: {exc}")
        if volume_uuid:
            jlog.log(f"Deleting volume {volume_uuid} …")
            try:
                client.delete_volume(volume_uuid)
                jlog.log("Volume deleted.")
            except Exception as exc:
                jlog.log(f"WARNING: volume delete: {exc}")
        if subsystem_uuid:
            jlog.log(f"Deleting NVMe subsystem {subsystem_uuid} …")
            try:
                client.delete_nvme_subsystem(subsystem_uuid)
                jlog.log("Subsystem deleted.")
            except Exception as exc:
                jlog.log(f"WARNING: subsystem delete: {exc}")


# ── NVMe: add single host ─────────────────────────────────────────────────────

def _add_host_nvme(ds_id, ds, host_id, db, jlog):
    endpoint_id    = ds["endpoint_id"]
    svm_name       = ds.get("svm_name", "")
    subsystem_uuid = ds.get("subsystem_uuid", "")
    ns_uuid        = ds.get("ns_uuid", "")
    vg_name        = ds.get("vg_name", "")
    lvm_type       = ds.get("lvm_type", "linear")
    lvm_pool_name  = ds.get("lvm_pool_name", "") or "data"
    pve_storage_id = ds.get("pve_storage_id", "")
    volume_uuid    = ds.get("volume_uuid", "")
    volume_name    = ds.get("volume_name", "")

    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)

    pve = build_pve_client(db, host_id)
    su, sp, sk = get_ssh_creds(pve)
    sh = pve.host

    # 1. Collect NQN
    jlog.log(f"[{sh}] Collecting NVMe host NQN …")
    nqn = get_nvme_host_nqn(sh, su, sp, sk)
    if not nqn:
        raise RuntimeError(f"No NQN found on host {sh}")
    jlog.log(f"[{sh}] NQN: {nqn}")

    # 2. Add NQN to ONTAP subsystem (idempotent)
    if subsystem_uuid:
        try:
            existing_sub  = client.get_nvme_subsystem(subsystem_uuid)
            existing_nqns = {h.get("nqn", "") for h in (existing_sub.get("hosts") or [])}
            if nqn not in existing_nqns:
                client.add_nvme_host_to_subsystem(subsystem_uuid, nqn)
                jlog.log(f"Host NQN added to subsystem.")
            else:
                jlog.log(f"Host NQN already in subsystem.")
        except Exception as exc:
            jlog.log(f"WARNING: add NQN to subsystem: {exc}")

    # 3. Get subsystem NQN + LIFs (same pattern as initial provisioning)
    lif_ips = []
    subsystem_nqn = ""
    try:
        lif_ips = client.get_nvme_lifs_for_svm(svm_name)
    except Exception:
        pass
    if subsystem_uuid:
        try:
            sub_info = client.get_nvme_subsystem(subsystem_uuid)
            subsystem_nqn = sub_info.get("target_nqn", "")
        except Exception:
            pass
    if subsystem_nqn:
        jlog.log(f"Subsystem NQN: {subsystem_nqn}")
    else:
        jlog.log("WARNING: could not retrieve subsystem NQN — falling back to connect-all")

    # Ensure discovery.conf has entries for these LIFs (persistent after reboot)
    if lif_ips:
        added = ensure_nvme_discovery_entries(sh, su, sp, sk, lif_ips)
        if added:
            jlog.log(f"[{sh}] Added {added} LIF(s) to /etc/nvme/discovery.conf")

    # 4. Connect NVMe
    jlog.log(f"[{sh}] Capturing NVMe device baseline …")
    devices_before = nvme_list_devices(sh, su, sp, sk)
    if subsystem_nqn and lif_ips:
        jlog.log(f"[{sh}] Connecting NVMe (direct per-LIF) …")
        nvme_connect_to_subsystem(sh, su, sp, sk, lif_ips, subsystem_nqn)
    else:
        jlog.log(f"[{sh}] Connecting NVMe (connect-all fallback) …")
        nvme_connect_all(sh, su, sp, sk)
    jlog.log(f"[{sh}] Waiting for NVMe namespace device …")
    if subsystem_nqn:
        device = find_nvme_device_for_subsystem_nqn(
            sh, su, sp, sk, subsystem_nqn, timeout_s=90, devices_before=devices_before)
    else:
        device = find_new_nvme_device(sh, su, sp, sk, devices_before, timeout_s=90)
    jlog.log(f"[{sh}] Device ready: {device}")

    # 4. pvscan to activate existing VG on this host
    jlog.log(f"[{sh}] Activating VG via pvscan …")
    ssh_run(sh, su, sp,
            f"pvscan --cache -aay {shlex.quote(device)} 2>/dev/null; true",
            key_material=sk, timeout=30)
    jlog.log(f"[{sh}] pvscan done.")

    # 5. pvesm (if not yet in this host's storage config)
    if pve_storage_id:
        vg_q  = shlex.quote(vg_name)
        sid_q = shlex.quote(pve_storage_id)
        try:
            check = ssh_run(sh, su, sp,
                            f"pvesm status {shlex.quote(pve_storage_id)} 2>/dev/null"
                            f" && echo EXISTS || echo MISSING",
                            capture=True, key_material=sk)
            if "EXISTS" not in check:
                jlog.log(f"[{sh}] Registering PVE storage …")
                if lvm_type == "thin":
                    pvesm_cmd = (f"pvesm add lvmthin {sid_q} --vgname {vg_q}"
                                 f" --thinpool {shlex.quote(lvm_pool_name)}"
                                 f" --shared 1 --content images,rootdir")
                else:
                    pvesm_cmd = (f"pvesm add lvm {sid_q} --vgname {vg_q}"
                                 f" --shared 1 --content images,rootdir")
                ssh_run(sh, su, sp, pvesm_cmd, key_material=sk, timeout=30)
                jlog.log(f"[{sh}] PVE storage registered.")
            else:
                jlog.log(f"[{sh}] PVE storage already in cluster config.")
        except Exception as exc:
            jlog.log(f"[{sh}] WARNING: pvesm: {exc}")

    # 6. Register volume_mapping
    ns_name = ""
    try:
        ns_info = client.get_namespace(ns_uuid)
        ns_name = ((ns_info.get("location") or {}).get("namespace") or
                   ns_info.get("name", "").split("/")[-1])
    except Exception:
        pass
    existing_map_nvme = db.query_one(
        "SELECT snapshot_locking_enabled FROM netapp_volume_mapping WHERE volume_uuid=? LIMIT 1",
        (volume_uuid,))
    _snap_lock_nvme = (dict(existing_map_nvme) if existing_map_nvme else {}).get("snapshot_locking_enabled", 0)
    mid = str(_uuid.uuid4())
    try:
        db.execute(
            """INSERT INTO netapp_volume_mapping
               (id, endpoint_id, pve_cluster_id, pve_storage_id, svm_name,
                volume_uuid, volume_name, junction_path, nfs_export_ip,
                nfs_mount_path, discovered_at,
                storage_protocol, lun_uuid, lun_path,
                lvm_vg_name, lvm_type, lvm_pool_name,
                snapinfo_initialized, snapinfo_lv_name,
                snapshot_locking_enabled, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(pve_cluster_id, pve_storage_id) DO UPDATE SET
               endpoint_id=excluded.endpoint_id, svm_name=excluded.svm_name,
               volume_uuid=excluded.volume_uuid, volume_name=excluded.volume_name,
               lun_uuid=excluded.lun_uuid, lun_path=excluded.lun_path,
               lvm_vg_name=excluded.lvm_vg_name, lvm_type=excluded.lvm_type,
               lvm_pool_name=excluded.lvm_pool_name,
               storage_protocol=excluded.storage_protocol,
               snapinfo_initialized=excluded.snapinfo_initialized,
               snapshot_locking_enabled=excluded.snapshot_locking_enabled,
               discovered_at=excluded.discovered_at""",
            (mid, endpoint_id, host_id, pve_storage_id, svm_name,
             volume_uuid, volume_name, "", "", "", _now(),
             "nvme", ns_uuid, f"/vol/{volume_name}/{ns_name}",
             vg_name, lvm_type, lvm_pool_name,
             1, "netapp_snapmanifest", _snap_lock_nvme, _now()),
        )
        jlog.log("Volume mapping registered.")
    except Exception as exc:
        jlog.log(f"WARNING: volume mapping: {exc}")

    # 7. Update pve_host_ids
    current_ids = json.loads(ds.get("pve_host_ids") or "[]")
    if host_id not in current_ids:
        current_ids.append(host_id)
        db.execute(
            "UPDATE netapp_provisioned_datastores SET pve_host_ids=?, updated_at=? WHERE id=?",
            (json.dumps(current_ids), _now(), ds_id),
        )
        jlog.log(f"Host {host_id} added to datastore record.")

    # ── Access test ───────────────────────────────────────────────────────────────
    jlog.log(f"[{sh}] Running NVMe access test …")
    _at_cmds = []
    if subsystem_nqn:
        _at_cmds.append(
            f"(nvme list-subsys 2>/dev/null || nvme show-topology 2>/dev/null)"
            f" | grep -q {shlex.quote(subsystem_nqn[:64])}"
            f" || {{ echo 'NVMe subsystem not connected'; exit 1; }}"
        )
    if vg_name:
        _at_cmds.append(
            f"vgs --noheadings {shlex.quote(vg_name)} >/dev/null 2>&1"
            f" || {{ echo 'VG not active'; exit 1; }}"
        )
    _at_cmds.append("echo OK")
    _at_out = ssh_run(sh, su, sp, "; ".join(_at_cmds), capture=True, key_material=sk, timeout=20)
    if "OK" not in _at_out:
        raise RuntimeError(
            f"NVMe access test failed on {sh} — subsystem or VG not active: {_at_out.strip()}"
        )
    jlog.log(f"[{sh}] NVMe access test passed.")


# ── NVMe: remove single host ──────────────────────────────────────────────────

def _remove_host_nvme(ds_id, ds, host_id, db, jlog):
    endpoint_id    = ds["endpoint_id"]
    svm_name       = ds.get("svm_name", "")
    vg_name        = ds.get("vg_name", "")
    pve_storage_id = ds.get("pve_storage_id", "")
    subsystem_uuid = ds.get("subsystem_uuid", "")
    subsystem_name = ds.get("subsystem_name", "")

    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)

    try:
        pve = build_pve_client(db, host_id)
        su, sp, sk = get_ssh_creds(pve)
        sh = pve.host

        if pve_storage_id:
            ssh_run(sh, su, sp,
                    f"pvesm remove {shlex.quote(pve_storage_id)} 2>/dev/null; true",
                    key_material=sk, timeout=30)

        if vg_name:
            vg_q = shlex.quote(vg_name)
            ssh_run(sh, su, sp,
                    f"vgchange -an {vg_q} 2>/dev/null; udevadm settle --timeout=5 2>/dev/null; true",
                    key_material=sk, timeout=30)

        nvme_disconnect_by_vg(sh, su, sp, sk, vg_name)
        nvme_disconnect_by_subsystem_name(sh, su, sp, sk, subsystem_name)

        # Remove host NQN from subsystem
        if subsystem_uuid:
            nqn = get_nvme_host_nqn(sh, su, sp, sk)
            if nqn:
                try:
                    client.remove_nvme_host_from_subsystem(subsystem_uuid, nqn)
                    jlog.log(f"Removed host NQN {nqn} from subsystem.")
                except Exception as exc:
                    jlog.log(f"WARNING: remove NQN from subsystem: {exc}")
    except Exception as exc:
        jlog.log(f"WARNING: cannot reach host {host_id}: {exc}")

    _remove_host_from_ds_record(ds_id, host_id, pve_storage_id, db, jlog)


# ── NFS helpers ───────────────────────────────────────────────────────────────

def _nfs_same_subnet(ip1, ip2, prefix_len):
    """True if ip1 and ip2 fall within the same /prefix_len network."""
    try:
        net = ipaddress.IPv4Network(f"{ip1}/{prefix_len}", strict=False)
        return ipaddress.IPv4Address(ip2) in net
    except Exception:
        return False


def _check_nfs_lif_reachability(pve_host_ids, nfs_lif_ip, db, jlog=None):
    """Ping + NFS port check from each PVE host to nfs_lif_ip.

    When pve.nfs_ip is configured the checks are bound to that IP so results
    reflect actual NFS VLAN reachability rather than management-network routing.

    Returns a list of dicts:
        host, host_id, client_ip, nfs_ip_configured,
        same_subnet, routed, ping_ok, nfs_port_ok, error
    """
    results = []
    for hid in pve_host_ids:
        entry = {
            "host_id": hid, "host": str(hid),
            "client_ip": "", "nfs_ip_configured": False,
            "same_subnet": False, "routed": False,
            "ping_ok": False, "nfs_port_ok": False,
        }
        try:
            pve = build_pve_client(db, hid)
            su, sp, sk = get_ssh_creds(pve)
            entry["host"] = pve.host
            ip_q = shlex.quote(nfs_lif_ip)

            if pve.nfs_ip:
                entry["nfs_ip_configured"] = True
                entry["client_ip"] = pve.nfs_ip
                nfs_q = shlex.quote(pve.nfs_ip)
                # Discover prefix length so we can test same-subnet vs. routed.
                # Ping and nc are bound (-I / -s) to the NFS VLAN IP so the result
                # reflects what ONTAP will actually see, not the management path.
                cmd = (
                    f"prefix=$(ip addr show"
                    f" | grep -oP 'inet {nfs_q}/\\K[0-9]+' | head -1);"
                    f"echo \"PREFIX:${{prefix:-}}\";"
                    f"ping -c2 -W3 -I {nfs_q} {ip_q} >/dev/null 2>&1"
                    f" && echo PING_OK || echo PING_FAIL;"
                    f"nc -s {nfs_q} -zw3 {ip_q} 2049 >/dev/null 2>&1"
                    f" && echo NFS_OK || echo NFS_FAIL"
                )
                out = ssh_run(pve.host, su, sp, cmd, capture=True,
                              key_material=sk, timeout=25)
                for line in out.splitlines():
                    line = line.strip()
                    if line.startswith("PREFIX:"):
                        val = line[7:].strip()
                        if val.isdigit():
                            entry["same_subnet"] = _nfs_same_subnet(
                                pve.nfs_ip, nfs_lif_ip, int(val))
                            entry["routed"] = not entry["same_subnet"]
                    elif line == "PING_OK":
                        entry["ping_ok"] = True
                    elif line == "NFS_OK":
                        entry["nfs_port_ok"] = True
            else:
                # No NFS IP configured — fall back to unbound check so we at least
                # know whether L3 connectivity exists at all.
                cmd = (
                    f"ping -c2 -W3 {ip_q} >/dev/null 2>&1 && echo PING_OK || echo PING_FAIL;"
                    f"nc -zw3 {ip_q} 2049 >/dev/null 2>&1 && echo NFS_OK || echo NFS_FAIL"
                )
                out = ssh_run(pve.host, su, sp, cmd, capture=True,
                              key_material=sk, timeout=25)
                for line in out.splitlines():
                    line = line.strip()
                    if line == "PING_OK":
                        entry["ping_ok"] = True
                    elif line == "NFS_OK":
                        entry["nfs_port_ok"] = True

        except Exception as exc:
            entry["error"] = str(exc)
            if jlog:
                jlog.log(f"  WARNING: connectivity check for host {hid}: {exc}")
        results.append(entry)
    return results


def _prov_check_nfs_lif():
    """GET provisioning/check-nfs-lif?nfs_lif_ip=<ip>[&pve_host_ids=1,2,3]

    Tests whether PVE hosts can reach the selected NFS LIF IP (ping + port 2049).
    If pve_host_ids is omitted, checks all configured PVE hosts.
    """
    err = _require_admin()
    if err:
        return err
    from flask import request
    nfs_lif_ip     = request.args.get("nfs_lif_ip", "").strip()
    pve_host_ids_s = request.args.get("pve_host_ids", "").strip()
    if not nfs_lif_ip:
        return {"error": "nfs_lif_ip required"}, 400
    db = get_db()
    if pve_host_ids_s:
        pve_host_ids = [int(x) for x in pve_host_ids_s.split(",")
                        if x.strip().lstrip("-").isdigit()]
    else:
        rows = db.query("SELECT id FROM netapp_pve_hosts ORDER BY id")
        pve_host_ids = [r["id"] for r in rows]
    if not pve_host_ids:
        return {"error": "No PVE hosts configured"}, 400
    results = _check_nfs_lif_reachability(pve_host_ids, nfs_lif_ip, db)
    return {"results": results}


# ── NFS: provision ────────────────────────────────────────────────────────────

def _grant_nfs_access(client, svm_name, policy_id, pve_host_ids, db, jlog):
    """Adds RW export rules to `policy_id` for each PVE host's NFS VLAN IP, and
    ensures the SVM root volume allows RO traversal from those IPs (ONTAP NFS
    requires namespace traversal on '/' or every mount fails with "access
    denied" even when the datastore volume's own policy is correct).

    Used both when creating a brand-new volume (its freshly-created policy)
    and when reusing an existing volume (its current policy) — a volume
    imported from another hypervisor typically only has the OLD hosts in its
    export policy, so this must run for both cases or the new PVE hosts
    simply won't have access.
    """
    if not policy_id:
        jlog.log("WARNING: no export policy id resolved — PVE hosts may not have "
                 "access to this volume. Add export rules manually if the mount fails.")
        return
    client_ips_for_root = []
    for hid in pve_host_ids:
        try:
            pve = build_pve_client(db, hid)
            if not pve.nfs_ip:
                raise RuntimeError(
                    f"NFS IP not configured for host '{pve.host}'. "
                    f"Set the NFS network IP in Hosts settings before provisioning NFS datastores."
                )
            client_ip = pve.nfs_ip
            client.add_nfs_export_rule_rw(policy_id, client_ip)
            jlog.log(f"Export rule added for {client_ip}")
            client_ips_for_root.append(client_ip)
        except Exception as exc:
            jlog.log(f"  WARNING: export rule for host {hid}: {exc}")

    if client_ips_for_root:
        try:
            added = client.ensure_svm_root_accessible(svm_name, client_ips_for_root)
            if added:
                jlog.log(f"SVM root volume: added RO rules for {', '.join(added)}")
            else:
                jlog.log("SVM root volume: RO access already configured.")
        except Exception as exc:
            jlog.log(f"  WARNING: could not check SVM root export policy: {exc}")


def _provision_nfs(ds_id, params, db, jlog):
    endpoint_id    = params["endpoint_id"]
    svm_name       = params.get("svm_name", "")
    name           = params.get("name", ds_id)
    pve_storage_id = params.get("pve_storage_id", "")
    pve_host_ids   = params["pve_host_ids"]
    size_bytes     = int(params.get("size_bytes", 0))
    aggregate_name = params.get("aggregate_name", "") or None
    aggregate_names = [a for a in (params.get("aggregate_names") or []) if a] or None
    junction_path  = params.get("nfs_junction_path", "") or f"/{name.replace(' ', '-').lower()}"
    volume_name    = _ontap_safe_name(params.get("volume_name", ""))
    nfs_lif_ip_sel = params.get("nfs_lif_ip", "").strip()   # user-selected LIF IP

    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)

    # ── ONTAP: Volume with NFS export ─────────────────────────────────────────

    # Get NFS LIF IP — prefer user selection, fall back to first available LIF
    nfs_ip = nfs_lif_ip_sel or client.get_nfs_lif_for_svm(svm_name)
    if not nfs_ip:
        raise RuntimeError(f"No NFS LIF found for SVM '{svm_name}'")
    jlog.log(f"NFS LIF: {nfs_ip}")

    # ── Connectivity pre-check: abort before touching ONTAP if LIF is unreachable ─
    jlog.log(f"Checking NFS LIF reachability from PVE hosts …")
    reach = _check_nfs_lif_reachability(pve_host_ids, nfs_ip, db, jlog)
    ok_hosts    = [r for r in reach if r.get("ping_ok")]
    bad_hosts   = [r for r in reach if not r.get("ping_ok")]
    no_nfs_port = [r for r in ok_hosts if not r.get("nfs_port_ok")]
    for r in ok_hosts:
        if r.get("client_ip"):
            route = " · direct" if r.get("same_subnet") else " · routed via mgmt ⚠"
            src = f" (NFS IP: {r['client_ip']}{route})"
        else:
            src = " (NFS IP not configured)"
        nfs = " · NFS port open" if r.get("nfs_port_ok") else " · NFS port CLOSED"
        jlog.log(f"  ✓ {r['host']}{src}{nfs}")
    for r in bad_hosts:
        cfg = "" if r.get("nfs_ip_configured") else " — NFS IP not configured"
        jlog.log(f"  ✗ {r['host']}{cfg} — UNREACHABLE (wrong VLAN / LIF?)")
    if no_nfs_port:
        hosts_str = ", ".join(r["host"] for r in no_nfs_port)
        jlog.log(f"  WARNING: NFS port 2049 not responding on {nfs_ip} from: {hosts_str}"
                 f" — NFS may not be enabled on this SVM LIF.")
    if not ok_hosts:
        hosts_str = ", ".join(r["host"] for r in bad_hosts)
        raise RuntimeError(
            f"NFS LIF {nfs_ip} is not reachable (ping) from any PVE host "
            f"({hosts_str}). Select the correct NFS LIF for your storage VLAN."
        )

    volume_uuid = params.get("volume_uuid", "")
    snap_locking_enabled = 0
    if not volume_uuid:
        if aggregate_names:
            ag_info = f" as a FlexGroup on aggregates: {', '.join(aggregate_names)}"
        elif aggregate_name:
            ag_info = f" on aggregate '{aggregate_name}'"
        else:
            ag_info = " (auto-placement)"
        jlog.log(f"Creating NFS volume '{volume_name}' ({size_bytes} bytes)"
                 f" junction='{junction_path}'{ag_info} …")
        # Advanced option: reuse an existing export policy instead of creating a
        # dedicated one — e.g. an admin-curated policy already scoped to the
        # right clients. Falls through to the create-new path when unset (the
        # default, unchanged behavior).
        existing_policy_name = (params.get("export_policy_name") or "").strip()
        if existing_policy_name:
            policy_name = existing_policy_name
            policy_id   = params.get("export_policy_id") or None
            jlog.log(f"Using existing export policy '{policy_name}' (id={policy_id}).")
        else:
            policy_name = f"nasnap-pol-{name.replace(' ', '-').lower()}"[:64]
            try:
                policy_id = client.create_export_policy(svm_name, policy_name)
                jlog.log(f"Export policy '{policy_name}' created (id={policy_id}).")
            except Exception as exc:
                jlog.log(f"WARNING: could not create export policy, using default: {exc}")
                policy_name = "default"
                policy_id   = None

        # Add rw export rules using the configured NFS VLAN IP of each PVE host.
        # pve.nfs_ip must be set to the dedicated NFS network interface IP — using
        # any other IP (management, iSCSI, NVMe) will cause ONTAP to deny the mount.
        _grant_nfs_access(client, svm_name, policy_id, pve_host_ids, db, jlog)

        volume_uuid = client.create_volume_nfs(svm_name, volume_name, size_bytes,
                                               junction_path,
                                               aggregate_name=aggregate_name,
                                               aggregate_names=aggregate_names,
                                               export_policy=policy_name)
        jlog.log(f"Volume created: {volume_uuid}")
        if aggregate_names:
            db.execute(
                "UPDATE netapp_provisioned_datastores SET is_flexgroup=1 WHERE id=?", (ds_id,))
        try:
            client.enable_inline_compression(volume_uuid)
            jlog.log("Inline compression enabled.")
        except Exception as _ce:
            jlog.log(f"NOTE: inline compression not set ({_ce}) — AFF/ASA enable it by default.")
        if params.get("enable_snapshot_locking"):
            try:
                client.enable_snapshot_locking(volume_uuid)
                snap_locking_enabled = 1
                jlog.log("Snapshot locking enabled on volume (Tamperproof-ready).")
            except Exception as _se:
                jlog.log(f"WARNING: could not enable snapshot locking: {_se}")
    else:
        # Reuse as-is — never recreated/reformatted. This is the path for volumes
        # migrated in from another hypervisor (e.g. old VMware NFS datastores):
        # mounting them here (instead of copying their VM disks onto a fresh
        # volume) avoids duplicating that data during migration.
        vol_info       = client.get_volume(volume_uuid)
        volume_name    = vol_info.get("name", volume_name)
        snap_locking_enabled = 1 if (vol_info.get("snaplock") or {}).get("snapshot_locking_enabled") else 0
        export_info    = client.get_volume_export_info(volume_uuid)
        junction_path  = export_info.get("junction_path", junction_path)
        policy_id      = export_info.get("export_policy_id", "")
        policy_name    = export_info.get("export_policy_name", "default")
        jlog.log(f"Using existing NFS volume: {volume_name}  junction={junction_path}"
                 + (" [snapshot locking enabled]" if snap_locking_enabled else ""))

        # The volume's existing export policy typically only lists the OLD
        # hosts (e.g. VMware ESXi) — grant the new PVE hosts access too, same
        # as a freshly-created volume gets above.
        _grant_nfs_access(client, svm_name, policy_id, pve_host_ids, db, jlog)

    # Persist ONTAP IDs
    db.execute(
        """UPDATE netapp_provisioned_datastores
           SET volume_uuid=?, volume_name=?, nfs_junction_path=?, updated_at=?
           WHERE id=?""",
        (volume_uuid, volume_name, junction_path, _now(), ds_id),
    )

    # ── PVE: register storage — NFS is cluster-wide, run pvesm add once ─────
    # In a PVE cluster, pvesm add writes to /etc/pve/storage.cfg (pmxcfs),
    # which propagates automatically. Run on the first host only.
    pve_content  = params.get("pve_content", "images,rootdir") or "images,rootdir"
    nfs_nconnect = int(params.get("nfs_nconnect", 0) or 0)
    nfs_options  = "vers=3"
    if 1 <= nfs_nconnect <= 16:
        nfs_options += f",nconnect={nfs_nconnect}"
    sid_q = shlex.quote(pve_storage_id)
    pvesm_cmd = (f"pvesm add nfs {sid_q}"
                 f" --server {shlex.quote(nfs_ip)}"
                 f" --export {shlex.quote(junction_path)}"
                 f" --content {shlex.quote(pve_content)}"
                 f" --options {shlex.quote(nfs_options)}")

    _pvesm_ok = False
    try:
        pve = build_pve_client(db, pve_host_ids[0])
        su, sp, sk = get_ssh_creds(pve)
        sh = pve.host
        jlog.log(f"[{sh}] Registering NFS storage '{pve_storage_id}' …")
        check = ssh_run(sh, su, sp,
                        f"pvesm status {shlex.quote(pve_storage_id)} 2>/dev/null"
                        f" && echo EXISTS || echo MISSING",
                        capture=True, key_material=sk)
        if "EXISTS" not in check:
            # ONTAP can take a few seconds to propagate the export policy to its
            # NFS daemon after volume creation. Retry up to 3 times with backoff
            # so a transient "access denied" doesn't fail the whole provisioning.
            _pvesm_exc = None
            for _attempt in range(3):
                if _attempt > 0:
                    wait = _attempt * 5
                    jlog.log(f"[{sh}] NFS export not ready yet — retrying in {wait}s …")
                    time.sleep(wait)
                try:
                    ssh_run(sh, su, sp, pvesm_cmd, key_material=sk, timeout=120)
                    _pvesm_exc = None
                    break
                except Exception as _e:
                    _pvesm_exc = _e
                    if "access denied" not in str(_e).lower() and "mount error" not in str(_e).lower():
                        break  # non-transient error, no point retrying
            if _pvesm_exc:
                raise _pvesm_exc
            jlog.log(f"[{sh}] PVE storage registered.")
        else:
            jlog.log(f"[{sh}] PVE storage already exists.")
        _pvesm_ok = True
        # Pre-create snapmanifest directory so NFS snapshots work immediately
        manifest_dir = shlex.quote(f"/mnt/pve/{pve_storage_id}/.netapp-snapmanifest")
        try:
            ssh_run(sh, su, sp, f"mkdir -p {manifest_dir}", key_material=sk, timeout=15)
            jlog.log(f"[{sh}] Snapmanifest directory created.")
        except Exception as md_exc:
            jlog.log(f"[{sh}] WARNING: could not create snapmanifest dir: {md_exc}")
    except Exception as exc:
        jlog.log(f"ERROR: pvesm add failed: {exc}")
        jlog.log("The ONTAP volume was created. Fix the NFS export/LIF and use 'Add Host' to retry the PVE registration.")
        _set_ds_status(db, ds_id, "error", str(exc))
        return

    if _pvesm_ok:
        _set_ds_status(db, ds_id, "active")

    # ── Register volume_mapping ───────────────────────────────────────────────
    now = _now()
    for hid in pve_host_ids:
        mid = str(_uuid.uuid4())
        try:
            db.execute(
                """INSERT INTO netapp_volume_mapping
                   (id, endpoint_id, pve_cluster_id, pve_storage_id, svm_name,
                    volume_uuid, volume_name, junction_path, nfs_export_ip,
                    nfs_mount_path, discovered_at, storage_protocol,
                    lun_uuid, lun_path, lvm_vg_name, lvm_type, lvm_pool_name,
                    snapinfo_initialized, snapinfo_lv_name,
                    snapshot_locking_enabled, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(pve_cluster_id, pve_storage_id) DO UPDATE SET
                   endpoint_id=excluded.endpoint_id, svm_name=excluded.svm_name,
                   volume_uuid=excluded.volume_uuid, volume_name=excluded.volume_name,
                   junction_path=excluded.junction_path, nfs_export_ip=excluded.nfs_export_ip,
                   storage_protocol=excluded.storage_protocol,
                   snapshot_locking_enabled=excluded.snapshot_locking_enabled,
                   discovered_at=excluded.discovered_at""",
                (mid, endpoint_id, hid, pve_storage_id, svm_name,
                 volume_uuid, volume_name, junction_path, nfs_ip,
                 f"/mnt/pve/{pve_storage_id}", now, "nfs",
                 "", "", "", "", "", 0, "", snap_locking_enabled, _now()),
            )
            jlog.log(f"Volume mapping registered for host {hid}.")
        except Exception as exc:
            jlog.log(f"WARNING: volume mapping for host {hid}: {exc}")

    jlog.log(f"NFS provisioning complete. Datastore '{name}' is active.")


# ── NFS: remove ───────────────────────────────────────────────────────────────

def _remove_nfs(ds_id, ds, delete_ontap, db, jlog):
    endpoint_id    = ds["endpoint_id"]
    svm_name       = ds.get("svm_name", "")
    pve_storage_id = ds.get("pve_storage_id", "")
    pve_host_ids   = json.loads(ds.get("pve_host_ids") or "[]")
    volume_uuid    = ds.get("volume_uuid", "")
    junction_path  = ds.get("nfs_junction_path", "")

    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)

    # ── Per-host: remove PVE storage entry ───────────────────────────────────
    for hid in pve_host_ids:
        try:
            pve = build_pve_client(db, hid)
            su, sp, sk = get_ssh_creds(pve)
            sh = pve.host
            if pve_storage_id:
                jlog.log(f"[{sh}] Removing PVE storage '{pve_storage_id}' …")
                ssh_run(sh, su, sp,
                        f"pvesm remove {shlex.quote(pve_storage_id)} 2>/dev/null; true",
                        key_material=sk, timeout=30)
                jlog.log(f"[{sh}] PVE storage removed.")
        except Exception as exc:
            jlog.log(f"WARNING: cannot reach host {hid}: {exc}")

    # ── ONTAP cleanup ─────────────────────────────────────────────────────────
    if delete_ontap and volume_uuid:
        # Unmount (remove junction path) before deleting
        try:
            jlog.log("Unmounting NFS volume …")
            client.unmount_volume(volume_uuid)
            jlog.log("Volume unmounted.")
        except Exception as exc:
            jlog.log(f"WARNING: unmount: {exc}")

        # Delete the export policy if it was created by provisioning
        try:
            export_info = client.get_volume_export_info(volume_uuid)
            policy_name = export_info.get("export_policy_name", "")
            if policy_name and (policy_name.startswith("nasnap-pol-") or policy_name.startswith("pgxpol-")):
                policy_id = export_info.get("export_policy_id", 0)
                if policy_id:
                    client.set_volume_export_policy(volume_uuid, "default")
                    client.delete_export_policy(policy_id)
                    jlog.log(f"Export policy '{policy_name}' deleted.")
        except Exception as exc:
            jlog.log(f"WARNING: export policy cleanup: {exc}")

        jlog.log(f"Deleting NFS volume {volume_uuid} …")
        try:
            client.delete_volume(volume_uuid)
            jlog.log("Volume deleted.")
        except Exception as exc:
            jlog.log(f"WARNING: volume delete: {exc}")


# ── NFS: add/remove single host ───────────────────────────────────────────────

def _add_host_nfs(ds_id, ds, host_id, db, jlog):
    endpoint_id    = ds["endpoint_id"]
    svm_name       = ds.get("svm_name", "")
    pve_storage_id = ds.get("pve_storage_id", "")
    volume_uuid    = ds.get("volume_uuid", "")
    volume_name    = ds.get("volume_name", "")
    junction_path  = ds.get("nfs_junction_path", "")

    endpoint = get_endpoint(db, endpoint_id)
    client   = build_ontap_client(endpoint)

    nfs_ip = client.get_nfs_lif_for_svm(svm_name)
    if not nfs_ip:
        raise RuntimeError(f"No NFS LIF found for SVM '{svm_name}'")

    # Add host IP to export policy + ensure SVM root RO access.
    # pve.nfs_ip must be the dedicated NFS network IP — any other IP will be rejected.
    pve = build_pve_client(db, host_id)
    if not pve.nfs_ip:
        raise RuntimeError(
            f"NFS IP not configured for host '{pve.host}'. "
            f"Set the NFS network IP in Hosts settings before adding NFS hosts."
        )
    client_ip = pve.nfs_ip
    try:
        export_info = client.get_volume_export_info(volume_uuid)
        policy_id   = export_info.get("export_policy_id", 0)
        if policy_id:
            # Pre-check existing rules — do not rely on 409 as proof the IP is covered.
            # ONTAP returns 409 when ANY rule in the policy has the same non-client fields
            # (ro_rule/rw_rule/superuser), which doesn't mean client_ip is actually allowed.
            existing_rules = client.list_nfs_export_rules(policy_id)
            covered = {c.get("match", "")
                       for r in existing_rules
                       for c in r.get("clients", [])}
            if client_ip not in covered:
                client.add_nfs_export_rule_rw(policy_id, client_ip)
                jlog.log(f"Export rule added for {client_ip}.")
            else:
                jlog.log(f"Export rule for {client_ip} already in policy.")
    except Exception as exc:
        jlog.log(f"WARNING: export rule: {exc}")
    try:
        added = client.ensure_svm_root_accessible(svm_name, [client_ip])
        if added:
            jlog.log(f"SVM root volume: added RO rules for {', '.join(added)}")
    except Exception as exc:
        jlog.log(f"  WARNING: could not check SVM root export policy: {exc}")

    # Register PVE storage on host.
    # In a PVE cluster, storage.cfg is shared via pmxcfs — pvesm add on any node writes
    # to the global config. On a new node, pvesm status <id> may return non-zero even when
    # the storage IS in storage.cfg (not yet mounted/active), so we check storage.cfg directly.
    pve_content  = ds.get("pve_content", "images,rootdir") or "images,rootdir"
    nfs_nconnect = int(ds.get("nfs_nconnect", 0) or 0)
    nfs_options  = "vers=3"
    if 1 <= nfs_nconnect <= 16:
        nfs_options += f",nconnect={nfs_nconnect}"
    sid_q     = shlex.quote(pve_storage_id)
    pvesm_cmd = (f"pvesm add nfs {sid_q}"
                 f" --server {shlex.quote(nfs_ip)}"
                 f" --export {shlex.quote(junction_path)}"
                 f" --content {shlex.quote(pve_content)}"
                 f" --options {shlex.quote(nfs_options)}")
    try:
        pve = build_pve_client(db, host_id)
        su, sp, sk = get_ssh_creds(pve)
        sh = pve.host
        # grep storage.cfg instead of pvesm status — status exits non-zero when storage
        # is defined but not yet mounted on a newly-joined cluster node.
        check = ssh_run(sh, su, sp,
                        f"grep -qw {sid_q} /etc/pve/storage.cfg 2>/dev/null"
                        f" && echo EXISTS || echo MISSING",
                        capture=True, key_material=sk)
        if "EXISTS" not in check:
            try:
                ssh_run(sh, su, sp, pvesm_cmd, key_material=sk, timeout=120)
                jlog.log(f"[{sh}] PVE storage registered.")
            except Exception as exc:
                if "already defined" in str(exc).lower():
                    jlog.log(f"[{sh}] PVE storage already in cluster config (idempotent).")
                else:
                    jlog.log(f"WARNING: pvesm: {exc}")
        else:
            jlog.log(f"[{sh}] PVE storage already in cluster config.")
    except Exception as exc:
        jlog.log(f"WARNING: pvesm: {exc}")

    # Register volume_mapping
    existing_map_nfs = db.query_one(
        "SELECT snapshot_locking_enabled FROM netapp_volume_mapping WHERE volume_uuid=? LIMIT 1",
        (volume_uuid,))
    _snap_lock_nfs = (dict(existing_map_nfs) if existing_map_nfs else {}).get("snapshot_locking_enabled", 0)
    mid = str(_uuid.uuid4())
    try:
        db.execute(
            """INSERT INTO netapp_volume_mapping
               (id, endpoint_id, pve_cluster_id, pve_storage_id, svm_name,
                volume_uuid, volume_name, junction_path, nfs_export_ip,
                nfs_mount_path, discovered_at, storage_protocol,
                lun_uuid, lun_path, lvm_vg_name, lvm_type, lvm_pool_name,
                snapinfo_initialized, snapinfo_lv_name,
                snapshot_locking_enabled, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(pve_cluster_id, pve_storage_id) DO UPDATE SET
               endpoint_id=excluded.endpoint_id, svm_name=excluded.svm_name,
               volume_uuid=excluded.volume_uuid, volume_name=excluded.volume_name,
               junction_path=excluded.junction_path, nfs_export_ip=excluded.nfs_export_ip,
               storage_protocol=excluded.storage_protocol,
               snapshot_locking_enabled=excluded.snapshot_locking_enabled,
               discovered_at=excluded.discovered_at""",
            (mid, endpoint_id, host_id, pve_storage_id, svm_name,
             volume_uuid, volume_name, junction_path, nfs_ip,
             f"/mnt/pve/{pve_storage_id}", _now(), "nfs",
             "", "", "", "", "", 0, "", _snap_lock_nfs, _now()),
        )
        jlog.log("Volume mapping registered.")
    except Exception as exc:
        jlog.log(f"WARNING: volume mapping: {exc}")

    current_ids = json.loads(ds.get("pve_host_ids") or "[]")
    if host_id not in current_ids:
        current_ids.append(host_id)
        db.execute(
            "UPDATE netapp_provisioned_datastores SET pve_host_ids=?, updated_at=? WHERE id=?",
            (json.dumps(current_ids), _now(), ds_id),
        )
        jlog.log(f"Host {host_id} added to datastore record.")

    # ── Access test ───────────────────────────────────────────────────────────────
    try:
        _pve_at = build_pve_client(db, host_id)
        _su_at, _sp_at, _sk_at = get_ssh_creds(_pve_at)
        _sh_at = _pve_at.host
    except Exception as exc:
        raise RuntimeError(f"NFS access test: cannot reach host — {exc}")
    jlog.log(f"[{_sh_at}] Running NFS access test …")
    _tmp = f"/tmp/.nasnap_nfstest_{int(time.time())}"
    _tmp_q = shlex.quote(_tmp)
    _cleanup = f"umount {_tmp_q} 2>/dev/null; rmdir {_tmp_q} 2>/dev/null; true"
    _mount_cmd = (
        f"mkdir -p {_tmp_q} && "
        f"timeout 20 mount -t nfs -o vers=3,ro {shlex.quote(nfs_ip)}:{shlex.quote(junction_path)} {_tmp_q} && "
        f"echo MOUNTED"
    )
    try:
        _at_out = ssh_run(_sh_at, _su_at, _sp_at, _mount_cmd, capture=True,
                          key_material=_sk_at, timeout=35)
        ssh_run(_sh_at, _su_at, _sp_at, _cleanup, key_material=_sk_at, timeout=10)
        if "MOUNTED" not in _at_out:
            raise RuntimeError(_at_out.strip() or "mount produced no output")
        jlog.log(f"[{_sh_at}] NFS access test passed.")
    except Exception as exc:
        try:
            ssh_run(_sh_at, _su_at, _sp_at, _cleanup, key_material=_sk_at, timeout=10)
        except Exception:
            pass
        raise RuntimeError(
            f"NFS access test failed on {_sh_at} — check export policy and network: {exc}"
        )


def _remove_host_nfs(ds_id, ds, host_id, db, jlog):
    pve_storage_id = ds.get("pve_storage_id", "")
    try:
        pve = build_pve_client(db, host_id)
        su, sp, sk = get_ssh_creds(pve)
        sh = pve.host
        if pve_storage_id:
            ssh_run(sh, su, sp,
                    f"pvesm remove {shlex.quote(pve_storage_id)} 2>/dev/null; true",
                    key_material=sk, timeout=30)
            jlog.log(f"[{sh}] PVE storage removed.")
    except Exception as exc:
        jlog.log(f"WARNING: cannot reach host {host_id}: {exc}")

    _remove_host_from_ds_record(ds_id, host_id, pve_storage_id, db, jlog)


# ── Shared: update DB record after host removal ───────────────────────────────

def _remove_host_from_ds_record(ds_id, host_id, pve_storage_id, db, jlog):
    """Removes host_id from pve_host_ids and cleans up volume_mapping."""
    row = db.query_one(
        "SELECT pve_host_ids FROM netapp_provisioned_datastores WHERE id=?", (ds_id,))
    if row:
        current_ids = json.loads(row["pve_host_ids"] or "[]")
        current_ids = [h for h in current_ids if h != host_id]
        db.execute(
            "UPDATE netapp_provisioned_datastores SET pve_host_ids=?, updated_at=? WHERE id=?",
            (json.dumps(current_ids), _now(), ds_id),
        )
        jlog.log(f"Host {host_id} removed from datastore record.")
    if pve_storage_id:
        db.execute(
            "DELETE FROM netapp_volume_mapping WHERE pve_cluster_id=? AND pve_storage_id=?",
            (host_id, pve_storage_id),
        )
        jlog.log("Volume mapping removed.")
