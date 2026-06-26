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

    # ── Public API ───────────────────────────────────────────────────────────

    def list(self, cfg, db):
        """List backup files on the remote target. Returns [{'filename', 'size', 'mtime'}] newest first."""
        target = cfg.get('target_type', '')
        if target == 'sftp':
            return self._list_sftp(cfg)
        elif target == 'cifs':
            return self._list_cifs(cfg)
        elif target == 'nfs_datastore':
            return self._list_nfs_ds(cfg, db)
        raise ValueError(f"Unknown target type: {target!r}")

    def fetch(self, cfg, db, filename):
        """Download a single backup file. Returns raw gzip bytes."""
        if not _is_backup_file(filename) or '/' in filename or '\\' in filename:
            raise ValueError("Invalid filename")
        target = cfg.get('target_type', '')
        if target == 'sftp':
            return self._fetch_sftp(cfg, filename)
        elif target == 'cifs':
            return self._fetch_cifs(cfg, filename)
        elif target == 'nfs_datastore':
            return self._fetch_nfs_ds(cfg, db, filename)
        raise ValueError(f"Unknown target type: {target!r}")

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

    def _resolve_nfs_target(self, cfg, db):
        """Returns (pve_host, pve_user, pve_pass, remote_dir) for the configured NFS DS."""
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
            "WHERE m.pve_storage_id = ? LIMIT 1", (storage_id,))
        if not host_row:
            host_row = db.query_one(
                "SELECT host, username, password_encrypted FROM netapp_pve_hosts LIMIT 1")
        if not host_row:
            raise RuntimeError("No PVE host configured")
        h = dict(host_row)
        pve_host = h['host']
        pve_user = h['username'].split('@')[0]
        pve_pass = db._decrypt(h['password_encrypted'])
        subdir = (cfg.get('nfs_subdir') or '.nasnap/db_backups').strip('/')
        remote_dir = f"/mnt/pve/{storage_id}/{subdir}"
        return pve_host, pve_user, pve_pass, remote_dir

    def _transfer_nfs_ds(self, data, cfg, filename, db):
        from ..core._helpers import ssh_run

        pve_host, pve_user, pve_pass, remote_dir = self._resolve_nfs_target(cfg, db)
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

    def _list_nfs_ds(self, cfg, db):
        from ..core._helpers import ssh_run
        pve_host, pve_user, pve_pass, remote_dir = self._resolve_nfs_target(cfg, db)
        try:
            ls_out = ssh_run(
                pve_host, pve_user, pve_pass,
                f"ls -la --time-style=+%Y-%m-%dT%H:%M:%SZ {shlex.quote(remote_dir)}/nasnap_db_backup_*.json.gz 2>/dev/null",
                capture=True, timeout=30)
        except RuntimeError:
            return []
        files = []
        for line in ls_out.strip().splitlines():
            parts = line.split()
            if len(parts) >= 7:
                size = int(parts[4]) if parts[4].isdigit() else 0
                mtime = parts[5] if len(parts) > 5 else ''
                fname = parts[-1].split('/')[-1]
                if _is_backup_file(fname):
                    files.append({'filename': fname, 'size': size, 'mtime': mtime})
        return sorted(files, key=lambda x: x['filename'], reverse=True)

    def _fetch_nfs_ds(self, cfg, db, filename):
        from ..core._helpers import ssh_run
        pve_host, pve_user, pve_pass, remote_dir = self._resolve_nfs_target(cfg, db)
        remote_path = f"{remote_dir}/{filename}"
        return ssh_run(pve_host, pve_user, pve_pass,
                       f"cat {shlex.quote(remote_path)}",
                       capture_bytes=True, timeout=120)

    # ── SFTP list/fetch ───────────────────────────────────────────────────────

    def _sftp_connect(self, cfg):
        try:
            import paramiko
        except ImportError:
            raise RuntimeError("paramiko not installed")
        host = cfg.get('sftp_host', '').strip()
        port = int(cfg.get('sftp_port') or 22)
        user = cfg.get('sftp_user', '').strip()
        password = cfg.get('sftp_password', '')
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = {'username': user, 'port': port, 'timeout': 30}
        if password:
            kw['password'] = password
            kw['look_for_keys'] = False
        client.connect(host, **kw)
        return client, (cfg.get('sftp_path') or '/').rstrip('/')

    def _list_sftp(self, cfg):
        client, remote_dir = self._sftp_connect(cfg)
        try:
            sftp = client.open_sftp()
            try:
                attrs = sftp.listdir_attr(remote_dir)
                files = []
                for a in attrs:
                    if _is_backup_file(a.filename):
                        mtime = (datetime.fromtimestamp(a.st_mtime, tz=timezone.utc).isoformat()
                                 if a.st_mtime else '')
                        files.append({'filename': a.filename, 'size': a.st_size or 0, 'mtime': mtime})
                return sorted(files, key=lambda x: x['filename'], reverse=True)
            finally:
                sftp.close()
        finally:
            client.close()

    def _fetch_sftp(self, cfg, filename):
        import io
        client, remote_dir = self._sftp_connect(cfg)
        try:
            sftp = client.open_sftp()
            try:
                buf = io.BytesIO()
                sftp.getfo(f"{remote_dir}/{filename}", buf)
                return buf.getvalue()
            finally:
                sftp.close()
        finally:
            client.close()

    # ── CIFS list/fetch ───────────────────────────────────────────────────────

    def _cifs_auth(self, cfg):
        host    = cfg.get('cifs_host', '').strip()
        share   = cfg.get('cifs_share', '').strip().strip('\\/')
        user    = cfg.get('cifs_user', '').strip()
        domain  = cfg.get('cifs_domain', '').strip()
        password = cfg.get('cifs_password', '')
        subdir  = (cfg.get('cifs_path') or '').strip('\\/')
        unc     = f"//{host}/{share}"
        auth    = f"{domain}\\{user}" if domain else user
        return unc, auth, password, subdir

    def _list_cifs(self, cfg):
        unc, auth, password, subdir = self._cifs_auth(cfg)
        pattern = (f"{subdir.replace('/', chr(92))}\\nasnap_db_backup_*"
                   if subdir else "nasnap_db_backup_*")
        try:
            out = self._smbclient(unc, auth, password, f"ls {pattern}", capture=True)
        except RuntimeError:
            return []
        return [{'filename': n, 'size': 0, 'mtime': ''}
                for n in sorted(_parse_smbclient_ls(out), reverse=True)]

    def _fetch_cifs(self, cfg, filename):
        unc, auth, password, subdir = self._cifs_auth(cfg)
        remote_path = (f"{subdir.replace('/', chr(92))}\\{filename}"
                       if subdir else filename)
        tmp_dir = tempfile.mkdtemp(prefix="nasnap-dbbak-fetch-")
        try:
            local_path = os.path.join(tmp_dir, filename)
            self._smbclient(unc, auth, password, f"get {remote_path} {local_path}")
            with open(local_path, 'rb') as f:
                return f.read()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


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
