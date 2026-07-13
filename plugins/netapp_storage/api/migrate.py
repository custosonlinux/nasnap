"""
Bulk VM Migration API

  storage/bulk-migrate-start   POST  – migrate N VMs from one datastore to another
"""

import logging

from flask import request
from nasnap_core.core.db import get_db
from nasnap_core.api.plugins import register_plugin_route

from ..core.migrate_engine import start_bulk_migrate

log = logging.getLogger(__name__)
from ..core._helpers import PLUGIN_ID, get_mapping  # noqa: F401


def _require_admin():
    from flask import request
    if request.session.get("role") != "admin":
        return {"error": "Admin access required"}, 403
    return None


def _bulk_migrate_start():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}

    source_mapping_id = str(data.get("source_mapping_id", "")).strip()
    target_mapping_id = str(data.get("target_mapping_id", "")).strip()
    vmids = data.get("vmids") or []

    if not source_mapping_id or not target_mapping_id:
        return {"error": "source_mapping_id and target_mapping_id required"}, 400
    if source_mapping_id == target_mapping_id:
        return {"error": "Source and target datastore must differ"}, 400
    if not isinstance(vmids, list) or not vmids:
        return {"error": "vmids must be a non-empty list"}, 400

    try:
        max_parallel = max(1, min(8, int(data.get("max_parallel", 3))))
    except (TypeError, ValueError):
        max_parallel = 3

    db = get_db()
    try:
        get_mapping(db, source_mapping_id)
        get_mapping(db, target_mapping_id)
    except RuntimeError as exc:
        return {"error": str(exc)}, 404

    username = request.session.get("user", "system")
    job_ids = start_bulk_migrate(source_mapping_id, target_mapping_id, vmids, max_parallel, username)
    return {"success": True, "job_ids": job_ids}


def register_routes():
    register_plugin_route(PLUGIN_ID, "storage/bulk-migrate-start", _bulk_migrate_start)
