"""
Snapshot Garbage Collection

NaSnap's netapp_snapshots table records every snapshot it creates, but ONTAP
rotates snapshots out on its own (retention schedules, manual deletes on the
cluster, snapmirror-driven cleanup) without ever telling NaSnap. Those DB rows
become orphans: still listed everywhere in the UI (Snapshots tab, VM history,
Single File Restore) with no corresponding snapshot on the array, so trying to
use one fails ("no disks found") instead of the row simply not existing.

gc_stale_snapshots() is the fix: compare each volume's *own* recorded rows
against ONTAP's live snapshot list for that volume, and delete rows that are
no longer there. Only ever deletes rows we can positively confirm are gone —
an unreachable/erroring ONTAP query skips that volume entirely rather than
risking a false-positive mass delete.
"""

import logging

log = logging.getLogger(__name__)


def gc_stale_snapshots(db, logger=None):
    """Removes netapp_snapshots rows whose ONTAP snapshot no longer exists.

    Groups by volume_uuid (not by mapping) since the same ONTAP volume can be
    represented by more than one netapp_volume_mapping row — a single ONTAP
    list_snapshots() call covers all of them.

    Returns {"volumes_checked", "records_checked", "removed"}.
    """
    from ._helpers import get_endpoint, build_ontap_client

    mappings = db.query(
        "SELECT vm.*, ep.id AS ep_id FROM netapp_volume_mapping vm "
        "JOIN netapp_endpoints ep ON ep.id = vm.endpoint_id"
    ) or []

    volumes_checked = records_checked = removed = 0
    seen_uuids = set()

    for m in mappings:
        m = dict(m)
        vol_uuid = m.get("volume_uuid", "")
        if not vol_uuid or vol_uuid in seen_uuids:
            continue
        seen_uuids.add(vol_uuid)

        try:
            ep = get_endpoint(db, m["endpoint_id"])
            client = build_ontap_client(ep)
            live_names = {s["name"] for s in client.list_snapshots(vol_uuid)}
        except Exception as exc:
            msg = f"GC: skipping volume '{m.get('volume_name','')}' — ONTAP unreachable: {exc}"
            log.warning(f"[netapp_storage] {msg}")
            if logger:
                logger.log(f"WARNING: {msg}")
            continue

        volumes_checked += 1
        rows = db.query(
            "SELECT s.id, s.snap_name FROM netapp_snapshots s "
            "JOIN netapp_volume_mapping vm2 ON vm2.id = s.mapping_id "
            "WHERE vm2.volume_uuid=? AND s.status='done'",
            (vol_uuid,),
        ) or []
        records_checked += len(rows)

        stale_ids = [
            r["id"] for r in rows
            if r["snap_name"] not in live_names and not r["snap_name"].startswith("snapmirror.")
        ]
        for sid in stale_ids:
            db.execute("DELETE FROM netapp_snapshots WHERE id=?", (sid,))
        removed += len(stale_ids)

    if logger:
        logger.log(
            f"GC: {volumes_checked} volume(s) checked, {records_checked} snapshot record(s) "
            f"verified against ONTAP, {removed} orphaned record(s) removed (rotated out on ONTAP)"
        )
    if removed:
        log.info(f"[netapp_storage] GC removed {removed} orphaned snapshot record(s) "
                  f"across {volumes_checked} volume(s)")

    return {"volumes_checked": volumes_checked, "records_checked": records_checked, "removed": removed}
