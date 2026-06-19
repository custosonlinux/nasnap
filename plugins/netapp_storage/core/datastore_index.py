import json
import shlex
import hashlib
import logging
from datetime import datetime, timezone

from ._helpers import ssh_run

log = logging.getLogger(__name__)

_INDEX_DIR = ".nasnap"
_INDEX_FILE = "index.json"
_INDEX_PATH = f"{_INDEX_DIR}/{_INDEX_FILE}"
SCHEMA_VERSION = 1


class DatastoreIndex:
    """Read/write .nasnap/index.json on a datastore mount path via SSH.

    The index is written before every ONTAP snapshot so it is automatically
    baked into each snapshot, making every datastore self-describing.
    """

    def __init__(self, pve_host: str, pve_user: str, pve_pass: str, pve_key: str = ""):
        self._host = pve_host
        self._user = pve_user
        self._pass = pve_pass
        self._key = pve_key

    def read(self, mount_path: str) -> dict | None:
        """Returns the parsed index dict, or None if the file does not exist."""
        idx_path = f"{mount_path}/{_INDEX_PATH}"
        sentinel = "__NASNAP_NOT_FOUND__"
        try:
            out = ssh_run(
                self._host, self._user, self._pass,
                f"cat {shlex.quote(idx_path)} 2>/dev/null || printf {shlex.quote(sentinel)}",
                capture=True,
                key_material=self._key,
            )
            if sentinel in out:
                return None
            return json.loads(out)
        except Exception as exc:
            log.warning(f"[datastore_index] read {idx_path}: {exc}")
            return None

    def write(self, mount_path: str, index: dict) -> None:
        """Atomically writes the index to the datastore mount path."""
        dir_path = f"{mount_path}/{_INDEX_DIR}"
        idx_path = f"{dir_path}/{_INDEX_FILE}"
        tmp_path = f"{idx_path}.tmp"
        data = json.dumps(index, indent=2).encode()
        ssh_run(self._host, self._user, self._pass,
                f"mkdir -p {shlex.quote(dir_path)}",
                key_material=self._key)
        ssh_run(self._host, self._user, self._pass,
                f"cat > {shlex.quote(tmp_path)}",
                stdin_data=data,
                key_material=self._key)
        ssh_run(self._host, self._user, self._pass,
                f"mv {shlex.quote(tmp_path)} {shlex.quote(idx_path)}",
                key_material=self._key)

    def add_snapshot(
        self,
        mount_path: str,
        snap_entry: dict,
        datastore_info: dict,
    ) -> None:
        """Reads the existing index, prepends the new snapshot entry, and writes back.

        Creates a fresh index if none exists yet. ``datastore_info`` is used to
        populate top-level metadata and ``vms_current`` on the index.
        """
        index = self.read(mount_path) or _new_index(datastore_info)
        index["vms_current"] = datastore_info.get("vms_current", [])
        index["last_updated"] = _now_iso()
        snapshots = index.get("snapshots", [])
        snapshots.insert(0, snap_entry)
        index["snapshots"] = snapshots
        self.write(mount_path, index)

    def discover_vms(self, mount_path: str) -> list[dict]:
        """Fallback: scan datastore for Proxmox VM config files (.conf)."""
        try:
            out = ssh_run(
                self._host, self._user, self._pass,
                f"find {shlex.quote(mount_path)} -maxdepth 2 -name '*.conf' "
                f"-not -path '*/.nasnap*' 2>/dev/null",
                capture=True,
                key_material=self._key,
            )
            vms = []
            seen: set[int] = set()
            prefix = mount_path.rstrip("/") + "/"
            for line in (out or "").splitlines():
                path = line.strip()
                if not path:
                    continue
                rel = path.removeprefix(prefix)
                parts = rel.split("/")
                if len(parts) != 2:
                    continue
                dir_id, conf_name = parts
                vmid_str = conf_name.removesuffix(".conf")
                if dir_id == vmid_str and vmid_str.isdigit():
                    vmid = int(vmid_str)
                    if vmid not in seen:
                        seen.add(vmid)
                        vms.append({"vmid": vmid, "name": "", "config_path": rel})
            return sorted(vms, key=lambda v: v["vmid"])
        except Exception as exc:
            log.warning(f"[datastore_index] discover_vms {mount_path}: {exc}")
            return []


def build_snap_entry(
    snap_name: str,
    schedule_name: str,
    consistency: str,
    vm_entries: list[dict],
    expiry_time: str = "",
) -> dict:
    """Builds the snapshot entry dict to insert into the index."""
    vms = []
    for entry in vm_entries:
        raw = entry.get("_raw_conf") or {}
        sha = hashlib.sha256(
            json.dumps(raw, sort_keys=True).encode()
        ).hexdigest()
        vmid = int(entry["vmid"])
        vms.append({
            "vmid": vmid,
            "name": entry.get("name", ""),
            "config_path": f"{vmid}/{vmid}.conf",
            "config_sha256": sha,
        })
    return {
        "ontap_snapshot_name": snap_name,
        "taken_at": _now_iso(),
        "schedule_name": schedule_name,
        "consistent": consistency in ("app", "suspend"),
        "locked": bool(expiry_time),
        "lock_expires": expiry_time or None,
        "vms": vms,
    }


def build_datastore_info(mapping: dict, vm_entries: list[dict]) -> dict:
    """Builds the datastore_info dict passed to DatastoreIndex.add_snapshot()."""
    return {
        "datastore_name": mapping.get("pve_storage_id", ""),
        "datastore_type": mapping.get("storage_protocol", "nfs"),
        "ontap_svm": mapping.get("svm_name", ""),
        "ontap_volume": mapping.get("volume_name", ""),
        "ontap_volume_uuid": mapping.get("volume_uuid", ""),
        "vms_current": [
            {
                "vmid": e["vmid"],
                "name": e.get("name", ""),
                "config_path": f"{e['vmid']}/{e['vmid']}.conf",
            }
            for e in vm_entries
        ],
    }


def _new_index(datastore_info: dict) -> dict:
    return {
        "nasnap_version": "1",
        "schema_version": SCHEMA_VERSION,
        "datastore_name": datastore_info.get("datastore_name", ""),
        "datastore_type": datastore_info.get("datastore_type", "nfs"),
        "ontap_svm": datastore_info.get("ontap_svm", ""),
        "ontap_volume": datastore_info.get("ontap_volume", ""),
        "ontap_volume_uuid": datastore_info.get("ontap_volume_uuid", ""),
        "last_updated": "",
        "snapshots": [],
        "vms_current": [],
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
