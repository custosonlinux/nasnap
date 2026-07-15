"""
NaSnap — SnapMirror/SnapVault Replication Engine

Shared, protocol-agnostic primitives for setting up and tearing down
SnapMirror replication of a NaSnap-managed volume to a second ONTAP system.
Reused by both the Provision Wizard's optional replication step and the
Datastores tab's "SnapMirror / Vault" retrofit action.

Cluster/SVM peering is fully automated: both endpoints must already be
registered in NaSnap (Settings -> NetApp Systems), since NaSnap needs admin
credentials for both sides to complete the peering handshake itself.
Peering requires intercluster LIFs to already exist on both clusters — NaSnap
cannot create those (too network/topology-specific) and will raise a clear
OntapError if they're missing.
"""

import uuid as _uuid
from datetime import datetime, timezone

from ._helpers import get_endpoint, build_ontap_client
from .ontap_client import OntapError


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_cluster_peered(source_client, source_cluster_name, dest_client, dest_cluster_name, jlog):
    """Ensures a cluster peer relationship exists between the two clusters.

    Idempotent — does nothing if already peered. Raises OntapError (with
    ONTAP's own message, e.g. missing intercluster LIF) if peering fails.
    """
    existing = source_client.get_cluster_peer_status(dest_cluster_name)
    if existing:
        jlog.log(f"  Cluster peer '{dest_cluster_name}' already exists.")
        return
    jlog.log(f"  No cluster peer found — establishing peering with '{dest_cluster_name}'…")
    passphrase, dest_lifs = dest_client.create_cluster_peer_passphrase()
    source_client.complete_cluster_peer(dest_lifs, passphrase)
    jlog.log(f"  Cluster peering with '{dest_cluster_name}' established.")


def ensure_svm_peered(source_client, source_svm, source_cluster_name,
                      dest_client, dest_svm, dest_cluster_name, jlog):
    """Ensures an SVM peer relationship exists between the two SVMs.

    Idempotent — does nothing if already peered. Initiates from the source
    side and immediately accepts on the destination side (NaSnap holds admin
    credentials for both, so no manual acceptance step is needed).
    """
    existing = source_client.get_svm_peer_status(source_svm, dest_svm, dest_cluster_name)
    if existing and existing.get("state") == "peered":
        jlog.log(f"  SVM peer '{source_svm}' <-> '{dest_svm}' already established.")
        return
    if not existing:
        jlog.log(f"  No SVM peer found — requesting peering '{source_svm}' -> '{dest_svm}'…")
        source_client.create_svm_peer(source_svm, dest_svm, dest_cluster_name)
    jlog.log(f"  Accepting SVM peer on '{dest_cluster_name}'…")
    dest_client.accept_svm_peer(source_svm, source_cluster_name)
    jlog.log(f"  SVM peering '{source_svm}' <-> '{dest_svm}' established.")


def resolve_policy(dest_client, dest_svm, policy_choice, jlog):
    """Resolves policy_choice to a policy name usable by create_snapmirror_relationship.

    policy_choice: {"existing_name": "..."} or
                   {"new": {"name", "type": "mirror"|"vault", "label", "retention_count"}}
    """
    if policy_choice.get("existing_name"):
        return policy_choice["existing_name"]
    new = policy_choice.get("new") or {}
    name = new.get("name", "").strip()
    ptype = new.get("type", "mirror")
    if not name:
        raise ValueError("Policy name is required")
    if ptype == "mirror":
        # No custom policy object needed — ONTAP's built-in MirrorAllSnapshots
        # already provides this behavior on every SVM.
        jlog.log(f"  Using built-in Mirror policy 'MirrorAllSnapshots' (no custom policy created).")
        return "MirrorAllSnapshots"
    jlog.log(f"  Creating Vault policy '{name}' (label={new.get('label')}, "
              f"retention={new.get('retention_count', 7)})…")
    dest_client.create_snapmirror_policy(
        dest_svm, name, "vault",
        snapmirror_label=new.get("label"),
        retention_count=new.get("retention_count", 7),
    )
    return name


def setup_replication(source_ep_id, source_svm, source_volume,
                      target_ep_id, target_svm, policy_choice, db, jlog):
    """Sets up SnapMirror replication of source_svm:source_volume to a second
    ONTAP system, automatically peering clusters/SVMs if needed.

    Returns the new netapp_snapmirror_relationships row (dict).
    """
    source_ep = get_endpoint(db, source_ep_id)
    target_ep = get_endpoint(db, target_ep_id)
    source_client = build_ontap_client(source_ep)
    target_client = build_ontap_client(target_ep)

    jlog.log("Checking cluster/SVM peering…")
    source_cluster_name, _, _ = source_client.test_connection()
    target_cluster_name, _, _ = target_client.test_connection()

    ensure_cluster_peered(source_client, source_cluster_name, target_client, target_cluster_name, jlog)
    ensure_svm_peered(source_client, source_svm, source_cluster_name,
                      target_client, target_svm, target_cluster_name, jlog)

    # Needed so storage/unified's SnapMirror JOIN (keyed on source_volume_uuid) picks this up.
    source_vol_uuid = ""
    try:
        for v in (source_client.get_volumes(svm_name=source_svm) or []):
            if v.get("name") == source_volume:
                source_vol_uuid = v.get("uuid", "")
                break
    except Exception:
        pass

    policy_name = resolve_policy(target_client, target_svm, policy_choice, jlog)

    source_path = f"{source_svm}:{source_volume}"
    dest_path = f"{target_svm}:{source_volume}"
    jlog.log(f"Creating SnapMirror relationship {source_path} -> {dest_path} (policy: {policy_name})…")
    # Must be created via the DESTINATION cluster's API — it provisions the destination
    # volume and resolves the policy locally. Creating it from the source side makes the
    # source cluster fetch that info over the intercluster link instead, which can fail
    # right after peering/policy creation (e.g. "Policy not found" on the destination).
    rel_uuid = target_client.create_snapmirror_relationship(
        source_path, dest_path, policy=policy_name,
        progress_cb=jlog.log, create_destination=True,
    )
    if not rel_uuid:
        raise OntapError("SnapMirror relationship was not created (no UUID returned)")

    jlog.log("Starting baseline transfer…")
    target_client.initialize_snapmirror(rel_uuid)

    # Look up the destination volume UUID for later teardown (delete_volume needs it).
    dest_vol_uuid = ""
    try:
        for v in (target_client.get_volumes(svm_name=target_svm) or []):
            if v.get("name") == source_volume:
                dest_vol_uuid = v.get("uuid", "")
                break
    except Exception:
        pass

    rid = str(_uuid.uuid4())
    now = _now()
    db.execute(
        "INSERT INTO netapp_snapmirror_relationships "
        "(id, source_endpoint_id, source_volume_uuid, source_svm, source_volume, "
        "dest_endpoint_id, dest_cluster_name, dest_svm, dest_volume, dest_volume_uuid, "
        "relationship_uuid, policy_type, policy_name, state, healthy, lag_time, "
        "last_transfer_time, last_scanned_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, source_ep_id, source_vol_uuid, source_svm, source_volume,
         target_ep_id, target_cluster_name, target_svm, source_volume, dest_vol_uuid,
         rel_uuid, "async", policy_name, "transferring", 1, "",
         "", now),
    )
    jlog.log("SnapMirror replication set up successfully.")
    row = db.query_one("SELECT * FROM netapp_snapmirror_relationships WHERE id=?", (rid,))
    return dict(row)


def teardown_replication(relationship_id, delete_destination, db, jlog):
    """Breaks and removes a SnapMirror relationship.

    If delete_destination is True, also destroys the destination volume —
    otherwise it's left in place (still readable, e.g. for a later restore).
    Always removes the local cache row regardless of outcome details.
    """
    row = db.query_one(
        "SELECT * FROM netapp_snapmirror_relationships WHERE id=?", (relationship_id,))
    if not row:
        jlog.log("No SnapMirror relationship found — nothing to do.")
        return
    rel = dict(row)
    source_ep = get_endpoint(db, rel["source_endpoint_id"])
    source_client = build_ontap_client(source_ep)

    jlog.log(f"Breaking SnapMirror relationship {rel.get('relationship_uuid', '')[:8]}…")
    try:
        source_client.snapmirror_break(rel["relationship_uuid"])
    except Exception as exc:
        jlog.log(f"  Break failed or already broken: {exc}")

    jlog.log("Removing relationship metadata…")
    try:
        source_client.delete_snapmirror_relationship(rel["relationship_uuid"])
    except Exception as exc:
        jlog.log(f"  Relationship delete failed: {exc}")

    if delete_destination and rel.get("dest_endpoint_id") and rel.get("dest_volume_uuid"):
        jlog.log(f"Deleting destination volume '{rel.get('dest_volume')}'…")
        try:
            dest_ep = get_endpoint(db, rel["dest_endpoint_id"])
            dest_client = build_ontap_client(dest_ep)
            dest_client.delete_volume(rel["dest_volume_uuid"])
            jlog.log("  Destination volume deleted.")
        except Exception as exc:
            jlog.log(f"  Destination volume delete failed: {exc}")
    elif delete_destination:
        jlog.log("  Destination volume UUID unknown — skipping delete (relationship removed only).")
    else:
        jlog.log("  Destination volume kept (not deleted).")

    db.execute("DELETE FROM netapp_snapmirror_relationships WHERE id=?", (relationship_id,))
    jlog.log("SnapMirror teardown complete.")
