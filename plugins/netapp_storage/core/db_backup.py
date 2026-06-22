"""
DB Backup Engine — builds the export payload and transfers to a remote target.

Supported targets:
  sftp          — SFTP server (paramiko)
  cifs          — CIFS/SMB share (smbclient subprocess)
  nfs_datastore — existing NaSnap NFS datastore via PVE host SSH
"""

import gzip
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_FILENAME_PREFIX = "nasnap_db_backup_"
_FILENAME_SUFFIX = ".json.gz"


def _make_filename():
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    return f"{_FILENAME_PREFIX}{ts}{_FILENAME_SUFFIX}"


def _is_backup_file(name):
    return name.startswith(_FILENAME_PREFIX) and name.endswith(_FILENAME_SUFFIX)


class DbBackupRunner:
    def run(self, cfg, db):
        """Execute one backup. Returns {'success', 'filename', 'bytes_written', 'error'}."""
        from ..api.settings import build_export_payload
        try:
            payload = build_export_payload()
            data = gzip.compress(
                json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                compresslevel=6,
            )
            filename = _make_filename()
            target = cfg.get('target_type', '')
            if target == 'sftp':
                self._transfer_sftp(data, cfg, filename)
            elif target == 'cifs':
                self._transfer_cifs(data, cfg, filename)
            elif target == 'nfs_datastore':
                self._transfer_nfs_ds(data, cfg, filename, db)
            else:
                raise ValueError(f"Unknown target type: {target!r}")
            return {'success': True, 'filename': filename, 'bytes_written': len(data)}
        except Exception as exc:
            log.warning(f"[db_backup] Backup failed: {exc}")
            return {'success': False, 'error': str(exc)}

    # ── SFTP ────────────────────────────────────────────────────────────────

    def _transfer_sftp(self, data, cfg, filename):
        try:
            import paramiko
        except ImportError:
            raise RuntimeError("paramiko not installed — add 'paramiko' to requirements.txt")

        host = cfg.get('sftp_host', '').strip()
        port = int(cfg.get('sftp_port') or 22)
        user = cfg.get('sftp_user', '').strip()
        password = cfg.get('sftp_password', '')
        remote_dir = (cfg.get('sftp_path') or '/').rstrip('/')

        if not host:
            raise ValueError("SFTP host not configured")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kw = {'username': user, 'port': port, 'timeout': 30}
        if password:
            connect_kw['password'] = password
            connect_kw['look_for_keys'] = False
        client.connect(host, **connect_kw)
        try:
            sftp = client.open_sftp()
            try:
                _sftp_makedirs(sftp, remote_dir)
                import io
                sftp.putfo(io.BytesIO(data), f"{remote_dir}/{filename}")
                self._prune_sftp(sftp, remote_dir, int(cfg.get('keep_copies') or 7))
            finally:
                sftp.close()
        finally:
            client.close()

    def _prune_sftp(self, sftp, remote_dir, keep):
        try:
            files = sorted(f for f in sftp.listdir(remote_dir) if _is_backup_file(f))
            for old in files[:-keep] if keep > 0 else files:
                try:
                    sftp.remove(f"{remote_dir}/{old}")
                except Exception as e:
                    log.warning(f"[db_backup] SFTP prune {old}: {e}")
        except Exception as e:
            log.warning(f"[db_backup] SFTP prune listing failed: {e}")

    # ── CIFS ─────────────────────────────────────────────────────────────────

    def _transfer_cifs(self, data, cfg, filename):
        host   = cfg.get('cifs_host', '').strip()
        share  = cfg.get('cifs_share', '').strip().strip('\\/')
        user   = cfg.get('cifs_user', '').strip()
        domain = cfg.get('cifs_domain', '').strip()
        password = cfg.get('cifs_password', '')
        subdir = (cfg.get('cifs_path') or '').strip('\\/')

        if not host or not share:
            raise ValueError("CIFS host/share not configured")

        tmp_dir = tempfile.mkdtemp(prefix="nasnap-dbbak-")
        try:
            local_path = os.path.join(tmp_dir, filename)
            with open(local_path, 'wb') as f:
                f.write(data)

            unc  = f"//{host}/{share}"
            auth = f"{domain}\\{user}" if domain else user
            remote_path = f"{subdir.replace('/', chr(92))}\\{filename}" if subdir else filename

            if subdir:
                try:
                    self._smbclient(unc, auth, password,
                                    f"mkdir {subdir.replace('/', chr(92))}")
                except RuntimeError as e:
                    if "COLLISION" not in str(e):
                        raise

            self._smbclient(unc, auth, password, f"put {local_path} {remote_path}")
            self._prune_cifs(unc, auth, password, subdir,
                             int(cfg.get('keep_copies') or 7))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _smbclient(self, unc, auth, password, commands, capture=False):
        if password:
            cmd = ["smbclient", unc, "-U", auth, f"--password={password}", "-c", commands]
        else:
            cmd = ["smbclient", unc, "-U", auth, "--no-pass", "-c", commands]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        out = result.stdout.decode(errors='replace')
        if result.returncode != 0:
            err = result.stderr.decode(errors='replace').strip()
            raise RuntimeError(f"smbclient: {err[:200]}")
        return out if capture else ""

    def _prune_cifs(self, unc, auth, password, subdir, keep):
        try:
            pattern = f"{subdir.replace('/', chr(92))}\\nasnap_db_backup_*" if subdir else "nasnap_db_backup_*"
            out = self._smbclient(unc, auth, password, f"ls {pattern}", capture=True)
            files = sorted(_parse_smbclient_ls(out))
            for old in files[:-keep] if keep > 0 else files:
                p = f"{subdir.replace('/', chr(92))}\\{old}" if subdir else old
                try:
                    self._smbclient(unc, auth, password, f"del {p}")
                except Exception as e:
                    log.warning(f"[db_backup] CIFS prune {old}: {e}")
        except Exception as e:
            log.warning(f"[db_backup] CIFS prune failed: {e}")

    # ── NFS Datastore (via PVE SSH) ──────────────────────────────────────────

    def _transfer_nfs_ds(self, data, cfg, filename, db):
        from ..core._helpers import ssh_run

        ds_id = cfg.get('nfs_ds_id', '').strip()
        if not ds_id:
            raise ValueError("NFS datastore not configured")

        ds = db.query_one(
            "SELECT pve_storage_id FROM netapp_provisioned_datastores WHERE id=?", (ds_id,))
        if not ds:
            raise ValueError(f"Datastore '{ds_id}' not found")
        storage_id = dict(ds)['pve_storage_id']

        host_row = db.query_one(
            "SELECT h.host, h.username, h.password_encrypted "
            "FROM netapp_pve_hosts h "
            "JOIN netapp_volume_mapping m ON m.pve_cluster_id = h.id "
            "WHERE m.pve_storage_id = ? LIMIT 1",
            (storage_id,))
        if not host_row:
            host_row = db.query_one(
                "SELECT host, username, password_encrypted FROM netapp_pve_hosts LIMIT 1")
        if not host_row:
            raise RuntimeError("No PVE host configured")

        h = dict(host_row)
        pve_host = h['host']
        pve_user = h['username'].split('@')[0]
        pve_pass = db._decrypt(h['password_encrypted'])

        subdir    = (cfg.get('nfs_subdir') or '.nasnap/db_backups').strip('/')
        remote_dir  = f"/mnt/pve/{storage_id}/{subdir}"
        remote_path = f"{remote_dir}/{filename}"

        ssh_run(pve_host, pve_user, pve_pass,
                f"mkdir -p {shlex.quote(remote_dir)}", timeout=30)
        ssh_run(pve_host, pve_user, pve_pass,
                f"cat > {shlex.quote(remote_path)}",
                stdin_data=data, timeout=120)

        keep = int(cfg.get('keep_copies') or 7)
        try:
            ls_out = ssh_run(
                pve_host, pve_user, pve_pass,
                f"ls -1 {shlex.quote(remote_dir)}/nasnap_db_backup_*.json.gz 2>/dev/null | sort",
                capture=True, timeout=30)
            paths = [l.strip() for l in ls_out.strip().splitlines() if l.strip()]
            for old_path in paths[:-keep] if keep > 0 else paths:
                try:
                    ssh_run(pve_host, pve_user, pve_pass,
                            f"rm -f {shlex.quote(old_path)}", timeout=30)
                except Exception as e:
                    log.warning(f"[db_backup] NFS prune {old_path}: {e}")
        except Exception as e:
            log.warning(f"[db_backup] NFS prune listing failed: {e}")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sftp_makedirs(sftp, path):
    parts = path.lstrip('/').split('/')
    current = ''
    for part in parts:
        if not part:
            continue
        current = f"{current}/{part}"
        try:
            sftp.mkdir(current)
        except IOError:
            pass  # already exists


def _parse_smbclient_ls(output):
    """Extract backup filenames from smbclient ls output."""
    files = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith('Domain=') or line.startswith('Try '):
            continue
        parts = line.split()
        if parts and _is_backup_file(parts[0]):
            files.append(parts[0])
    return files
