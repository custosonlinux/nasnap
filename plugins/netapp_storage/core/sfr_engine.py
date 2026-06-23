"""
Single File Restore Engine

Left panel  — QCOW2 snapshot mounted via qemu-nbd on PVE host, browsed via SSH
Right panel — live VM filesystem via QEMU Guest Agent (PVE REST API)
"""

import json
import logging
import os
import re
import shlex
import time
import uuid

log = logging.getLogger(__name__)

_MOUNT_BASE = "/tmp/nasnap-sfr-"
_QGA_POLL   = 0.5
_QGA_TIMEOUT = 30


# ── QGA ──────────────────────────────────────────────────────────────────────

def check_qga(pve, vmid, node):
    """Returns True if QEMU Guest Agent is running in the VM."""
    try:
        r = pve._api_get(f"{pve._base}/nodes/{node}/qemu/{vmid}/agent/info")
        return r.ok
    except Exception:
        return False


def get_osinfo(pve, vmid, node):
    """Query OS details via QGA guest-get-osinfo.

    Returns a dict with keys: guest_os ('linux'|'windows'), os_name (pretty string),
    os_id (raw id from QGA), os_version.
    Returns None if QGA is not running, the command is unavailable, or the
    response contains no usable data (older Windows QGA versions may return empty).
    Does NOT exec anything inside the VM — uses a read-only QGA query.
    """
    try:
        r = pve._api_get(f"{pve._base}/nodes/{node}/qemu/{vmid}/agent/get-osinfo")
        if not r.ok:
            log.debug(f"[sfr] get-osinfo {vmid}: HTTP {r.status_code} — will probe with exec")
            return None
        data = (r.json().get("data") or {})
        os_id   = (data.get("id")   or "").lower().strip()
        os_name = (data.get("pretty-name") or data.get("name") or "").strip()
        # Empty response → treat as unsupported (older QGA versions)
        if not os_id and not os_name:
            return None
        if "windows" in os_id or os_id == "mswindows":
            guest_os = "windows"
        elif "windows" in os_name.lower():
            guest_os = "windows"
            os_id    = os_id or "mswindows"
        else:
            guest_os = "linux"
        return {
            "guest_os":   guest_os,
            "os_name":    os_name or os_id,
            "os_id":      os_id,
            "os_version": (data.get("version") or data.get("version-id") or "").strip(),
        }
    except Exception:
        return None


def _qga_exec(pve, vmid, node, command, timeout=_QGA_TIMEOUT):
    """Run a command array in the VM via QGA. Returns (stdout, stderr, exitcode)."""
    r = pve._api_post(
        f"{pve._base}/nodes/{node}/qemu/{vmid}/agent/exec",
        {"command": command},
    )
    if not r.ok:
        raise RuntimeError(f"QGA exec failed ({r.status_code}): {r.text[:200]}")
    pid = (r.json().get("data") or {}).get("pid")
    if pid is None:
        raise RuntimeError("QGA exec: no pid returned")

    deadline = time.time() + timeout
    while time.time() < deadline:
        rs = pve._api_get(
            f"{pve._base}/nodes/{node}/qemu/{vmid}/agent/exec-status?pid={pid}"
        )
        if not rs.ok:
            raise RuntimeError(f"QGA exec-status error ({rs.status_code})")
        d = rs.json().get("data") or {}
        if d.get("exited"):
            return (d.get("out-data", ""), d.get("err-data", ""), int(d.get("exitcode", 0)))
        time.sleep(_QGA_POLL)
    raise RuntimeError(f"QGA exec timed out after {timeout}s: {command}")


def detect_guest_os(pve, vmid, node):
    """Returns 'linux' or 'windows'.

    Detection chain:
    1. agent/get-osinfo  (read-only, no exec in VM)
    2. uname probe       — success → Linux; "No such file" exception → Windows
    3. cmd.exe probe     — success → Windows
    4. Default → Linux
    """
    info = get_osinfo(pve, vmid, node)
    if info:
        return info["guest_os"]

    # Probe 1: uname — Linux/BSD have it, Windows does not
    try:
        _, _, rc = _qga_exec(pve, vmid, node, ["uname"], timeout=8)
        if rc == 0:
            return "linux"
        # uname returned but exit code != 0 — unusual; fall through to cmd.exe probe
    except Exception as e:
        msg = str(e).lower()
        # "Failed to execute child process / No such file" = executable not found = Windows
        if "no such file" in msg or "failed to execute" in msg or "child process" in msg:
            log.debug(f"[sfr] detect_guest_os {vmid}: uname not found → Windows")
            return "windows"
        # Timeout or other QGA error — continue to next probe

    # Probe 2: cmd.exe — exists on Windows, not on Linux
    try:
        _, _, rc = _qga_exec(pve, vmid, node, ["cmd.exe", "/c", "exit", "0"], timeout=8)
        if rc == 0:
            log.debug(f"[sfr] detect_guest_os {vmid}: cmd.exe responded → Windows")
            return "windows"
    except Exception:
        pass

    return "linux"


# ── SAN disk listing ─────────────────────────────────────────────────────────

def list_san_vm_disks(pve, vg_name, vmid):
    """List LV names for a VM in a SAN datastore (from the live VG).
    Returns names like ['vm-105-disk-0', 'vm-105-disk-1'] sorted.
    """
    from ._helpers import ssh_run
    try:
        out = ssh_run(
            pve.host, pve.ssh_user, pve.ssh_password,
            f"lvs --noheadings -o lv_name {shlex.quote(vg_name)} 2>/dev/null"
            f" | grep 'vm-{int(vmid)}-disk' || true",
            capture=True, timeout=15,
        )
        return sorted(l.strip() for l in out.strip().splitlines() if l.strip())
    except Exception as e:
        log.warning(f"[sfr] list_san_vm_disks: {e}")
        return []


# ── Snapshot disk listing ─────────────────────────────────────────────────────

def list_snap_disks(pve, nfs_mount_path, snap_name, vmid):
    """List QCOW2 files for a VM inside an ONTAP snapshot (SSH ls on PVE host).
    PVE NFS storage places VM images under images/{vmid}/ on the volume.
    """
    from ._helpers import ssh_run
    snap_images = f"{nfs_mount_path}/.snapshot/{snap_name}/images/{vmid}"
    try:
        out = ssh_run(
            pve.host, pve.ssh_user, pve.ssh_password,
            f"ls {shlex.quote(snap_images)}/*.qcow2 2>/dev/null || true",
            capture=True, timeout=15,
        )
        disks = sorted(l.strip() for l in out.strip().splitlines() if l.strip().endswith(".qcow2"))
        if disks:
            return disks
        # Fallback: search one level deeper (non-standard layouts)
        out2 = ssh_run(
            pve.host, pve.ssh_user, pve.ssh_password,
            f"find {shlex.quote(nfs_mount_path + '/.snapshot/' + snap_name)} "
            f"-maxdepth 4 -name '*.qcow2' 2>/dev/null | grep '/{vmid}/' || true",
            capture=True, timeout=20,
        )
        return sorted(l.strip() for l in out2.strip().splitlines() if l.strip().endswith(".qcow2"))
    except Exception as e:
        log.warning(f"[sfr] list_snap_disks: {e}")
        return []


# ── Mount / Umount ────────────────────────────────────────────────────────────

def _find_free_nbd(pve):
    from ._helpers import ssh_run
    script = (
        "for i in $(seq 0 15); do "
        "  sz=$(cat /sys/block/nbd${i}/size 2>/dev/null || echo 0); "
        '  if [ "$sz" = "0" ]; then echo /dev/nbd${i}; break; fi; '
        "done"
    )
    out = ssh_run(pve.host, pve.ssh_user, pve.ssh_password, script, capture=True, timeout=15)
    dev = out.strip()
    if not re.match(r"^/dev/nbd\d+$", dev):
        raise RuntimeError(f"No free nbd device found (got: {dev!r})")
    return dev


def _activate_lvm_guests(host, user, pw, ssh_key, lvm_pv_devs):
    """Activate LVM VGs found inside a guest disk image exposed via qemu-nbd.

    Called when partition scanning finds lvm2_member partitions.  The VGs inside
    a QCOW2/snapshot are not visible to the host normally, so UUID collision with
    live host VGs is not a concern.

    Returns (extra_partitions, vg_names).  vg_names must be deactivated on
    session close (before qemu-nbd disconnect) to avoid stale dm devices.
    """
    from ._helpers import ssh_run
    _ENCRYPTED_FS = {"bitlocker", "crypto_luks", "veracrypt"}
    extra    = []
    vg_names = []

    for pv_dev in lvm_pv_devs:
        try:
            pv_q = shlex.quote(pv_dev)
            # Register PV with LVM (safe even on read-only nbd: updates daemon cache only)
            ssh_run(host, user, pw,
                    f"pvscan --cache {pv_q} 2>/dev/null; true",
                    key_material=ssh_key, timeout=15)
            vg_out = ssh_run(host, user, pw,
                             f"pvs --noheadings -o vg_name {pv_q} 2>/dev/null",
                             capture=True, key_material=ssh_key, timeout=10).strip()
            if not vg_out:
                continue
            vg_name = vg_out
            vg_q    = shlex.quote(vg_name)
            # Activate — dm inherits read-only from nbd, so LVs become read-only automatically
            ssh_run(host, user, pw,
                    f"vgchange -ay {vg_q} 2>/dev/null; true",
                    key_material=ssh_key, timeout=15)
            vg_names.append(vg_name)
            log.info(f"[sfr] Activated guest LVM VG '{vg_name}' from {pv_dev}")

            # List LVs via lvs (lsblk on /dev/<vg> doesn't work — it's a directory)
            try:
                lvs_out = ssh_run(host, user, pw,
                                  f"lvs --noheadings -o lv_name,lv_size {vg_q} 2>/dev/null",
                                  capture=True, key_material=ssh_key, timeout=10)
                lv_lines = [l.strip().split() for l in lvs_out.strip().splitlines() if l.strip()]
            except Exception:
                lv_lines = []

            for parts in lv_lines:
                if not parts:
                    continue
                lv_name = parts[0]
                lv_size = parts[1] if len(parts) > 1 else "?"
                lv_dev  = f"/dev/{vg_name}/{lv_name}"
                fstype  = ""
                label   = ""
                try:
                    fstype = ssh_run(host, user, pw,
                                     f"blkid -s TYPE -o value {shlex.quote(lv_dev)} 2>/dev/null || true",
                                     capture=True, key_material=ssh_key, timeout=10).strip()
                except Exception:
                    pass
                try:
                    label = ssh_run(host, user, pw,
                                    f"blkid -s LABEL -o value {shlex.quote(lv_dev)} 2>/dev/null || true",
                                    capture=True, key_material=ssh_key, timeout=10).strip()
                except Exception:
                    pass
                fs = fstype.lower()
                if fs == "swap":
                    continue
                extra.append({
                    "dev":       lv_dev,
                    "size":      lv_size,
                    "fstype":    fstype,
                    "label":     label or f"{vg_name}/{lv_name}",
                    "encrypted": fs in _ENCRYPTED_FS,
                    "skip":      fs in _ENCRYPTED_FS,
                    "lvm":       True,
                })
                log.info(f"[sfr] Guest LV: {lv_dev} fstype={fstype!r} label={label!r}")
        except Exception as e:
            log.warning(f"[sfr] LVM guest activation failed for {pv_dev}: {e}")

    return extra, vg_names


def mount_disk(pve, session_id, disk_path):
    """Mount a QCOW2 via qemu-nbd on the PVE host (read-only).
    Returns {'nbd_device', 'mount_base', 'partitions': [{dev, size, fstype, label}]}.
    """
    from ._helpers import ssh_run
    mount_base = f"{_MOUNT_BASE}{session_id}"

    ssh_run(pve.host, pve.ssh_user, pve.ssh_password,
            "modprobe nbd max_part=8 2>/dev/null || true", timeout=15)

    nbd_dev = _find_free_nbd(pve)
    ssh_run(pve.host, pve.ssh_user, pve.ssh_password,
            f"qemu-nbd --read-only -c {shlex.quote(nbd_dev)} {shlex.quote(disk_path)}",
            timeout=30)
    ssh_run(pve.host, pve.ssh_user, pve.ssh_password,
            f"sleep 1; partprobe {shlex.quote(nbd_dev)} 2>/dev/null || true", timeout=15)

    _ENCRYPTED_FS  = {"bitlocker", "crypto_luks", "veracrypt"}
    _SKIP_FS       = {"swap", "linux_raid_member", "lvm2_member"}

    host = pve.host
    user = pve.ssh_user
    pw   = pve.ssh_password
    key  = getattr(pve, "ssh_key", None) or ""

    partitions = []
    lvm_pv_devs = []
    try:
        # settle udev so lsblk can see filesystem types
        ssh_run(host, user, pw, "udevadm settle 2>/dev/null || true", timeout=10)
        out = ssh_run(host, user, pw,
                      f"lsblk -J -o NAME,SIZE,FSTYPE,LABEL {shlex.quote(nbd_dev)}",
                      capture=True, timeout=15)
        children = (json.loads(out).get("blockdevices") or [{}])[0].get("children") or []
        for c in children:
            if not c.get("name"):
                continue
            dev = f"/dev/{c['name']}"
            fstype = c.get("fstype") or ""
            label  = c.get("label") or ""
            if not fstype:
                try:
                    fstype = ssh_run(host, user, pw,
                                     f"blkid -s TYPE -o value {shlex.quote(dev)} 2>/dev/null || true",
                                     capture=True, timeout=10).strip()
                except Exception:
                    pass
            if not label:
                try:
                    label = ssh_run(host, user, pw,
                                    f"blkid -s LABEL -o value {shlex.quote(dev)} 2>/dev/null || true",
                                    capture=True, timeout=10).strip()
                except Exception:
                    pass
            fs = fstype.lower()
            if fs == "lvm2_member":
                lvm_pv_devs.append(dev)
            partitions.append({
                "dev":       dev,
                "size":      c.get("size", "?"),
                "fstype":    fstype,
                "label":     label,
                "encrypted": fs in _ENCRYPTED_FS,
                "skip":      fs in _ENCRYPTED_FS or fs in _SKIP_FS,
            })
    except Exception as e:
        log.warning(f"[sfr] partition scan failed: {e}")

    # Activate guest LVM VGs and add their LVs as browsable partitions
    guest_lvm_vgs = []
    if lvm_pv_devs:
        lv_parts, guest_lvm_vgs = _activate_lvm_guests(host, user, pw, key, lvm_pv_devs)
        partitions.extend(lv_parts)

    ssh_run(host, user, pw, f"mkdir -p {shlex.quote(mount_base)}/mnt", timeout=15)

    return {
        "nbd_device":     nbd_dev,
        "mount_base":     mount_base,
        "partitions":     partitions,
        "guest_lvm_vgs":  guest_lvm_vgs,
    }


def mount_partition(pve, partition_dev, mount_base):
    """Mount a partition read-only. Returns a list of log lines for debugging.
    Tries multiple mount options to handle dirty journals (ext4/xfs).
    """
    from ._helpers import ssh_run
    mnt   = f"{mount_base}/mnt"
    lines = []

    def _try(opts, label):
        cmd = f"mount {opts} {shlex.quote(partition_dev)} {shlex.quote(mnt)}"
        lines.append(f"$ {cmd}")
        try:
            ssh_run(pve.host, pve.ssh_user, pve.ssh_password, cmd, timeout=30)
            lines.append(f"✓ {label} succeeded")
            return True
        except Exception as e:
            lines.append(f"✗ {label}: {e}")
            return False

    ssh_run(pve.host, pve.ssh_user, pve.ssh_password,
            f"umount {shlex.quote(mnt)} 2>/dev/null || true", timeout=10)

    # Try options in order of preference
    if _try("-o ro", "plain ro"):
        return lines
    if _try("-o ro,norecovery", "ro + norecovery"):
        return lines
    if _try("-o ro,noload", "ro + noload"):
        return lines
    if _try("-o ro,errors=continue", "ro + errors=continue"):
        return lines

    # All attempts failed — raise with full log
    raise RuntimeError("All mount attempts failed:\n" + "\n".join(lines))


def umount_session(pve, nbd_device, mount_base, guest_lvm_vgs=None):
    """Best-effort cleanup: deactivate guest LVMs → umount → disconnect qemu-nbd → remove tmpdir."""
    from ._helpers import ssh_run

    host = pve.host
    user = pve.ssh_user
    pw   = pve.ssh_password
    key  = getattr(pve, "ssh_key", None) or ""

    def _try(cmd):
        try:
            ssh_run(host, user, pw, cmd, key_material=key, timeout=20)
        except Exception as e:
            log.warning(f"[sfr] cleanup step failed: {e}")

    # Step 1: deactivate guest LVM VGs before killing processes / umounting
    for vg in (guest_lvm_vgs or []):
        _try(f"vgchange -an {shlex.quote(vg)} 2>/dev/null; true")

    if mount_base:
        # Kill any processes still using the mountpoint (e.g. lingering SSH cat after cancel),
        # then unmount normally; fall back to lazy umount only if needed.
        _try(
            f"fuser -km {shlex.quote(mount_base)}/mnt 2>/dev/null; "
            f"umount {shlex.quote(mount_base)}/mnt 2>/dev/null || "
            f"umount -l {shlex.quote(mount_base)}/mnt 2>/dev/null || true"
        )
    if nbd_device:
        _try(f"qemu-nbd -d {shlex.quote(nbd_device)} 2>/dev/null || true")
    if mount_base:
        _try(f"rm -rf {shlex.quote(mount_base)}")


# ── SAN mount / cleanup ───────────────────────────────────────────────────────

def mount_san_disk(pve, client, mapping, snap_name, vmid, lv_name, session_id):
    """Clone LUN/namespace from snapshot, map to PVE host, mount via qemu-nbd (read-only).

    Returns {'nbd_device', 'mount_base', 'partitions': [...], 'san_state': {...}}.
    san_state carries all info needed for cleanup in cleanup_san_state().
    """
    from ._helpers import ssh_run, load_plugin_config

    host = pve.host
    user = pve.ssh_user
    pw   = pve.ssh_password
    key  = getattr(pve, "ssh_key", None) or ""

    protocol  = mapping.get("storage_protocol", "nvme")
    svm_name  = mapping["svm_name"]
    vol_uuid  = mapping["volume_uuid"]
    vg_name   = mapping["lvm_vg_name"]
    lvm_type  = mapping.get("lvm_type", "linear")
    pool_name = mapping.get("lvm_pool_name", "")

    poll_cfg    = load_plugin_config()
    poll_ivl    = poll_cfg.get("job_poll_interval_s", 3)
    poll_to     = poll_cfg.get("job_poll_timeout_s", 300)
    token       = uuid.uuid4().hex[:8]
    clone_name  = f"nasnap_sfr_{token}"
    mount_base  = f"{_MOUNT_BASE}{session_id}"

    san = {
        "protocol":                   protocol,
        "clone_name":                 clone_name,
        "temp_lun_uuid":              "",
        "temp_iscsi_clone_vol_uuid":  "",
        "temp_iscsi_serial":          "",
        "igroup_uuid":                "",
        "temp_ns_uuid":               "",
        "subsystem_uuid":             "",
        "temp_vg_name":               "",
        "lv_name":                    lv_name,
    }

    try:
        vol_info = client.get_volume(vol_uuid)
        vol_name = (vol_info or {}).get("name", "")
        if not vol_name:
            raise RuntimeError(f"Cannot resolve volume name for UUID {vol_uuid}")

        device = ""

        if protocol == "iscsi":
            from .san_helpers import rescan_iscsi, find_device_by_serial
            log.info(f"[sfr-san] Cloning iSCSI LUN from snapshot '{snap_name}' …")
            temp_lun_uuid, temp_clone_vol_uuid = client.clone_lun_from_snapshot(
                vol_uuid, snap_name, svm_name, clone_name,
                poll_interval=poll_ivl, poll_timeout=poll_to,
            )
            san["temp_lun_uuid"]             = temp_lun_uuid
            san["temp_iscsi_clone_vol_uuid"] = temp_clone_vol_uuid

            lun_uuid = mapping.get("lun_uuid", "")
            existing_maps = client.list_lun_maps(lun_uuid=lun_uuid) if lun_uuid else []
            if not existing_maps:
                raise RuntimeError("No igroup mapping found for main LUN — re-run discovery")
            igroup_uuid = existing_maps[0]["igroup"]["uuid"]
            san["igroup_uuid"] = igroup_uuid
            client.map_lun(temp_lun_uuid, igroup_uuid, svm_name)

            rescan_iscsi(host, user, pw, key)
            temp_lun_info = client.get_lun(temp_lun_uuid)
            temp_serial   = temp_lun_info.get("serial_number", "")
            san["temp_iscsi_serial"] = temp_serial
            if not temp_serial:
                raise RuntimeError("Cannot determine serial number of clone LUN")
            from .san_helpers import find_device_by_serial as _fds
            device = _fds(host, user, pw, key, temp_serial, timeout_s=60)

        elif protocol == "nvme":
            from .san_helpers import nvme_list_devices, nvme_ns_rescan, find_new_nvme_device
            log.info(f"[sfr-san] Cloning NVMe namespace from snapshot '{snap_name}' …")
            devices_before = nvme_list_devices(host, user, pw, key)

            namespaces = client.list_nvme_namespaces(svm_name=svm_name)
            main_ns = next(
                (ns for ns in namespaces
                 if (ns.get("location") or {}).get("volume", {}).get("uuid") == vol_uuid),
                None,
            )
            if not main_ns:
                main_ns = next(
                    (ns for ns in namespaces
                     if (ns.get("location") or {}).get("volume", {}).get("name") == vol_name),
                    None,
                )
            if not main_ns:
                raise RuntimeError(f"Cannot find NVMe namespace for volume {vol_uuid}")
            main_ns_uuid  = main_ns["uuid"]
            subsystem     = client.get_nvme_subsystem_for_namespace(main_ns_uuid, svm_name=svm_name)
            if not subsystem:
                raise RuntimeError("No NVMe subsystem found for main namespace")
            subsystem_uuid = subsystem["uuid"]
            san["subsystem_uuid"] = subsystem_uuid

            temp_ns_uuid, ns_job = client.clone_namespace(
                main_ns_uuid, snap_name, vol_name, clone_name, svm_name)
            if ns_job:
                client.poll_job(ns_job, interval_s=poll_ivl, timeout_s=poll_to)
            if not temp_ns_uuid:
                raise RuntimeError("clone_namespace returned no UUID")
            san["temp_ns_uuid"] = temp_ns_uuid

            clone_already_mapped = bool(
                client.get_nvme_subsystem_for_namespace(temp_ns_uuid, svm_name=svm_name))
            if not clone_already_mapped:
                client.add_nvme_namespace_to_subsystem(subsystem_uuid, temp_ns_uuid, svm_name=svm_name)

            nvme_ns_rescan(host, user, pw, key)
            device = find_new_nvme_device(host, user, pw, key, devices_before, timeout_s=60)
            san["nvme_device"] = device  # stored for host-side disconnect on cleanup
        else:
            raise RuntimeError(f"Unsupported SAN protocol for SFR: {protocol}")

        log.info(f"[sfr-san] Clone device: {device}")

        # vgimportclone → temp VG
        from .san_helpers import vg_import_clone, activate_lv_for_restore
        temp_vg = vg_import_clone(host, user, pw, key, device, vg_name)
        san["temp_vg_name"] = temp_vg
        log.info(f"[sfr-san] Clone VG: {temp_vg}")

        # Activate the VM's LV (clone VG → read-only nbd, so write to clone is harmless)
        activate_lv_for_restore(host, user, pw, key, temp_vg, lv_name, lvm_type, pool_name)
        lv_device = f"/dev/{temp_vg}/{lv_name}"

        # qemu-nbd: expose LV as block device (read-only)
        ssh_run(host, user, pw, "modprobe nbd max_part=8 2>/dev/null || true", timeout=15)
        nbd_dev = _find_free_nbd(pve)
        ssh_run(host, user, pw,
                f"qemu-nbd --read-only -c {shlex.quote(nbd_dev)} {shlex.quote(lv_device)}",
                timeout=30)
        ssh_run(host, user, pw,
                f"sleep 1; partprobe {shlex.quote(nbd_dev)} 2>/dev/null || true", timeout=15)

        # Partition detection (delegates to shared NFS path logic)
        _ENCRYPTED_FS = {"bitlocker", "crypto_luks", "veracrypt"}
        _SKIP_FS      = {"swap", "linux_raid_member", "lvm2_member"}
        partitions    = []
        lvm_pv_devs   = []
        try:
            ssh_run(host, user, pw, "udevadm settle 2>/dev/null || true", timeout=10)
            out = ssh_run(host, user, pw,
                          f"lsblk -J -o NAME,SIZE,FSTYPE,LABEL {shlex.quote(nbd_dev)}",
                          capture=True, timeout=15)
            children = (json.loads(out).get("blockdevices") or [{}])[0].get("children") or []
            for c in children:
                if not c.get("name"):
                    continue
                dev    = f"/dev/{c['name']}"
                fstype = c.get("fstype") or ""
                label  = c.get("label") or ""
                if not fstype:
                    try:
                        fstype = ssh_run(host, user, pw,
                                         f"blkid -s TYPE -o value {shlex.quote(dev)} 2>/dev/null || true",
                                         capture=True, timeout=10).strip()
                    except Exception:
                        pass
                if not label:
                    try:
                        label = ssh_run(host, user, pw,
                                        f"blkid -s LABEL -o value {shlex.quote(dev)} 2>/dev/null || true",
                                        capture=True, timeout=10).strip()
                    except Exception:
                        pass
                fs = fstype.lower()
                if fs == "lvm2_member":
                    lvm_pv_devs.append(dev)
                partitions.append({
                    "dev":       dev,
                    "size":      c.get("size", "?"),
                    "fstype":    fstype,
                    "label":     label,
                    "encrypted": fs in _ENCRYPTED_FS,
                    "skip":      fs in _ENCRYPTED_FS or fs in _SKIP_FS,
                })
        except Exception as e:
            log.warning(f"[sfr-san] partition scan failed: {e}")

        # Activate guest LVM VGs (LVM inside the VM disk image)
        guest_lvm_vgs = []
        if lvm_pv_devs:
            lv_parts, guest_lvm_vgs = _activate_lvm_guests(host, user, pw, key, lvm_pv_devs)
            partitions.extend(lv_parts)
        san["guest_lvm_vgs"] = guest_lvm_vgs

        ssh_run(host, user, pw, f"mkdir -p {shlex.quote(mount_base)}/mnt", timeout=15)
        return {
            "nbd_device":  nbd_dev,
            "mount_base":  mount_base,
            "partitions":  partitions,
            "san_state":   san,
        }

    except Exception:
        # Partial cleanup on failure
        _san_partial_cleanup(pve, client, san)
        raise


def _san_partial_cleanup(pve, client, san):
    """Best-effort partial cleanup when mount_san_disk fails mid-way."""
    from ._helpers import ssh_run

    def _try(fn):
        try:
            fn()
        except Exception as e:
            log.warning(f"[sfr-san] partial cleanup step: {e}")

    host = pve.host
    user = pve.ssh_user
    pw   = pve.ssh_password
    key  = getattr(pve, "ssh_key", None) or ""

    for vg in san.get("guest_lvm_vgs", []):
        _try(lambda vg=vg: ssh_run(host, user, pw,
                                   f"vgchange -an {shlex.quote(vg)} 2>/dev/null; true",
                                   key_material=key, timeout=15))

    temp_vg = san.get("temp_vg_name", "")
    if temp_vg:
        from .san_helpers import cleanup_restore_vg
        _try(lambda: cleanup_restore_vg(host, user, pw, key, temp_vg))

    if san.get("protocol") == "nvme":
        nvme_device = san.get("nvme_device", "")
        if nvme_device:
            import re as _re
            m = _re.match(r'(/dev/nvme\d+)', nvme_device)
            if m:
                ctrl = shlex.quote(m.group(1))
                _try(lambda: ssh_run(host, user, pw,
                                     f"timeout 15 nvme disconnect --device {ctrl} 2>/dev/null; true",
                                     key_material=key, timeout=25))

    _san_cleanup_ontap(client, san)


def cleanup_san_state(pve, client, san_state):
    """Full SAN cleanup after a session closes: guest LVMs → outer VG → host disconnect → ONTAP clone.

    Order matters for PVE stability:
      1. Deactivate guest LVM VGs (inside the VM disk image, accessed via nbd)
      2. Deactivate + remove the outer restore VG (datastore-level clone VG)
      3. Host-side protocol disconnect (iSCSI flush / NVMe disconnect)
      4. ONTAP clone cleanup (unmap + delete LUN/namespace + delete volume)
    """
    from ._helpers import ssh_run

    def _try(fn):
        try:
            fn()
        except Exception as e:
            log.warning(f"[sfr-san] cleanup: {e}")

    host = pve.host
    user = pve.ssh_user
    pw   = pve.ssh_password
    key  = getattr(pve, "ssh_key", None) or ""

    # Step 1: deactivate guest LVM VGs (inside the VM disk, mounted via nbd)
    for vg in san_state.get("guest_lvm_vgs", []):
        _try(lambda vg=vg: ssh_run(host, user, pw,
                                   f"vgchange -an {shlex.quote(vg)} 2>/dev/null; true",
                                   key_material=key, timeout=15))

    # Step 2: remove the outer restore VG (clone of datastore LVM VG)
    temp_vg = san_state.get("temp_vg_name", "")
    if temp_vg:
        from .san_helpers import cleanup_restore_vg
        _try(lambda: cleanup_restore_vg(host, user, pw, key, temp_vg))

    # Step 3: host-side protocol disconnect
    protocol = san_state.get("protocol", "")

    if protocol == "nvme":
        # Disconnect the NVMe controller that served the clone namespace.
        # We stored the device path at mount time; use it directly since the VG is gone.
        nvme_device = san_state.get("nvme_device", "")
        if nvme_device:
            import re as _re
            m = _re.match(r'(/dev/nvme\d+)', nvme_device)
            if m:
                ctrl = shlex.quote(m.group(1))
                _try(lambda: ssh_run(host, user, pw,
                                     f"timeout 15 nvme disconnect --device {ctrl} 2>/dev/null; true",
                                     key_material=key, timeout=25))
                log.info(f"[sfr-san] NVMe disconnect {m.group(1)} on {host}")

    elif protocol == "iscsi":
        temp_iscsi_serial = san_state.get("temp_iscsi_serial", "")
        if temp_iscsi_serial:
            from .san_helpers import flush_iscsi_clone_device
            _try(lambda: flush_iscsi_clone_device(host, user, pw, key, temp_iscsi_serial))

    # Step 4: delete ONTAP clone (unmap + delete namespace/LUN + delete volume)
    _san_cleanup_ontap(client, san_state)


def _san_cleanup_ontap(client, san):
    """Delete the ONTAP clone LUN/namespace and its volume (if applicable)."""
    def _try(fn):
        try:
            fn()
        except Exception as e:
            log.warning(f"[sfr-san] ONTAP cleanup: {e}")

    protocol = san.get("protocol", "")

    if protocol == "iscsi":
        lun_uuid  = san.get("temp_lun_uuid", "")
        vol_uuid  = san.get("temp_iscsi_clone_vol_uuid", "")
        ig_uuid   = san.get("igroup_uuid", "")
        if lun_uuid and ig_uuid:
            _try(lambda: client.unmap_lun(lun_uuid, ig_uuid))
        if vol_uuid:
            _try(lambda: client.delete_volume(vol_uuid))
        elif lun_uuid:
            _try(lambda: client.delete_lun(lun_uuid))

    elif protocol == "nvme":
        ns_uuid  = san.get("temp_ns_uuid", "")
        sub_uuid = san.get("subsystem_uuid", "")
        if ns_uuid and sub_uuid:
            _try(lambda: client.remove_nvme_namespace_from_subsystem(sub_uuid, ns_uuid))
        if ns_uuid:
            _try(lambda: client.delete_namespace(ns_uuid))


# ── Browsing: snapshot ────────────────────────────────────────────────────────

def snap_ls(pve, mount_path, rel_path):
    """List a directory in the mounted snapshot via SSH."""
    from ._helpers import ssh_run
    rel = rel_path.strip("/")
    full = f"{mount_path}/{rel}" if rel else mount_path
    try:
        out = ssh_run(pve.host, pve.ssh_user, pve.ssh_password,
                      f"ls -la --time-style=long-iso {shlex.quote(full)} 2>&1",
                      capture=True, timeout=15)
        return _parse_ls(out)
    except Exception as e:
        return {"error": str(e), "entries": []}


def _parse_ls(output):
    entries = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("total ") or "ls: cannot" in line:
            continue
        parts = line.split(None, 8)
        if len(parts) < 8:
            continue
        perm, size, date, time_ = parts[0], parts[4], parts[5], parts[6]
        rest = parts[7] + (" " + parts[8] if len(parts) > 8 else "")
        link_target = None
        if " -> " in rest:
            name_part, link_target = rest.split(" -> ", 1)
        else:
            name_part = rest
        name = name_part.strip()
        if not name or name in (".", ".."):
            continue
        entry_type = "d" if perm.startswith("d") else ("l" if perm.startswith("l") else "f")
        try:
            sz = int(size)
        except (ValueError, TypeError):
            sz = 0
        entries.append({
            "name": name,
            "type": entry_type,
            "size": sz,
            "modified": f"{date} {time_}",
        })
    return {"entries": entries}


# ── Browsing: live VM ─────────────────────────────────────────────────────────

def vm_ls(pve, vmid, node, path, guest_os="linux"):
    """List a directory in the live VM via QGA."""
    try:
        if guest_os == "windows":
            return _vm_ls_windows(pve, vmid, node, path)
        return _vm_ls_linux(pve, vmid, node, path)
    except Exception as e:
        return {"error": str(e), "entries": []}


def _vm_ls_linux(pve, vmid, node, path):
    p = path or "/"
    out, _, _ = _qga_exec(pve, vmid, node,
                           ["ls", "-la", "--time-style=long-iso", p], timeout=15)
    return _parse_ls(out)


def _vm_ls_windows(pve, vmid, node, path):
    """List a directory inside a Windows VM via QGA.

    When path is '/' or empty, returns the list of available drive letters instead.
    Uses PowerShell for locale-independent output; falls back to cmd.exe dir.
    """
    if not path or path in ("/", "\\"):
        return _vm_ls_windows_drives(pve, vmid, node)

    # Normalise to Windows backslash path with trailing separator
    p = path.replace("/", "\\")
    if not p.endswith("\\"):
        p += "\\"

    # PowerShell: locale-independent pipe-separated output
    ps_cmd = (
        "Get-ChildItem -Path '" + p.replace("'", "''") + "' -Force -ErrorAction SilentlyContinue"
        " | ForEach-Object {"
        " ($(if($_.PSIsContainer){'d'}else{'f'})+'|'+$_.Name+'|'+$_.Length"
        "+'|'+$_.LastWriteTime.ToString('yyyy-MM-dd HH:mm')) }"
    )
    try:
        out, _, rc = _qga_exec(
            pve, vmid, node,
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            timeout=20,
        )
        if rc == 0 and out.strip():
            return _parse_ps_ls(out)
    except Exception:
        pass

    # Fallback: cmd.exe dir
    try:
        out, _, _ = _qga_exec(
            pve, vmid, node,
            ["cmd.exe", "/c", f"dir /a:-s /q \"{p}\""],
            timeout=15,
        )
        return _parse_dir_windows(out)
    except Exception as e:
        return {"entries": [], "error": str(e)}


def _vm_ls_windows_drives(pve, vmid, node):
    """Return available Windows drive letters via fsutil fsinfo drives."""
    try:
        out, _, _ = _qga_exec(pve, vmid, node,
                               ["cmd.exe", "/c", "fsutil fsinfo drives"], timeout=10)
        entries = []
        for part in out.split():
            # Output looks like:  Drives: C:\ D:\
            part = part.strip().rstrip("\\")
            if len(part) == 2 and part[1] == ":" and part[0].isalpha():
                entries.append({"name": part[0].upper() + ":\\",
                                 "type": "d", "size": 0, "modified": ""})
        if entries:
            return {"entries": entries, "is_drives": True}
    except Exception:
        pass
    # Fallback: assume C: always exists
    return {"entries": [{"name": "C:\\", "type": "d", "size": 0, "modified": ""}],
            "is_drives": True}


def _parse_ps_ls(output):
    """Parse pipe-separated PowerShell Get-ChildItem output."""
    entries = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) < 2:
            continue
        ftype    = parts[0]
        name     = parts[1]
        size_s   = parts[2] if len(parts) > 2 else ""
        modified = parts[3].strip() if len(parts) > 3 else ""
        if not name or name in (".", "..") or ftype not in ("d", "f"):
            continue
        try:
            size = 0 if ftype == "d" else int(size_s or 0)
        except ValueError:
            size = 0
        entries.append({"name": name, "type": ftype, "size": size, "modified": modified})
    return {"entries": entries}


def _parse_dir_windows(output):
    """Parse English-locale cmd.exe dir /q output (fallback)."""
    entries = []
    for line in output.strip().splitlines():
        # date time  <DIR>|size  owner  name
        m = re.match(
            r"(\S+)\s+(\S+\s*(?:[AP]M)?)\s+(<DIR>|[\d,]+)\s+\S+\s+(.*)",
            line.strip(),
        )
        if not m:
            continue
        date_s, time_s, size_s, name = m.group(1), m.group(2).strip(), m.group(3), m.group(4).strip()
        if not name or name in (".", ".."):
            continue
        is_dir = size_s == "<DIR>"
        entries.append({
            "name": name,
            "type": "d" if is_dir else "f",
            "size": 0 if is_dir else int(size_s.replace(",", "").replace(".", "")),
            "modified": f"{date_s} {time_s}",
        })
    return {"entries": entries}


# ── VM operations ──────────────────────────────────────────────────────────────

def vm_mkdir(pve, vmid, node, path, guest_os="linux"):
    if guest_os == "windows":
        _qga_exec(pve, vmid, node, ["cmd.exe", "/c", f"mkdir \"{path}\""], timeout=15)
    else:
        _qga_exec(pve, vmid, node, ["mkdir", "-p", path], timeout=15)


def vm_delete(pve, vmid, node, path, guest_os="linux"):
    if guest_os == "windows":
        _qga_exec(pve, vmid, node,
                  ["cmd.exe", "/c",
                   f"(del /q /f \"{path}\" 2>nul) || (rmdir /s /q \"{path}\" 2>nul)"],
                  timeout=30)
    else:
        _qga_exec(pve, vmid, node, ["rm", "-rf", path], timeout=30)


# ≤ 4 MB: single file-write call (fast path)
_QGA_SMALL    = 4 * 1024 * 1024
# Conservative default chunk size — replaced at runtime by _probe_qga_fw_limit()
_QGA_FW_CHUNK = 30 * 1024

# Per-PVE-endpoint cache: _base URL → discovered binary chunk limit in bytes
_qga_fw_limit_cache: dict = {}
# Files larger than this get a clear "use Download" error instead of a very long transfer
# Set to 0 to disable the limit.
SFR_COPY_LIMIT = int(os.environ.get("SFR_COPY_LIMIT_MB", "200")) * 1024 * 1024


def copy_file_to_vm(pve, mount_path, snap_rel, vmid, node, vm_dest_path,
                    guest_os="linux", progress_cb=None, cancel_check=None):
    """Copy a file from the mounted snapshot into the VM via QGA.

    All data travels through the QGA socket on the PVE host — no VM network
    access required.  Works in DMZ / isolated VLAN environments.

    Small files (≤ 4 MB):  single file-write call.
    Larger files (Linux):   chunked — probed chunk size → temp files → cat.
    Windows large files:    not supported; use Download.
    Files over SFR_COPY_LIMIT: rejected with clear message.
    progress_cb(bytes_done, total_bytes) called after each SSH block.
    cancel_check() called before each SSH block; should raise to abort.
    """
    from ._helpers import ssh_run
    full = f"{mount_path}/{snap_rel.lstrip('/')}"

    try:
        size_str = ssh_run(pve.host, pve.ssh_user, pve.ssh_password,
                           f"stat -c%s {shlex.quote(full)}", capture=True, timeout=10).strip()
        file_size = int(size_str)
    except Exception:
        file_size = 0

    if SFR_COPY_LIMIT and file_size > SFR_COPY_LIMIT:
        mb = file_size // 1048576
        lim = SFR_COPY_LIMIT // 1048576
        raise RuntimeError(
            f"File is {mb} MB — exceeds the {lim} MB copy limit. "
            "Use ↓ Download to save the file locally and copy it to the VM yourself."
        )

    if file_size <= _QGA_SMALL:
        if cancel_check:
            cancel_check()
        if progress_cb:
            progress_cb(0, file_size)
        _qga_write_single(pve, vmid, node, pve.host, pve.ssh_user, pve.ssh_password,
                          full, vm_dest_path)
        if progress_cb:
            progress_cb(file_size, file_size)
        return

    if guest_os == "windows":
        raise RuntimeError(
            f"File is {file_size // 1048576} MB — large-file copy is not supported for "
            "Windows VMs. Use ↓ Download instead."
        )

    _qga_write_chunked(pve, vmid, node, pve.host, pve.ssh_user, pve.ssh_password,
                       full, vm_dest_path, file_size, progress_cb, cancel_check)


def _qga_write_single(pve, vmid, node, host, user, pw, src_path, vm_dest_path):
    from ._helpers import ssh_run
    b64 = ssh_run(host, user, pw,
                  f"base64 {shlex.quote(src_path)}", capture=True, timeout=60)
    b64 = b64.replace("\n", "").replace("\r", "").strip()
    if not b64:
        raise RuntimeError("base64 returned empty — file may be empty or unreadable")
    r = pve._api_post(
        f"{pve._base}/nodes/{node}/qemu/{vmid}/agent/file-write",
        {"file": vm_dest_path, "content": b64, "encode": 1},
    )
    if not r.ok:
        raise RuntimeError(f"QGA file-write failed ({r.status_code}): {r.text[:200]}")


def _probe_qga_fw_limit(pve, vmid, node):
    """Discover actual PVE agent/file-write binary size limit via binary search.

    Writes null-byte test payloads via QGA, doubling size until PVE rejects the
    request, then narrows with binary search to ±8 KB precision.  Result is cached
    per PVE endpoint so the probe runs at most once per NaSnap process lifetime.
    Returns the safe binary byte limit (90 % of discovered maximum).
    """
    import base64 as _b64

    cache_key = pve._base
    if cache_key in _qga_fw_limit_cache:
        return _qga_fw_limit_cache[cache_key]

    tmp = f"/tmp/.nasnap_fw_probe_{uuid.uuid4().hex[:6]}"

    def _try(n):
        data = _b64.b64encode(bytes(n)).decode()
        r = pve._api_post(
            f"{pve._base}/nodes/{node}/qemu/{vmid}/agent/file-write",
            {"file": tmp, "content": data, "encode": 1},
        )
        return r.ok

    try:
        # Phase 1: double from 4 KB until PVE rejects or we reach the 512 KB ceiling.
        # Cap at 512 KB (640 KB base64) — well above any known PVE limit, avoids
        # sending multi-MB payloads that could hit HTTP body size limits on pveproxy.
        lo = 4 * 1024
        hi = lo
        while hi <= 512 * 1024:
            if not _try(hi):
                break
            lo = hi
            hi = hi * 2
        else:
            # Loop exited normally: all sizes up to 512 KB were accepted
            lo = 512 * 1024

        # Phase 2: binary search between lo (last accepted) and hi (first rejected)
        # Condition: gap > 8 KB (there is room to narrow further)
        while hi - lo > 8 * 1024:
            mid = (lo + hi) // 2
            if _try(mid):
                lo = mid
            else:
                hi = mid

        limit = int(lo * 0.9)  # 10 % safety margin
        log.info(f"[sfr] QGA file-write probe: max={lo // 1024} KB → safe chunk={limit // 1024} KB")
    except Exception as exc:
        log.warning(f"[sfr] QGA file-write probe failed: {exc} — using default {_QGA_FW_CHUNK // 1024} KB")
        limit = _QGA_FW_CHUNK
    finally:
        try:
            _qga_exec(pve, vmid, node, ["bash", "-c", f"rm -f {shlex.quote(tmp)}"], timeout=10)
        except Exception:
            pass

    _qga_fw_limit_cache[cache_key] = limit
    return limit


def _read_n(fh, n):
    """Read exactly n bytes from a paramiko ChannelFile, blocking until data or EOF.

    paramiko's read(n) may return fewer bytes than requested when the SSH receive
    window has less data buffered; this wrapper retries until we have n bytes or
    the channel signals EOF.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = fh.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _qga_write_chunked(pve, vmid, node, host, user, pw,
                       src_path, vm_dest_path, file_size, progress_cb=None, cancel_check=None):
    """Write a large file into the VM via QGA — no VM network required.

    Optimisations vs the original block-based approach:
    1. File is streamed in a single SSH channel (`cat`) — no per-block `dd` seek,
       no repeated channel opens.  Python reads chunk_size raw bytes at a time and
       base64-encodes them locally (fast, avoids base64 process on PVE host).
    2. Auto-probed chunk size — binary-searches PVE's file-write limit so we use
       the largest safe chunk, minimising the number of API round-trips.
    3. Progress is reported after every single QGA write so the UI bar moves
       immediately from the very first chunk.

    Data path: NFS snapshot ──SSH/cat──▶ Python ──HTTPS/PVE API──▶ QGA ──▶ VM disk
    """
    import base64 as _b64
    from ._helpers import SshSession

    # Signal file size immediately so the UI shows a deterministic progress bar
    # while the probe is running — avoids "Connecting…" during that phase.
    if progress_cb:
        progress_cb(0, file_size)

    # Discover actual chunk limit once per PVE endpoint
    chunk_size = _probe_qga_fw_limit(pve, vmid, node)

    token       = uuid.uuid4().hex[:8]
    tmp_prefix  = f"/tmp/.nasnap_sfr_{token}"
    chunk_paths = []
    bytes_done  = 0
    chunk_idx   = 0
    est_chunks  = (file_size + chunk_size - 1) // chunk_size

    log.info(
        f"[sfr] chunked QGA: {file_size // 1048576} MB, chunk={chunk_size // 1024} KB, "
        f"~{est_chunks} PVE calls (streaming)"
    )

    try:
        with SshSession(host, user, pw) as ssh:
            # Stream the entire file in one SSH channel — no per-block dd+skip overhead.
            _, out_fh, err_fh = ssh._client.exec_command(
                f"cat {shlex.quote(src_path)}", timeout=600
            )
            try:
                while True:
                    if cancel_check:
                        cancel_check()  # raises if user cancelled

                    raw = _read_n(out_fh, chunk_size)
                    if not raw:
                        break  # EOF

                    piece_b64 = _b64.b64encode(raw).decode()
                    cpath     = f"{tmp_prefix}_c{chunk_idx:06d}"
                    r = pve._api_post(
                        f"{pve._base}/nodes/{node}/qemu/{vmid}/agent/file-write",
                        {"file": cpath, "content": piece_b64, "encode": 1},
                    )
                    if not r.ok:
                        raise RuntimeError(
                            f"QGA file-write chunk {chunk_idx} failed "
                            f"({r.status_code}): {r.text[:120]}"
                        )
                    chunk_paths.append(cpath)
                    chunk_idx += 1
                    bytes_done += len(raw)

                    if progress_cb:
                        progress_cb(bytes_done, file_size)
                    log.debug(f"[sfr] {bytes_done // 1048576}/{file_size // 1048576} MB")
            finally:
                # Drain exit status so the channel closes cleanly.
                try:
                    out_fh.channel.recv_exit_status()
                except Exception:
                    pass

        if not chunk_paths:
            raise RuntimeError("No data read from source file")

        # Assemble in VM: ls | sort | xargs cat → dest, then clean up
        log.info(f"[sfr] concat {len(chunk_paths)} chunks → {vm_dest_path}")
        pat     = shlex.quote(f"{tmp_prefix}_c??????")
        cat_cmd = (
            f"ls {pat} | sort | xargs cat > {shlex.quote(vm_dest_path)}"
            f" && rm -f {pat}"
        )
        _, stderr, rc = _qga_exec(pve, vmid, node, ["bash", "-c", cat_cmd], timeout=300)
        if rc != 0:
            raise RuntimeError(f"Chunk concat failed (rc={rc}): {stderr[:200]}")

    except Exception:
        if chunk_paths:
            try:
                pat = shlex.quote(f"{tmp_prefix}_c??????")
                _qga_exec(pve, vmid, node, ["bash", "-c", f"rm -f {pat}"], timeout=30)
            except Exception:
                pass
        raise

    if progress_cb:
        progress_cb(file_size, file_size)


# ── Download from snapshot ────────────────────────────────────────────────────

def read_snap_file_bytes(pve, mount_path, rel_path):
    """Returns raw bytes of a file from the mounted snapshot."""
    import base64
    from ._helpers import ssh_run
    full = f"{mount_path}/{rel_path.lstrip('/')}"
    b64 = ssh_run(pve.host, pve.ssh_user, pve.ssh_password,
                  f"base64 {shlex.quote(full)}", capture=True, timeout=120)
    return base64.b64decode(b64.replace("\n", "").replace("\r", "").strip())


def snap_tar_bytes(pve, mount_path, rel_path):
    """Returns bytes of a tar.gz of a path inside the mounted snapshot."""
    import base64
    from ._helpers import ssh_run
    full = f"{mount_path}/{rel_path.lstrip('/')}"
    parent = full.rsplit("/", 1)[0] if "/" in full else mount_path
    name   = full.rsplit("/", 1)[1] if "/" in full else rel_path.strip("/")
    b64 = ssh_run(pve.host, pve.ssh_user, pve.ssh_password,
                  f"tar -czf - -C {shlex.quote(parent)} {shlex.quote(name)} | base64",
                  capture=True, timeout=300)
    return base64.b64decode(b64.replace("\n", "").replace("\r", "").strip())
