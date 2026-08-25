"""
Stale Datastore Pruning

Nothing previously checked whether a netapp_provisioned_datastores /
netapp_volume_mapping row's pve_storage_id is still registered in Proxmox.
If the storage entry was removed directly on PVE — or a ghost duplicate row
survived a "remove" click that only deleted the sibling row sharing the same
mountpoint (see the mount-existing dedup fix) — NaSnap kept showing it in the
Datastores list forever.

prune_missing_datastores() closes that gap: for every PVE host it can
actually reach, it reads the live storage list once (GET /storage — the
in-memory view of storage.cfg) and deletes DB rows whose pve_storage_id is
confirmed absent from every host it's supposed to live on. A host it cannot
reach is simply skipped for that row's check — never treated as "absent" —
so a temporary SSH/API outage can never cause a false-positive delete.
"""

import json
import logging

log = logging.getLogger(__name__)


def _live_storage_ids_by_host(db):
    """Returns {pve_host_id: set(storage_id)} for every host that answered,
    and a separate list of host ids that could not be reached."""
    from .discovery import _pve_session

    live = {}
    unreachable = []
    rows = db.query("SELECT * FROM netapp_pve_hosts") or []
    for row in rows:
        pve = dict(row)
        pve["password"] = db._decrypt(pve.pop("password_encrypted", ""))
        try:
            sess, base = _pve_session(pve)
            r = sess.get(f"{base}/storage", timeout=15)
            if r.status_code != 200:
                unreachable.append(pve["id"])
                continue
            live[pve["id"]] = {s.get("storage", "") for s in r.json().get("data", [])}
        except Exception:
            unreachable.append(pve["id"])
    return live, unreachable


def prune_missing_datastores(db, logger=None):
    """Removes datastore/mapping rows whose pve_storage_id is confirmed gone
    from every PVE host it's registered on.

    Returns {"hosts_checked", "hosts_unreachable", "datastores_removed", "mappings_removed"}.
    """
    live, unreachable = _live_storage_ids_by_host(db)

    if unreachable and logger:
        placeholders = ",".join("?" * len(unreachable))
        names = db.query(
            f"SELECT name FROM netapp_pve_hosts WHERE id IN ({placeholders})",
            tuple(unreachable),
        ) or []
        for n in names:
            logger.log(
                f"WARNING: prune: PVE host '{dict(n)['name']}' unreachable — "
                f"skipping its datastores for this pass"
            )

    datastores_removed = 0
    mappings_removed = 0

    # ── netapp_provisioned_datastores ──────────────────────────────────────
    prov_rows = db.query(
        "SELECT id, name, pve_storage_id, pve_host_ids FROM netapp_provisioned_datastores"
    ) or []
    for row in prov_rows:
        row = dict(row)
        sid = row.get("pve_storage_id", "")
        if not sid:
            continue
        try:
            host_ids = json.loads(row.get("pve_host_ids") or "[]")
        except Exception:
            host_ids = []
        checked_hosts = [h for h in host_ids if h in live]
        if not checked_hosts:
            continue  # none of its hosts were reachable this pass — can't confirm, leave it
        if any(sid in live[h] for h in checked_hosts):
            continue  # still present on at least one reachable host

        for h in host_ids:
            db.execute(
                "DELETE FROM netapp_volume_mapping WHERE pve_cluster_id=? AND pve_storage_id=?",
                (h, sid),
            )
        db.execute("DELETE FROM netapp_provisioned_datastores WHERE id=?", (row["id"],))
        datastores_removed += 1
        msg = (f"prune: removed '{row.get('name')}' (storage id '{sid}') — "
               f"no longer present in Proxmox on any of its configured hosts")
        log.info(f"[netapp_storage] {msg}")
        if logger:
            logger.log(msg)

    # ── netapp_volume_mapping rows not backed by a provisioned datastore ────
    # (auto-discovered entries — "source: discovered" in the Datastores list)
    map_rows = db.query(
        "SELECT id, pve_storage_id, pve_cluster_id FROM netapp_volume_mapping"
    ) or []
    for row in map_rows:
        row = dict(row)
        sid = row.get("pve_storage_id", "")
        hid = row.get("pve_cluster_id", "")
        if not sid or hid not in live:
            continue
        if sid in live[hid]:
            continue
        db.execute("DELETE FROM netapp_volume_mapping WHERE id=?", (row["id"],))
        mappings_removed += 1
        msg = f"prune: removed stale mapping '{sid}' — no longer present on its Proxmox host"
        log.info(f"[netapp_storage] {msg}")
        if logger:
            logger.log(msg)

    if logger:
        logger.log(
            f"Prune: {len(live)} PVE host(s) checked ({len(unreachable)} unreachable), "
            f"{datastores_removed} datastore(s) and {mappings_removed} auto-discovered "
            f"mapping(s) removed (no longer present on Proxmox)"
        )

    return {
        "hosts_checked": len(live),
        "hosts_unreachable": len(unreachable),
        "datastores_removed": datastores_removed,
        "mappings_removed": mappings_removed,
    }
