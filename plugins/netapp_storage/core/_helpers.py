"""
Shared helpers for snapshot_engine, restore_engine, and clone_engine.
"""

import os
import json
import logging
import re
import subprocess
import shlex
import tempfile
from datetime import datetime, timezone

import requests as _requests

log = logging.getLogger(__name__)

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Derived from the actual directory name so the plugin works regardless of how
# the repo was cloned (e.g. `git clone ... netapp_storage` vs the default name).
PLUGIN_ID = os.path.basename(_PLUGIN_DIR)


class JobCancelledError(RuntimeError):
    """Raised inside a job thread when a cancel request is detected."""


# ONTAP-internal snapshots that are never meaningful to show or usable for
# restore/clone/instant-recovery — SnapMirror transfer markers and SVM-DR
# baselines/updates. Used only at display/merge layers; callers that need the
# full raw ONTAP list (retention GC, exact-name lookups) must not use this.
_SYSTEM_SNAPSHOT_PREFIXES = ("snapmirror.", "vserverdr")


def is_system_snapshot(name: str) -> bool:
    return (name or "").startswith(_SYSTEM_SNAPSHOT_PREFIXES)


def check_cancel(job_id: str) -> None:
    """Raise JobCancelledError if a cancel has been requested for this job."""
    from ._job_registry import is_cancel_requested
    if is_cancel_requested(job_id):
        raise JobCancelledError("Cancelled by user")


def load_plugin_config():
    try:
        with open(os.path.join(_PLUGIN_DIR, "config.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def get_endpoint(db, endpoint_id):
    row = db.query_one("SELECT * FROM netapp_endpoints WHERE id=?", (endpoint_id,))
    if not row:
        raise RuntimeError(f"ONTAP endpoint '{endpoint_id}' not found")
    d = dict(row)
    d["password"] = db._decrypt(d.pop("password_encrypted", ""))
    return d


def get_mapping(db, mapping_id):
    row = db.query_one("SELECT * FROM netapp_volume_mapping WHERE id=?", (mapping_id,))
    if not row:
        raise RuntimeError(f"Volume mapping '{mapping_id}' not found")
    return dict(row)


_VM_NAME_RE = re.compile(r'[^A-Za-z0-9.-]')


def sanitize_vm_name(name, fallback):
    """Returns an ASCII-safe VM name/hostname (letters, digits, dot, dash only).

    PVE's own `name:`/`hostname:` config keys already require this charset —
    enforcing it here, BEFORE the value is spliced into a raw .conf file text
    blob, also closes a config-injection hole: an unsanitized name containing
    a newline could otherwise smuggle extra config lines/keys into the
    written file.
    """
    name = (name or "").strip()
    name = name.splitlines()[0] if name else ""  # drop everything after any embedded newline
    safe = _VM_NAME_RE.sub('-', name).strip('-')
    return safe or fallback


def is_vmid_in_use(host, user, password, key_material, vmid, timeout=10):
    """True if a VM or CT config already exists for this VMID anywhere in the
    cluster. qemu-server and lxc share one VMID numbering space in PVE, so
    both directories must be checked — checking only the guest type being
    written would miss a same-numbered guest of the *other* type."""
    qemu_path = f"/etc/pve/qemu-server/{vmid}.conf"
    lxc_path  = f"/etc/pve/lxc/{vmid}.conf"
    out = ssh_run(
        host, user, password,
        f"{{ [ -f {shlex.quote(qemu_path)} ] && echo QEMU; "
        f"[ -f {shlex.quote(lxc_path)} ] && echo LXC; }} 2>/dev/null; true",
        capture=True, key_material=key_material, timeout=timeout,
    )
    return bool((out or "").strip())


_SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9 ._-]+$')


def validate_safe_name(name, field_label="Name"):
    """Raises ValueError if `name` is empty or contains anything outside
    plain ASCII letters/digits/space/dot/dash/underscore — blocks umlauts,
    other unicode, and shell/path-special characters that have caused
    mount-path and filename bugs elsewhere in this plugin. Returns the
    trimmed name on success."""
    name = (name or "").strip()
    if not name:
        raise ValueError(f"{field_label} is required")
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(
            f"{field_label} may only contain letters, numbers, spaces, dots, "
            f"dashes and underscores (no umlauts or special characters): '{name}'"
        )
    return name


def find_name_conflict(db, table, name, exclude_id=None, name_column="name"):
    """Generic case-insensitive uniqueness check for a `name`-type column.

    `table`/`name_column` are always caller-supplied string literals (never
    request input), so building the SQL with an f-string here is safe.
    Returns the conflicting row as a dict, or None.
    """
    name = (name or "").strip()
    if not name:
        return None
    q = f"SELECT * FROM {table} WHERE LOWER({name_column})=LOWER(?)"
    params = [name]
    if exclude_id:
        q += " AND id != ?"
        params.append(exclude_id)
    row = db.query_one(q, tuple(params))
    return dict(row) if row else None


def find_datastore_conflict(db, name, pve_storage_id=""):
    """Returns the conflicting row (as dict) if a datastore with this display
    name or this PVE storage id (i.e. the same /mnt/pve/<id> mountpoint) is
    already registered, else None.

    Any row present in netapp_provisioned_datastores — regardless of status
    (provisioning/active/removing/error) — still owns its name/mountpoint
    until it is actually deleted, so all statuses are checked.
    """
    name = (name or "").strip()
    pve_storage_id = (pve_storage_id or "").strip()
    if not name and not pve_storage_id:
        return None
    q = "SELECT id, name, pve_storage_id, status FROM netapp_provisioned_datastores WHERE LOWER(name)=LOWER(?)"
    params = [name]
    if pve_storage_id:
        q += " OR pve_storage_id=?"
        params.append(pve_storage_id)
    row = db.query_one(q, tuple(params))
    return dict(row) if row else None


_SIZE_SUFFIX = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_disk_size(conf_value: str):
    """Extracts the size= token from a PVE disk config line, in bytes.

    e.g. 'nfs-store:images/100/vm-100-disk-0.qcow2,size=20G' -> 21474836480
    Returns None if no size= token is present or it can't be parsed.
    """
    for part in str(conf_value).split(","):
        part = part.strip()
        if not part.startswith("size="):
            continue
        raw = part[len("size="):].strip().upper()
        try:
            if raw and raw[-1] in _SIZE_SUFFIX:
                return int(float(raw[:-1]) * _SIZE_SUFFIX[raw[-1]])
            return int(raw)
        except ValueError:
            return None
    return None


def get_snapshot_record(db, snapshot_id):
    row = db.query_one("SELECT * FROM netapp_snapshots WHERE id=?", (snapshot_id,))
    if not row:
        raise RuntimeError(f"Snapshot '{snapshot_id}' not found")
    return dict(row)


def count_snapshots_in_range(db, start_iso, end_iso, status_filter=None):
    """Count netapp_snapshots rows created in [start_iso, end_iso).

    status_filter: None (all), 'done', or an iterable of statuses (e.g. ('failed','error')).
    """
    where = "created_at >= ? AND created_at < ?"
    params = [start_iso, end_iso]
    if status_filter is not None:
        statuses = [status_filter] if isinstance(status_filter, str) else list(status_filter)
        placeholders = ",".join("?" for _ in statuses)
        where += f" AND status IN ({placeholders})"
        params.extend(statuses)
    row = db.query_one(f"SELECT COUNT(*) AS c FROM netapp_snapshots WHERE {where}", params)
    return dict(row or {}).get("c", 0)


def build_ontap_client(endpoint):
    from .ontap_client import OntapClient
    return OntapClient(
        host=endpoint["host"],
        username=endpoint["username"],
        password=endpoint["password"],
        ssl_verify=bool(endpoint.get("ssl_verify", 1)),
    )


def pve_for_mapping(db, mapping):
    """Try mapping's pve_cluster_id first, then fall back to any configured PVE host.

    Guards against a dangling pve_cluster_id reference (host removed from
    netapp_pve_hosts after the mapping row was created) — any other host in
    the same cluster can usually still reach the same shared storage.
    """
    candidate = (mapping or {}).get("pve_cluster_id", "")
    ids = [candidate] if candidate else []
    others = db.query(
        "SELECT id FROM netapp_pve_hosts WHERE id!=? ORDER BY id LIMIT 10", (candidate,)
    )
    ids += [r["id"] for r in (others or [])]
    last_exc = None
    for hid in ids:
        try:
            return build_pve_client(db, hid), hid
        except Exception as e:
            last_exc = e
    raise RuntimeError(f"No accessible PVE host found (tried {len(ids)}): {last_exc}")


def build_pve_client(db, pve_host_id):
    """Returns a PluginPveSession for a plugin-managed PVE host."""
    row = db.query_one("SELECT * FROM netapp_pve_hosts WHERE id=?", (pve_host_id,))
    if not row:
        raise RuntimeError(f"PVE host '{pve_host_id}' not found in netapp_pve_hosts")
    d = dict(row)
    d["password"] = db._decrypt(d.pop("password_encrypted", ""))
    return PluginPveSession(d)


class PluginPveSession:
    """Lightweight PVE REST client using plugin-managed credentials."""

    def __init__(self, pve_host):
        self.host   = pve_host["host"]
        self.nfs_ip = pve_host.get("nfs_ip", "").strip()
        self.port   = int(pve_host.get("port", 8006))
        self._base = f"https://{self.host}:{self.port}/api2/json"
        ssl_verify = bool(pve_host.get("ssl_verify", 0))

        self._session = _requests.Session()
        self._session.verify = ssl_verify
        if not ssl_verify:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        last_exc = None
        for attempt in range(3):
            try:
                r = self._session.post(
                    f"{self._base}/access/ticket",
                    data={"username": pve_host["username"], "password": pve_host["password"]},
                    timeout=(10, 60),
                )
                r.raise_for_status()
                break
            except Exception as exc:
                last_exc = exc
                import time as _time
                _time.sleep(5)
        else:
            raise last_exc
        data = r.json().get("data", {})
        self._session.cookies.set("PVEAuthCookie", data["ticket"])
        self._session.headers.update({"CSRFPreventionToken": data.get("CSRFPreventionToken", "")})
        self.is_connected = True

        # SSH credentials: root@pam → user=root, same password
        uname = pve_host.get("username", "root@pam")
        self.ssh_user = uname.split("@")[0]
        self.ssh_password = pve_host["password"]
        self.ssh_key = ""

    def _api_get(self, url):
        return self._session.get(url, timeout=(10, 60))

    def _api_post(self, url, data=None):
        return self._session.post(url, json=data or {}, timeout=(10, 120))

    def get_node_status(self):
        """{node_name: {ip, host}} for all nodes in the cluster."""
        r = self._api_get(f"{self._base}/nodes")
        if not r.ok:
            return {}
        result = {}
        for n in r.json().get("data", []):
            name = n.get("node", "")
            if name:
                result[name] = {"ip": n.get("ip", name), "host": name}
        return result

    def get_vm_config(self, node, vmid, vm_type="qemu"):
        """{'success': bool, 'config': {'raw': dict}}"""
        vt = "qemu" if vm_type == "qemu" else "lxc"
        r = self._api_get(f"{self._base}/nodes/{node}/{vt}/{vmid}/config")
        if not r.ok:
            return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"success": True, "config": {"raw": r.json().get("data", {})}}

    def find_vm_node(self, vmid):
        """Returns the node name where a VM/CT is running."""
        r = self._api_get(f"{self._base}/cluster/resources?type=vm")
        if not r.ok:
            return None
        for res in r.json().get("data", []):
            if res.get("vmid") == int(vmid):
                return res.get("node")
        return None


def get_ssh_creds(mgr):
    """Returns (user, password, key_material) from a ClusterManager or PluginPveSession."""
    if isinstance(mgr, PluginPveSession):
        return mgr.ssh_user, mgr.ssh_password, mgr.ssh_key
    user = getattr(mgr.config, "ssh_user", None) or "root"
    key_material = getattr(mgr.config, "ssh_key", None) or ""
    password = mgr.config.pass_ if not key_material else ""
    return user, password, key_material


def _find_system_ssh_key():
    """Returns the path to the first available system SSH key, or None."""
    home = os.path.expanduser("~")
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        path = os.path.join(home, ".ssh", name)
        if os.path.exists(path):
            return path
    return None


def ssh_run(host, user, password, cmd, capture=False, capture_bytes=False, stdin_data=None, timeout=60, key_material=""):
    """Runs an SSH command. Raises RuntimeError on failure.

    Authentication priority:
      1. key_material (SSH private key as string) → temp file → -i
      2. password → sshpass (if installed)
      3. No auth → only works with a pre-configured SSH agent key

    capture=True returns stdout as a string.
    stdin_data=bytes pipes data to stdin.
    """
    from nasnap_core.utils.ssh_pool import controlmaster_args
    cm_args = controlmaster_args(host, user)

    base_ssh = [
        "ssh",
        *cm_args,
        "-o", "StrictHostKeyChecking=no",
        "-o", f"ConnectTimeout={min(timeout, 15)}",
        "-o", "BatchMode=yes",
    ]

    key_tmp = None
    try:
        if key_material:
            # Write private key to a temp file for -i flag
            fd, key_tmp = tempfile.mkstemp(prefix="nasnap-ssh-key-")
            try:
                os.write(fd, key_material.encode() if isinstance(key_material, str) else key_material)
            finally:
                os.close(fd)
            os.chmod(key_tmp, 0o600)
            ssh_cmd = base_ssh + ["-i", key_tmp, "-o", "PasswordAuthentication=no",
                                   f"{user}@{host}", cmd]
            final_cmd = ssh_cmd
        elif password:
            # Prefer system key (PVE often disables password login for root)
            _system_key = _find_system_ssh_key()
            if _system_key:
                ssh_cmd = base_ssh + ["-i", _system_key, "-o", "PasswordAuthentication=no",
                                      f"{user}@{host}", cmd]
                final_cmd = ssh_cmd
            else:
                # sshpass braucht BatchMode=no
                no_batch = [c if c != "BatchMode=yes" else "BatchMode=no" for c in base_ssh]
                ssh_cmd = no_batch + [f"{user}@{host}", cmd]
                import shutil as _shutil
                if _shutil.which("sshpass"):
                    final_cmd = ["sshpass", "-p", password] + ssh_cmd
                else:
                    log.warning("[netapp_storage] sshpass not found")
                    final_cmd = ssh_cmd
        else:
            ssh_cmd = base_ssh + [f"{user}@{host}", cmd]
            final_cmd = ssh_cmd

        try:
            result = subprocess.run(
                final_cmd,
                input=stdin_data,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SSH timeout after {timeout}s: {cmd[:80]}")
        except FileNotFoundError:
            raise RuntimeError("ssh binary not found")

        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"SSH command failed (rc={result.returncode}): {stderr[:300]}")

        if capture_bytes:
            return result.stdout
        if capture:
            return result.stdout.decode(errors="replace")
        return ""

    finally:
        if key_tmp and os.path.exists(key_tmp):
            try:
                os.unlink(key_tmp)
            except Exception:
                pass


class SshSession:
    """Persistent SSH connection via paramiko.

    Opens one TCP+SSH session and reuses it for many exec_command calls.
    Each call costs ~1-2 ms (channel open) vs ~3-8 ms with subprocess+ControlMaster.
    Use as context manager:  with SshSession(host, user, pw) as s: s.run(cmd)
    """

    def __init__(self, host, user, password, key_material="", timeout=15):
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(hostname=host, username=user, timeout=timeout,
                  look_for_keys=False, allow_agent=False)
        if key_material:
            import io
            for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
                try:
                    kw["pkey"] = cls.from_private_key(io.StringIO(key_material))
                    break
                except Exception:
                    pass
            else:
                raise RuntimeError("Could not load SSH key material")
        else:
            sys_key = _find_system_ssh_key()
            if sys_key:
                kw["key_filename"] = sys_key
            else:
                kw["password"] = password
        client.connect(**kw)
        self._client = client

    def run(self, cmd, timeout=120):
        """Execute cmd, return stdout as str. Raises RuntimeError on non-zero exit.

        stdout is drained BEFORE recv_exit_status to prevent a paramiko channel
        deadlock: if stdout data exceeds the SSH window size (2 MB default) the
        remote process blocks on write, and recv_exit_status() waits forever.
        """
        _, out_fh, err_fh = self._client.exec_command(cmd, timeout=timeout)
        stdout = out_fh.read().decode("utf-8", errors="replace")  # drain before exit status
        rc     = out_fh.channel.recv_exit_status()
        if rc != 0:
            stderr = err_fh.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"SSH command failed (rc={rc}): {stderr[:300]}")
        return stdout

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class JobLogger:
    """Appends log lines to netapp_jobs.log_json."""

    def __init__(self, job_id, db):
        self.job_id = job_id
        self.db = db

    def log(self, msg):
        log.info(f"[netapp_storage job={self.job_id}] {msg}")
        try:
            row = self.db.query_one("SELECT log_json FROM netapp_jobs WHERE id=?", (self.job_id,))
            existing = json.loads(row["log_json"] or "[]") if row else []
            entry = {"ts": datetime.now(timezone.utc).isoformat(), "msg": msg}
            existing.append(entry)
            self.db.execute(
                "UPDATE netapp_jobs SET log_json=? WHERE id=?",
                (json.dumps(existing), self.job_id),
            )
        except Exception as e:
            log.warning(f"[netapp_storage] JobLogger write failed: {e}")


def get_global_timezone_name() -> str:
    """Global fallback timezone (IANA name): the DB setting set in Settings
    overrides the TZ env var, which overrides UTC. Used wherever a per-user
    timezone can't apply — email report rendering (recipients are free-text
    addresses, not NaSnap logins) and cron schedule evaluation."""
    try:
        from nasnap_core.core.db import get_db
        row = get_db().query_one("SELECT value FROM np_settings WHERE key='global_timezone'")
        if row and row["value"]:
            return row["value"]
    except Exception:
        pass
    return os.environ.get("TZ", "UTC")


def get_global_timezone():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(get_global_timezone_name())
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def run_as_job(db, job_type, created_by, fn):
    """Wraps a background action in a netapp_jobs row so it shows up in the UI
    Activity Log instead of only in the server log file.

    fn receives a JobLogger and returns a dict of result fields (or None);
    that dict is returned to the caller alongside the job_id. On exception the
    job is marked 'failed' (with the error appended to its log) and the
    exception re-raised for the caller to handle.
    """
    import uuid
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO netapp_jobs (id, job_type, vmid, node, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, job_type, None, "", "running", created_by, now),
    )
    logger = JobLogger(job_id, db)
    try:
        result = fn(logger) or {}
        db.execute(
            "UPDATE netapp_jobs SET status='done', completed_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
        return job_id, result
    except Exception as exc:
        logger.log(f"ERROR: {exc}")
        db.execute(
            "UPDATE netapp_jobs SET status='failed', completed_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
        raise
