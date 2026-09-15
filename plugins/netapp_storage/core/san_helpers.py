"""
SAN helpers for iSCSI/NVMe-oF snapshots and restores.

Encapsulates all SSH-based LVM operations on PVE nodes:
  - create / write / unmount snapmanifest LV
  - import cloned LUN for restore (vgimportclone)
  - block-copy LV data (dd)
  - detect LVM type (linear vs. thin)
  - iSCSI rescan + device lookup by serial number

Requirements on PVE nodes:
  lvm2, e2fsprogs, open-iscsi (for iSCSI), nvme-cli (for NVMe)
"""

import json
import logging
import shlex
import time
import uuid as _uuid

from ._helpers import ssh_run

log = logging.getLogger(__name__)


# ── LVM type detection ─────────────────────────────────────────────────────────

def get_vg_lv_map(ssh_host, ssh_user, ssh_pass, ssh_key, vg_name):
    """Returns all LVs of a VG: {lv_name: {'size': str, 'attr': str}}."""
    vg_q = shlex.quote(vg_name)
    try:
        out = ssh_run(
            ssh_host, ssh_user, ssh_pass,
            f"lvs --noheadings -o lv_name,lv_size,lv_attr {vg_q} 2>/dev/null",
            capture=True, key_material=ssh_key,
        )
        result = {}
        for line in out.splitlines():
            parts = line.split()
            if parts:
                result[parts[0].strip()] = {
                    "size": parts[1] if len(parts) > 1 else "",
                    "attr": parts[2] if len(parts) > 2 else "",
                }
        return result
    except Exception as exc:
        log.warning(f"[netapp_storage] LV list {vg_name} failed: {exc}")
        return {}


# ── snapmanifest LV: setup ────────────────────────────────────────────────────────

def snapmanifest_initialize(ssh_host, ssh_user, ssh_pass, ssh_key,
                        vg_name, lv_name="netapp_snapmanifest", size_mb=64):
    """Creates the snapmanifest LV and formats it with ext4 (idempotent).

    The LV is created as a regular (thick) LV directly in the VG —
    not inside a thin pool — so it can be activated without the thin-pool daemon.

    Raises RuntimeError on failure.
    """
    vg_q  = shlex.quote(vg_name)
    lv_q  = shlex.quote(lv_name)
    dev_q = shlex.quote(f"/dev/{vg_name}/{lv_name}")

    # Idempotenz: already exists?
    out = ssh_run(
        ssh_host, ssh_user, ssh_pass,
        f"lvs {vg_q}/{lv_q} 2>/dev/null && echo EXISTS || true",
        capture=True, key_material=ssh_key,
    )
    if "EXISTS" in out:
        log.info(f"[netapp_storage] snapmanifest LV {vg_name}/{lv_name} already exists")
        return True

    # Check free space
    free_out = ssh_run(
        ssh_host, ssh_user, ssh_pass,
        f"vgs --noheadings --units m --nosuffix -o vg_free {vg_q} 2>/dev/null",
        capture=True, key_material=ssh_key,
    )
    try:
        free_mb = float(free_out.strip())
        if free_mb < size_mb:
            raise RuntimeError(
                f"VG {vg_name}: only {free_mb:.0f} MB free, {size_mb} MB required."
            )
    except ValueError:
        pass  # parsing failed, try anyway

    ssh_run(
        ssh_host, ssh_user, ssh_pass,
        f"lvcreate -L {size_mb}M -n {lv_q} {vg_q}",
        key_material=ssh_key,
    )
    ssh_run(
        ssh_host, ssh_user, ssh_pass,
        f"mkfs.ext4 -F {dev_q}",
        key_material=ssh_key, timeout=60,
    )
    log.info(f"[netapp_storage] snapmanifest LV {vg_name}/{lv_name} created ({size_mb} MB, ext4)")
    return True


# ── snapmanifest LV: write manifest ───────────────────────────────────────────────

def snapmanifest_write_manifest(ssh_host, ssh_user, ssh_pass, ssh_key,
                             vg_name, lv_name, manifest, jlog=None):
    """Activates the snapmanifest LV exclusively, writes the manifest, unmounts.

    manifest: dict — stored as JSON plus individual VM config files.
    Cleanup (umount + deactivate) always runs, even on error.
    """
    vg_q  = shlex.quote(vg_name)
    lv_q  = shlex.quote(lv_name)
    dev   = f"/dev/{vg_name}/{lv_name}"
    mp    = f"/tmp/.pgsi_{_uuid.uuid4().hex[:10]}"
    mp_q  = shlex.quote(mp)

    def _log(msg):
        log.info(f"[netapp_storage] {msg}")
        if jlog:
            jlog.log(msg)

    activated = False
    mounted   = False
    try:
        _log(f"Activating snapmanifest LV ({vg_name}/{lv_name}) …")
        # -aey: exclusive activation via lvmlockd (cluster-safe)
        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"lvchange -aey {vg_q}/{lv_q}",
                key_material=ssh_key)
        activated = True

        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"mkdir -p {mp_q} && mount {shlex.quote(dev)} {mp_q}",
                key_material=ssh_key)
        mounted = True

        # manifest.json via stdin (avoids shell quoting issues with JSON content)
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"cat > {mp_q}/manifest.json",
                stdin_data=manifest_bytes, key_material=ssh_key)

        # Einzelne VM-Config-Dateien
        for vm in manifest.get("vms", []):
            vmid   = vm.get("vmid")
            config = vm.get("config", "")
            if vmid and config:
                dest_q = shlex.quote(f"{mp}/vmconfigs/{vmid}.conf")
                ssh_run(ssh_host, ssh_user, ssh_pass,
                        f"mkdir -p {mp_q}/vmconfigs && cat > {dest_q}",
                        stdin_data=(config.encode("utf-8") if isinstance(config, str) else config),
                        key_material=ssh_key)

        ssh_run(ssh_host, ssh_user, ssh_pass, "sync", key_material=ssh_key)
        _log("snapmanifest manifest written.")

    finally:
        if mounted:
            try:
                ssh_run(ssh_host, ssh_user, ssh_pass,
                        f"umount {mp_q} 2>/dev/null; rmdir {mp_q} 2>/dev/null",
                        key_material=ssh_key)
            except Exception as exc:
                log.warning(f"[netapp_storage] snapmanifest umount failed: {exc}")
        if activated:
            try:
                ssh_run(ssh_host, ssh_user, ssh_pass,
                        f"lvchange -an {vg_q}/{lv_q}",
                        key_material=ssh_key)
            except Exception as exc:
                log.warning(f"[netapp_storage] snapmanifest deactivate failed: {exc}")


# ── snapmanifest LV: read manifest ───────────────────────────────────────────

def snapmanifest_read_manifest(ssh_host, ssh_user, ssh_pass, ssh_key,
                               vg_name, lv_name="netapp_snapmanifest"):
    """Reads manifest.json from a snapmanifest LV (e.g. in a temp clone VG).

    Returns the parsed manifest dict.
    Raises RuntimeError if the LV does not exist or manifest.json can't be read.
    """
    vg_q  = shlex.quote(vg_name)
    lv_q  = shlex.quote(lv_name)
    dev   = f"/dev/{vg_name}/{lv_name}"
    mp    = f"/tmp/.pgsi_{_uuid.uuid4().hex[:10]}"
    mp_q  = shlex.quote(mp)

    # Check LV exists
    out = ssh_run(ssh_host, ssh_user, ssh_pass,
                  f"lvs {vg_q}/{lv_q} 2>/dev/null && echo EXISTS || echo MISSING",
                  capture=True, key_material=ssh_key)
    if "MISSING" in out:
        raise RuntimeError(f"snapmanifest LV {vg_name}/{lv_name} not found in VG")

    activated = False
    mounted   = False
    try:
        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"lvchange -aey {vg_q}/{lv_q}",
                key_material=ssh_key)
        activated = True

        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"mkdir -p {mp_q} && mount -o ro {shlex.quote(dev)} {mp_q}",
                key_material=ssh_key)
        mounted = True

        raw = ssh_run(ssh_host, ssh_user, ssh_pass,
                      f"cat {mp_q}/manifest.json",
                      capture=True, key_material=ssh_key)
        return json.loads(raw)

    except json.JSONDecodeError as exc:
        raise RuntimeError(f"snapmanifest manifest.json malformed: {exc}")
    finally:
        if mounted:
            try:
                ssh_run(ssh_host, ssh_user, ssh_pass,
                        f"umount {mp_q} 2>/dev/null; rmdir {mp_q} 2>/dev/null",
                        key_material=ssh_key)
            except Exception as exc:
                log.warning(f"[netapp_storage] snapmanifest read umount failed: {exc}")
        if activated:
            try:
                ssh_run(ssh_host, ssh_user, ssh_pass,
                        f"lvchange -an {vg_q}/{lv_q}",
                        key_material=ssh_key)
            except Exception as exc:
                log.warning(f"[netapp_storage] snapmanifest read deactivate failed: {exc}")


# ── snapmanifest LV: index read/write ────────────────────────────────────────

def snapmanifest_write_index(ssh_host, ssh_user, ssh_pass, ssh_key,
                              vg_name, lv_name, snap_entry, datastore_info, jlog=None):
    """Reads the current index.json from the snapmanifest LV, prepends snap_entry,
    and writes it back atomically. Creates a fresh index if none exists yet.
    Called just before the ONTAP snapshot so the index bakes into every snapshot.
    """
    from .datastore_index import _new_index, _now_iso

    vg_q = shlex.quote(vg_name)
    lv_q = shlex.quote(lv_name)
    dev  = f"/dev/{vg_name}/{lv_name}"
    mp   = f"/tmp/.pgsi_{_uuid.uuid4().hex[:10]}"
    mp_q = shlex.quote(mp)

    def _log(msg):
        log.info(f"[netapp_storage] {msg}")
        if jlog:
            jlog.log(msg)

    activated = False
    mounted   = False
    try:
        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"lvchange -aey {vg_q}/{lv_q}", key_material=ssh_key)
        activated = True

        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"mkdir -p {mp_q} && mount {shlex.quote(dev)} {mp_q}",
                key_material=ssh_key)
        mounted = True

        sentinel = "__NASNAP_NOT_FOUND__"
        raw = ssh_run(ssh_host, ssh_user, ssh_pass,
                      f"cat {mp_q}/index.json 2>/dev/null || printf {shlex.quote(sentinel)}",
                      capture=True, key_material=ssh_key)
        if sentinel in (raw or ""):
            index = _new_index(datastore_info)
        else:
            try:
                index = json.loads(raw)
            except Exception:
                index = _new_index(datastore_info)

        index["vms_current"] = datastore_info.get("vms_current", [])
        index["last_updated"] = _now_iso()
        snapshots = index.get("snapshots", [])
        snapshots.insert(0, snap_entry)
        index["snapshots"] = snapshots

        data = json.dumps(index, indent=2).encode()
        tmp_q = shlex.quote(f"{mp}/index.json.tmp")
        idx_q = shlex.quote(f"{mp}/index.json")
        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"cat > {tmp_q}", stdin_data=data, key_material=ssh_key)
        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"mv {tmp_q} {idx_q} && sync", key_material=ssh_key)
        _log("Datastore index written to snapmanifest LV.")

    finally:
        if mounted:
            try:
                ssh_run(ssh_host, ssh_user, ssh_pass,
                        f"umount {mp_q} 2>/dev/null; rmdir {mp_q} 2>/dev/null",
                        key_material=ssh_key)
            except Exception as exc:
                log.warning(f"[netapp_storage] snapmanifest index umount failed: {exc}")
        if activated:
            try:
                ssh_run(ssh_host, ssh_user, ssh_pass,
                        f"lvchange -an {vg_q}/{lv_q}", key_material=ssh_key)
            except Exception as exc:
                log.warning(f"[netapp_storage] snapmanifest index deactivate failed: {exc}")


def snapmanifest_read_index(ssh_host, ssh_user, ssh_pass, ssh_key,
                             vg_name, lv_name="netapp_snapmanifest"):
    """Reads index.json from the snapmanifest LV. Returns dict or None."""
    vg_q = shlex.quote(vg_name)
    lv_q = shlex.quote(lv_name)
    dev  = f"/dev/{vg_name}/{lv_name}"
    mp   = f"/tmp/.pgsi_{_uuid.uuid4().hex[:10]}"
    mp_q = shlex.quote(mp)

    out = ssh_run(ssh_host, ssh_user, ssh_pass,
                  f"lvs {vg_q}/{lv_q} 2>/dev/null && echo EXISTS || echo MISSING",
                  capture=True, key_material=ssh_key)
    if "MISSING" in (out or ""):
        return None

    activated = False
    mounted   = False
    try:
        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"lvchange -aey {vg_q}/{lv_q}", key_material=ssh_key)
        activated = True

        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"mkdir -p {mp_q} && mount -o ro {shlex.quote(dev)} {mp_q}",
                key_material=ssh_key)
        mounted = True

        sentinel = "__NASNAP_NOT_FOUND__"
        raw = ssh_run(ssh_host, ssh_user, ssh_pass,
                      f"cat {mp_q}/index.json 2>/dev/null || printf {shlex.quote(sentinel)}",
                      capture=True, key_material=ssh_key)
        if sentinel in (raw or ""):
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    finally:
        if mounted:
            try:
                ssh_run(ssh_host, ssh_user, ssh_pass,
                        f"umount {mp_q} 2>/dev/null; rmdir {mp_q} 2>/dev/null",
                        key_material=ssh_key)
            except Exception as exc:
                log.warning(f"[netapp_storage] snapmanifest index read umount failed: {exc}")
        if activated:
            try:
                ssh_run(ssh_host, ssh_user, ssh_pass,
                        f"lvchange -an {vg_q}/{lv_q}", key_material=ssh_key)
            except Exception as exc:
                log.warning(f"[netapp_storage] snapmanifest index read deactivate failed: {exc}")


# ── iSCSI: Rescan + Device-Lookup ────────────────────────────────────────────

def get_iscsi_initiator_iqn(ssh_host, ssh_user, ssh_pass, ssh_key):
    """Returns the iSCSI initiator IQN configured on the host, or ''."""
    try:
        out = ssh_run(
            ssh_host, ssh_user, ssh_pass,
            "awk -F= '/^InitiatorName/{print $2}' /etc/iscsi/initiatorname.iscsi 2>/dev/null",
            capture=True, key_material=ssh_key, timeout=10,
        )
        iqn = out.strip()
        if iqn.startswith("iqn."):
            log.info(f"[netapp_storage] IQN of {ssh_host}: {iqn}")
            return iqn
    except Exception as exc:
        log.warning(f"[netapp_storage] Cannot get IQN from {ssh_host}: {exc}")
    return ""


def rescan_iscsi(ssh_host, ssh_user, ssh_pass, ssh_key):
    """Rescans existing iSCSI sessions for new LUNs and updates multipath."""
    try:
        ssh_run(ssh_host, ssh_user, ssh_pass,
                "iscsiadm -m session --rescan 2>/dev/null; "
                "sleep 3; "
                "udevadm settle --timeout=10 2>/dev/null; "
                "multipath 2>/dev/null; "
                "multipathd reconfigure 2>/dev/null; "
                "sleep 2; "
                "true",
                key_material=ssh_key, timeout=60)
        log.info("[netapp_storage] iSCSI rescan completed")
    except Exception as exc:
        log.warning(f"[netapp_storage] iSCSI rescan failed: {exc}")


def _iscsi_serial_to_mapper(serial):
    """Convert ONTAP ASCII LUN serial to /dev/mapper path.

    ONTAP iSCSI LUN WWID format (NAA type-6, IEEE Registered Extended):
      3  600a0980  <hex(serial_12_bytes)>
      ^  ^^^^^^^^  ^^^^^^^^^^^^^^^^^^^^^^^^
      |  NetApp    12-byte ASCII serial as hex (lowercase)
      multipath NAA prefix

    '600a0980' encodes NAA=6 + NetApp OUI (00:a0:98) + 4 vendor bits.
    This is distinct from the wrong '3' + hex(serial) formula.
    """
    try:
        return "/dev/mapper/3600a0980" + serial.encode("latin-1").hex()
    except Exception:
        return ""


def find_device_by_serial(ssh_host, ssh_user, ssh_pass, ssh_key, serial, timeout_s=90):
    """Finds the block device for an ONTAP iSCSI LUN by serial number.

    Tries (in order):
    1. /dev/mapper/<wwid>           — multipath device (multi-path or find_multipaths no)
    2. /dev/disk/by-id/scsi-<wwid>  — raw sdX device via udev (find_multipaths yes / single-path)

    Returns the resolved device path or raises RuntimeError on timeout.
    """
    mapper_dev = _iscsi_serial_to_mapper(serial)
    if not mapper_dev:
        raise RuntimeError(f"Cannot compute mapper path for serial {serial!r}")
    wwid = mapper_dev.replace("/dev/mapper/", "")
    byid_dev = f"/dev/disk/by-id/scsi-{wwid}"

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        # 1. Multipath device
        try:
            out = ssh_run(
                ssh_host, ssh_user, ssh_pass,
                f"test -b {shlex.quote(mapper_dev)} && echo yes || echo no",
                capture=True, key_material=ssh_key, timeout=10,
            )
            if out.strip() == "yes":
                log.info(f"[netapp_storage] multipath device for serial {serial}: {mapper_dev}")
                return mapper_dev
        except Exception:
            pass
        # 2. Raw device via /dev/disk/by-id/ (single-path / find_multipaths yes)
        try:
            out = ssh_run(
                ssh_host, ssh_user, ssh_pass,
                f"test -L {shlex.quote(byid_dev)} && readlink -f {shlex.quote(byid_dev)} || echo no",
                capture=True, key_material=ssh_key, timeout=10,
            )
            result = out.strip()
            if result and result != "no":
                log.info(f"[netapp_storage] raw device for serial {serial}: {result}")
                return result
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError(
        f"Block device with serial {serial} not found after {timeout_s}s "
        f"(tried {mapper_dev} and {byid_dev})"
    )


def flush_iscsi_clone_device(ssh_host, ssh_user, ssh_pass, ssh_key, serial):
    """Remove the iSCSI clone device from the host after ONTAP cleanup.

    Handles both multipath DM devices and single-path raw sdX devices.
    Errors are logged but not raised — cleanup is best-effort.
    """
    if not serial:
        return
    mapper_dev = _iscsi_serial_to_mapper(serial)
    if not mapper_dev:
        return
    wwid = shlex.quote(mapper_dev.replace("/dev/mapper/", ""))
    byid = shlex.quote(f"/dev/disk/by-id/scsi-{mapper_dev.replace('/dev/mapper/', '')}")
    # multipath path: disable queuing, flush DM device, delete underlying sdX
    # single-path fallback: find sdX via by-id symlink and delete via sysfs
    flush_cmd = (
        f"WWID={wwid}; "
        f"BYID={byid}; "
        # Collect sdX paths known to multipathd (multipath case)
        "devs=$(multipathd show paths format '%d %w' 2>/dev/null"
        "       | awk -v w=$WWID '$2==w{print $1}'); "
        "multipathd disablequeueing map $WWID 2>/dev/null; "
        "multipath -f $WWID 2>/dev/null; "
        # Single-path fallback: pick up the sdX via /dev/disk/by-id
        "if [ -z \"$devs\" ] && [ -L $BYID ]; then "
        "  devs=$(basename $(readlink -f $BYID 2>/dev/null)); "
        "fi; "
        # Delete each sdX device through sysfs
        "for d in $devs; do "
        "  hcil=$(readlink /sys/block/$d 2>/dev/null"
        "         | grep -oE '[0-9]+:[0-9]+:[0-9]+:[0-9]+' | tail -1); "
        "  if [ -n \"$hcil\" ] && [ -e /sys/class/scsi_device/$hcil/device/delete ]; then "
        "    echo 1 > /sys/class/scsi_device/$hcil/device/delete 2>/dev/null; "
        "  elif [ -e /sys/block/$d/device/delete ]; then "
        "    echo 1 > /sys/block/$d/device/delete 2>/dev/null; "
        "  fi; "
        "done; "
        "true"
    )
    try:
        ssh_run(ssh_host, ssh_user, ssh_pass, flush_cmd,
                key_material=ssh_key, timeout=30)
        log.info(f"[netapp_storage] flushed iSCSI device {wwid} (serial={serial})")
    except Exception as exc:
        log.warning(f"[netapp_storage] flush iSCSI device (serial={serial}): {exc}")


def connect_iscsi_target(ssh_host, ssh_user, ssh_pass, ssh_key, portal, target_iqn):
    """Discovers and logs into a new iSCSI target on the host, then triggers multipath."""
    cmd = (
        f"iscsiadm -m discovery -t st -p {shlex.quote(portal)} 2>/dev/null; "
        f"iscsiadm -m node -T {shlex.quote(target_iqn)} -p {shlex.quote(portal)} "
        f"--login 2>/dev/null; "
        "sleep 3; "
        "udevadm settle --timeout=10 2>/dev/null; "
        "multipath 2>/dev/null; "
        "multipathd reconfigure 2>/dev/null; "
        "sleep 2; "
        "true"
    )
    ssh_run(ssh_host, ssh_user, ssh_pass, cmd, key_material=ssh_key, timeout=60)
    log.info(f"[netapp_storage] iSCSI connect {target_iqn} via {portal} done")


def disconnect_iscsi_target(ssh_host, ssh_user, ssh_pass, ssh_key, portal, target_iqn):
    """Logs out from an iSCSI target and removes its discovery/node record."""
    cmd = (
        f"iscsiadm -m node -T {shlex.quote(target_iqn)} "
        f"-p {shlex.quote(portal)} --logout 2>/dev/null || true; "
        f"iscsiadm -m node -T {shlex.quote(target_iqn)} "
        f"-p {shlex.quote(portal)} --op delete 2>/dev/null || true; "
        "true"
    )
    try:
        ssh_run(ssh_host, ssh_user, ssh_pass, cmd, key_material=ssh_key, timeout=30)
        log.info(f"[netapp_storage] iSCSI logout {target_iqn} via {portal} done")
    except Exception as exc:
        log.warning(f"[netapp_storage] iSCSI logout {target_iqn}: {exc}")


# ── Restore: VG importieren ───────────────────────────────────────────────────

def vg_import_clone(ssh_host, ssh_user, ssh_pass, ssh_key, device, base_vg_name):
    """Imports a cloned VG with new UUIDs (vgimportclone).

    Prevents UUID collision with the live VG.
    Returns the actual new VG name (may be '{base}1').
    """
    dev_q  = shlex.quote(device)
    base_q = shlex.quote(base_vg_name)

    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"pvscan --cache {dev_q} 2>/dev/null; true",
            key_material=ssh_key)

    # vgimportclone rejects a whole-disk device that carries a partition table
    # (exit 5, "Device is excluded: device is partitioned").  Ubuntu-style VMs
    # put the LVM PV on a partition (e.g. /dev/nvme1n3p2), not the whole disk.
    # Detect this and redirect to the actual lvm2_member partition.
    try:
        out = ssh_run(ssh_host, ssh_user, ssh_pass,
                      f"udevadm settle 2>/dev/null; lsblk -J -o NAME,FSTYPE {dev_q}",
                      capture=True, key_material=ssh_key, timeout=15)
        children = (json.loads(out).get("blockdevices") or [{}])[0].get("children") or []
        for child in children:
            cname = child.get("name", "")
            if not cname:
                continue
            fs = (child.get("fstype") or "").lower()
            if not fs:
                try:
                    fs = ssh_run(ssh_host, ssh_user, ssh_pass,
                                 f"blkid -s TYPE -o value /dev/{cname} 2>/dev/null || true",
                                 capture=True, key_material=ssh_key, timeout=10).strip().lower()
                except Exception:
                    pass
            if fs == "lvm2_member":
                pv_dev = f"/dev/{cname}"
                log.info(f"[netapp_storage] {device} is partitioned; using LVM PV partition {pv_dev}")
                device = pv_dev
                dev_q  = shlex.quote(device)
                ssh_run(ssh_host, ssh_user, ssh_pass,
                        f"pvscan --cache {dev_q} 2>/dev/null; true",
                        key_material=ssh_key)
                break
    except Exception as exc:
        log.warning(f"[netapp_storage] partition scan before vgimportclone {device}: {exc}")

    # Remove any stale VG with the same target name from a previous failed run.
    # vgchange -an ensures dm devices are removed; vgremove -f wipes VG metadata.
    # Without this, dm-create fails with "Device or resource busy" on the same UUID.
    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"vgchange -an {base_q}1 2>/dev/null; vgremove -f {base_q}1 2>/dev/null; true",
            key_material=ssh_key)

    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"vgimportclone --basevgname {base_q} {dev_q}",
            key_material=ssh_key)

    # Determine actual VG name
    out = ssh_run(ssh_host, ssh_user, ssh_pass,
                  f"pvs --noheadings -o vg_name {dev_q} 2>/dev/null",
                  capture=True, key_material=ssh_key)
    actual = out.strip()
    if not actual:
        actual = base_vg_name

    # pvscan --cache and vgimportclone both trigger udev auto-activation of the cloned VG.
    # udev may activate LVs with pre-import or post-import UUIDs, leaving dm entries whose
    # NAME conflicts when activate_lv_for_restore tries to create them with the new UUID.
    # Fix: wait for udev to finish, deactivate the entire VG, then remove any remaining
    # stale dm entries by name prefix before returning.
    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"udevadm settle --timeout=5 2>/dev/null; true",
            key_material=ssh_key)
    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"vgchange -an {shlex.quote(actual)} 2>/dev/null; "
            f"udevadm settle --timeout=3 2>/dev/null; true",
            key_material=ssh_key)
    # Force-remove any dm entries whose name starts with the VG prefix that vgchange may
    # have missed (e.g. entries orphaned by a UUID change mid-activation).
    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"dmsetup ls 2>/dev/null | awk '{{print $1}}' | "
            f"grep '^{actual}-' | "
            f"xargs -r -I{{}} dmsetup remove --force {{}} 2>/dev/null; true",
            key_material=ssh_key)

    # If the production VG spanned multiple PVs (multiple NVMe namespaces / LUNs),
    # the cloned VG metadata still references those other PVs by UUID — but only
    # one namespace was cloned.  Remove the missing-PV references from the clone VG
    # so that lvchange -ay can activate the LVs that reside on the available PV.
    # This is safe: we operate on the renamed clone (new UUIDs), not the live VG.
    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"vgreduce --removemissing --force {shlex.quote(actual)} 2>/dev/null; true",
            key_material=ssh_key)

    log.info(f"[netapp_storage] VG clone imported as '{actual}' from {device}")
    return actual


def clear_stale_dm_entries(ssh_host, ssh_user, ssh_pass, ssh_key, vg_name):
    """Deactivates vg_name and force-removes any leftover device-mapper
    entries under its name prefix — the same defensive cleanup
    vg_import_clone() already does once right after import (see its comment
    on udev auto-activation racing the UUID change), but callable again
    later.

    Needed for Instant Recovery specifically: unlike Clone/Restore-single,
    which activate+dd-copy the LV themselves right after import, Instant
    Recovery does nothing between import and registering the clone VG as a
    PVE storage (pvesm add) — and `qm rescan`/`qm start` afterwards can
    trigger udev to re-activate the VG under a stale mapping again, which
    then makes PVE's own disk-attach activation fail with "device-mapper:
    create ioctl ... failed: Device or resource busy" (observed live against
    ucnlabasa01 — the VM's config and disk were both fine, only the LV
    activation at boot time failed).
    """
    vg_q = shlex.quote(vg_name)
    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"vgchange -an {vg_q} 2>/dev/null; udevadm settle --timeout=3 2>/dev/null; true",
            key_material=ssh_key)
    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"dmsetup ls 2>/dev/null | awk '{{print $1}}' | "
            f"grep '^{vg_name}-' | "
            f"xargs -r -I{{}} dmsetup remove --force {{}} 2>/dev/null; true",
            key_material=ssh_key)


def activate_lv_for_restore(ssh_host, ssh_user, ssh_pass, ssh_key,
                              vg_name, lv_name, lvm_type, pool_name=""):
    """Activates an LV (and thin pool if needed) for restore access.

    For lvm_type='thin': activate pool first, then the thin LV.
    """
    vg_q = shlex.quote(vg_name)
    lv_q = shlex.quote(lv_name)

    if lvm_type == "thin" and pool_name:
        pool_q = shlex.quote(pool_name)
        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"lvchange -ay {vg_q}/{pool_q}",
                key_material=ssh_key)

    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"lvchange -ay {vg_q}/{lv_q}",
            key_material=ssh_key)


def lv_copy(ssh_host, ssh_user, ssh_pass, ssh_key,
            src_vg, src_lv, dst_vg, dst_lv, jlog=None):
    """Copies an LV block-by-block via dd.

    Uses 512 MiB blocks with O_DIRECT on both sides to saturate NVMe throughput
    and avoid polluting the page cache.  Timeout: 4 hours.
    Returns True on success, raises RuntimeError on failure.
    """
    src_q = shlex.quote(f"/dev/{src_vg}/{src_lv}")
    dst_q = shlex.quote(f"/dev/{dst_vg}/{dst_lv}")
    tmp   = f"/tmp/.dd_{_uuid.uuid4().hex[:8]}"
    tmp_q = shlex.quote(tmp)

    size_bytes = get_lv_size_bytes(ssh_host, ssh_user, ssh_pass, ssh_key, src_vg, src_lv)
    size_str = f" ({size_bytes / 1073741824:.1f} GiB)" if size_bytes else ""

    msg = f"LV copy: {src_vg}/{src_lv} → {dst_vg}/{dst_lv}{size_str}"
    log.info(f"[netapp_storage] {msg}")
    if jlog:
        jlog.log(msg)

    # dd stderr (progress lines \r-terminated, final stats \n-terminated) → temp file.
    # After copy: convert \r to \n, drop empty lines, take the last line = final stats.
    out = ssh_run(
        ssh_host, ssh_user, ssh_pass,
        f"dd if={src_q} of={dst_q} bs=512M iflag=direct oflag=direct conv=fsync "
        f"status=progress 2>{tmp_q}; "
        f"tr '\\r' '\\n' <{tmp_q} 2>/dev/null | grep -v '^$' | tail -1; "
        f"rm -f {tmp_q}",
        capture=True, key_material=ssh_key, timeout=14400,
    )
    stats = out.strip()

    done = f"LV copy completed: {src_vg}/{src_lv}"
    if stats:
        done += f" — {stats}"
    log.info(f"[netapp_storage] {done}")
    if jlog:
        jlog.log(done)
    return True


def cleanup_restore_vg(ssh_host, ssh_user, ssh_pass, ssh_key, vg_name):
    """Deactivates and removes a temporary restore VG.

    The physical device (clone LUN) is separately unmapped
    and deleted via ONTAP API afterwards.
    vgremove -f is needed to release dm UUIDs — without it
    a subsequent vgimportclone fails with "Device or resource busy".

    Also force-clears any leftover dm-mapper entry under this VG's name
    prefix afterwards (see clear_stale_dm_entries) — vgremove alone doesn't
    reliably clear a dm entry left "Open count: 1" by an interrupted
    activation attempt (observed live: a VM whose Instant Recovery boot
    failed left the LV's dm device behind even after vgremove -f, which
    then made the *next* clone using the same VG/LV name fail with
    "device-mapper: create ioctl ... Device or resource busy" at qm start).
    """
    vg_q = shlex.quote(vg_name)
    try:
        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"vgchange -an {vg_q} 2>/dev/null; vgremove -f {vg_q} 2>/dev/null; true",
                key_material=ssh_key)
        log.info(f"[netapp_storage] restore VG '{vg_name}' removed")
    except Exception as exc:
        log.warning(f"[netapp_storage] VG cleanup {vg_name} failed: {exc}")
    try:
        clear_stale_dm_entries(ssh_host, ssh_user, ssh_pass, ssh_key, vg_name)
    except Exception as exc:
        log.warning(f"[netapp_storage] stale dm cleanup for {vg_name} failed: {exc}")


# ── SAN-Restore: VG deaktivieren / reaktivieren ───────────────────────────────

def vg_deactivate(ssh_host, ssh_user, ssh_pass, ssh_key, vg_name):
    """Deactivates all LVs of a VG (before volume revert on ONTAP).

    Raises RuntimeError if deactivation fails.
    """
    vg_q = shlex.quote(vg_name)
    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"vgchange -an {vg_q}",
            key_material=ssh_key)
    log.info(f"[netapp_storage] VG '{vg_name}' deactivated")


# ── LV management ─────────────────────────────────────────────────────────────

def get_lv_size_bytes(ssh_host, ssh_user, ssh_pass, ssh_key, vg_name, lv_name):
    """Returns LV size in bytes (0 on error or LV not found)."""
    vg_q = shlex.quote(vg_name)
    lv_q = shlex.quote(lv_name)
    try:
        out = ssh_run(
            ssh_host, ssh_user, ssh_pass,
            f"lvs --noheadings --units b --nosuffix -o lv_size {vg_q}/{lv_q} 2>/dev/null",
            capture=True, key_material=ssh_key,
        )
        return int(out.strip())
    except Exception:
        return 0


def create_lv(ssh_host, ssh_user, ssh_pass, ssh_key,
              vg_name, lv_name, size_bytes, lvm_type, pool_name=""):
    """Creates a new LV in a VG — thin-provisioned if lvm_type='thin', thick otherwise."""
    vg_q = shlex.quote(vg_name)
    lv_q = shlex.quote(lv_name)
    if lvm_type == "thin" and pool_name:
        pool_q = shlex.quote(pool_name)
        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"lvcreate -V {size_bytes}B -T {vg_q}/{pool_q} -n {lv_q}"
                f" --zero n --wipesignatures n",
                key_material=ssh_key)
    else:
        ssh_run(ssh_host, ssh_user, ssh_pass,
                f"lvcreate -L {size_bytes}B -n {lv_q} {vg_q}"
                f" --zero n --wipesignatures n",
                key_material=ssh_key)
    log.info(f"[netapp_storage] LV created: {vg_name}/{lv_name} ({size_bytes} B, {lvm_type})")


# ── NVMe-oF: Rescan + Device-Discovery ───────────────────────────────────────

def nvme_list_devices(ssh_host, ssh_user, ssh_pass, ssh_key):
    """Returns the current set of NVMe namespace block devices (/dev/nvme*n*)."""
    try:
        out = ssh_run(ssh_host, ssh_user, ssh_pass,
                      "ls /dev/nvme*n* 2>/dev/null || true",
                      capture=True, key_material=ssh_key)
        return {line.strip() for line in out.splitlines()
                if line.strip().startswith("/dev/nvme") and "n" in line.strip().split("/")[-1]}
    except Exception:
        return set()


def nvme_ns_rescan(ssh_host, ssh_user, ssh_pass, ssh_key):
    """Triggers NVMe namespace rescan on all controllers."""
    try:
        out = ssh_run(
            ssh_host, ssh_user, ssh_pass,
            "ls /dev/nvme[0-9]* 2>/dev/null | grep -E '^/dev/nvme[0-9]+$' || true",
            capture=True, key_material=ssh_key,
        )
        for ctrl in (c.strip() for c in out.splitlines() if c.strip()):
            try:
                ssh_run(ssh_host, ssh_user, ssh_pass,
                        f"nvme ns-rescan {shlex.quote(ctrl)} 2>/dev/null; true",
                        key_material=ssh_key, timeout=15)
            except Exception:
                pass
        log.info("[netapp_storage] NVMe namespace rescan completed")
    except Exception as exc:
        log.warning(f"[netapp_storage] NVMe rescan failed: {exc}")


def find_new_nvme_device(ssh_host, ssh_user, ssh_pass, ssh_key,
                         devices_before, timeout_s=30):
    """Finds a newly-appeared NVMe namespace device after subsystem mapping.

    devices_before: set of /dev/nvme*n* paths known before the mapping.
    Returns the first new device path, or raises RuntimeError on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        current = nvme_list_devices(ssh_host, ssh_user, ssh_pass, ssh_key)
        new_devs = current - devices_before
        if new_devs:
            dev = sorted(new_devs)[0]
            log.info(f"[netapp_storage] New NVMe namespace device: {dev}")
            return dev
        time.sleep(2)
    raise RuntimeError(f"New NVMe namespace device not found after {timeout_s}s")


def find_nvme_device_for_subsystem_nqn(ssh_host, ssh_user, ssh_pass, ssh_key,
                                        subsystem_nqn, timeout_s=60,
                                        devices_before=None):
    """Finds the /dev/nvme*n* device for a specific subsystem NQN.

    Search strategies (in order):
    1. JSON:  nvme list-subsys -o json — unambiguous direct namespace path
    2. Text:  nvme list-subsys plain text — parse controller names → ls /dev/nvmeXn*
    3. Sysfs: /sys/class/nvme/*/subsysnqn — catches controllers not listed in
              nvme list-subsys (e.g. stale controllers from a previous bind attempt
              that were not fully disconnected but still hold a namespace device)
    4. After timeout: baseline-diff — any device not in `devices_before` (if provided)

    Retries with nvme ns-rescan between iterations until timeout_s.
    """
    import re as _re
    import json as _json

    def _try_json(out):
        """Parse nvme list-subsys -o json → first namespace device path."""
        try:
            data = _json.loads(out)
            # Output is either a list of subsystems or {"Subsystems": [...]}
            subsystems = data if isinstance(data, list) else data.get("Subsystems", [])
            for subsys in subsystems:
                nqn = subsys.get("NQN") or subsys.get("nqn", "")
                if subsystem_nqn not in nqn:
                    continue
                for ctrl in (subsys.get("Controllers") or subsys.get("controllers") or []):
                    for ns in (ctrl.get("Namespaces") or ctrl.get("namespaces") or []):
                        ns_name = (ns.get("Name") or ns.get("name")
                                   or ns.get("NameSpace") or "").strip()
                        if ns_name:
                            return ns_name if ns_name.startswith("/dev/") else f"/dev/{ns_name}"
                    ctrl_name = (ctrl.get("Name") or ctrl.get("name", "")).strip()
                    if ctrl_name:
                        return f"_ctrl:{ctrl_name}"  # signal: need ls
        except Exception:
            pass
        return None

    def _try_text(out):
        """Parse nvme list-subsys plain text → controller names."""
        controllers = []
        in_subsys = False
        for line in out.splitlines():
            if subsystem_nqn in line:
                in_subsys = True
                continue
            if not in_subsys:
                continue
            if line.strip().startswith("nvme-subsys"):
                break
            m = _re.search(r'[-+\\|]+\s*(nvme\d+)', line)
            if m:
                controllers.append(m.group(1))
        return controllers

    def _try_sysfs():
        """Find namespace device via /sys/class/nvme/*/subsysnqn.

        Catches controllers that are active (have namespace devices) but are
        not listed in 'nvme list-subsys' for our NQN — e.g. controllers from
        a previous bind attempt that survived an nvme disconnect and still
        hold a namespace device under the same subsystem NQN.
        """
        try:
            nqn_q = shlex.quote(subsystem_nqn)
            script = (
                "for f in /sys/class/nvme/nvme*/subsysnqn; do "
                "  [ -f \"$f\" ] || continue; "
                "  nqn=$(cat \"$f\" 2>/dev/null); "
                f"  [ \"$nqn\" = {nqn_q} ] || continue; "
                "  ctrl=$(basename $(dirname \"$f\")); "
                "  for ns in /dev/${ctrl}n[0-9]*; do "
                "    [ -b \"$ns\" ] || continue; "
                "    case \"$ns\" in *p*) continue ;; esac; "
                "    echo \"$ns\"; break 2; "
                "  done; "
                "done 2>/dev/null | head -1"
            )
            out = ssh_run(ssh_host, ssh_user, ssh_pass, script,
                          capture=True, key_material=ssh_key, timeout=15)
            dev = out.strip()
            if dev and dev.startswith("/dev/"):
                return dev
        except Exception:
            pass
        return None

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            # ── JSON path ────────────────────────────────────────────────────
            out_json = ssh_run(ssh_host, ssh_user, ssh_pass,
                               "nvme list-subsys -o json 2>/dev/null",
                               capture=True, key_material=ssh_key, timeout=15)
            result = _try_json(out_json)
            if result and not result.startswith("_ctrl:"):
                log.info(f"[netapp_storage] NVMe device (JSON): {result}")
                return result
            if result and result.startswith("_ctrl:"):
                ctrl_name = result[6:]
                dev_out = ssh_run(ssh_host, ssh_user, ssh_pass,
                                  f"ls /dev/{ctrl_name}n* 2>/dev/null | grep -v p | head -1",
                                  capture=True, key_material=ssh_key, timeout=10)
                dev = dev_out.strip()
                if dev and dev.startswith("/dev/"):
                    log.info(f"[netapp_storage] NVMe device (JSON+ls): {dev}")
                    return dev

            # ── Text fallback ─────────────────────────────────────────────────
            out_txt = ssh_run(ssh_host, ssh_user, ssh_pass,
                              "nvme list-subsys 2>/dev/null",
                              capture=True, key_material=ssh_key, timeout=15)
            for ctrl in _try_text(out_txt):
                dev_out = ssh_run(ssh_host, ssh_user, ssh_pass,
                                  f"ls /dev/{ctrl}n* 2>/dev/null | grep -v p | head -1",
                                  capture=True, key_material=ssh_key, timeout=10)
                dev = dev_out.strip()
                if dev and dev.startswith("/dev/"):
                    log.info(f"[netapp_storage] NVMe device (text): {dev}")
                    return dev

            # ── Sysfs fallback ────────────────────────────────────────────────
            dev = _try_sysfs()
            if dev:
                log.info(f"[netapp_storage] NVMe device (sysfs): {dev}")
                return dev

            # ── Baseline-diff fallback (in-loop) ─────────────────────────────
            # Device may have appeared on a controller with a different NQN
            # (e.g. the production controller picked up the recovery namespace).
            # Only active when devices_before was provided by the caller.
            if devices_before is not None:
                current = nvme_list_devices(ssh_host, ssh_user, ssh_pass, ssh_key)
                new_devs = current - devices_before
                if new_devs:
                    dev = sorted(new_devs)[0]
                    log.info(f"[netapp_storage] NVMe device (baseline diff, in-loop): {dev}")
                    return dev

        except Exception:
            pass
        nvme_ns_rescan(ssh_host, ssh_user, ssh_pass, ssh_key)
        time.sleep(3)

    # ── Post-timeout: final sysfs check ───────────────────────────────────────
    dev = _try_sysfs()
    if dev:
        log.info(f"[netapp_storage] NVMe device (sysfs, post-timeout): {dev}")
        return dev

    # ── Post-timeout: baseline-diff fallback ──────────────────────────────────
    if devices_before is not None:
        current = nvme_list_devices(ssh_host, ssh_user, ssh_pass, ssh_key)
        new_devs = current - devices_before
        if new_devs:
            dev = sorted(new_devs)[0]
            log.info(f"[netapp_storage] NVMe device (baseline diff): {dev}")
            return dev

    raise RuntimeError(f"NVMe namespace device not found after {timeout_s}s")


def get_nvme_host_nqn(ssh_host, ssh_user, ssh_pass, ssh_key):
    """Returns the host NQN from /etc/nvme/hostnqn, or empty string."""
    try:
        out = ssh_run(ssh_host, ssh_user, ssh_pass,
                      "cat /etc/nvme/hostnqn 2>/dev/null || true",
                      capture=True, key_material=ssh_key, timeout=10)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("nqn."):
                return line
    except Exception as exc:
        log.warning(f"[netapp_storage] get_nvme_host_nqn {ssh_host}: {exc}")
    return ""


def nvme_connect_all(ssh_host, ssh_user, ssh_pass, ssh_key, timeout_s=60):
    """Runs nvme connect-all (reads discovery.conf) and triggers a namespace rescan.

    Uses Linux timeout(1) to cap the nvme connect-all call: in environments
    where discovery.conf contains LIFs without a DDC (port 8009), nvme discover
    hangs per entry. The actual data connections complete quickly; the Linux
    timeout kills the stalled discovery without failing the overall operation.
    The SSH timeout is set slightly above the inner timeout to give it room.
    """
    inner = max(20, timeout_s - 10)
    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"timeout {inner} nvme connect-all 2>/dev/null; sleep 2; true",
            key_material=ssh_key, timeout=timeout_s)
    nvme_ns_rescan(ssh_host, ssh_user, ssh_pass, ssh_key)


def nvme_connect_to_subsystem(ssh_host, ssh_user, ssh_pass, ssh_key,
                               lif_ips, subsystem_nqn, timeout_s=15):
    """Connects to a specific NVMe subsystem on each LIF via explicit nvme connect.

    Unlike nvme connect-all (which reads discovery.conf and requires a DDC on
    port 8009), this issues one nvme connect per LIF directly to the data port
    (4420). Synchronous, DDC-free, and deterministic.
    """
    for lif_ip in lif_ips:
        try:
            cmd = (f"nvme connect -t tcp -a {shlex.quote(lif_ip)} -s 4420 "
                   f"-n {shlex.quote(subsystem_nqn)} 2>/dev/null; true")
            ssh_run(ssh_host, ssh_user, ssh_pass, cmd,
                    key_material=ssh_key, timeout=timeout_s)
        except Exception as exc:
            log.warning(f"[netapp_storage] nvme connect {lif_ip} → {subsystem_nqn[:40]}…: {exc}")
    nvme_ns_rescan(ssh_host, ssh_user, ssh_pass, ssh_key)


def ensure_nvme_discovery_entries(ssh_host, ssh_user, ssh_pass, ssh_key, lif_ips):
    """Ensure /etc/nvme/discovery.conf has an entry for every LIF IP.

    Matches each LIF to an existing host-traddr/host-iface pair by /16 subnet
    (first two octets). Idempotent — only appends missing lines.
    Returns the number of entries added.
    """
    CONF = "/etc/nvme/discovery.conf"
    try:
        existing_raw = ssh_run(ssh_host, ssh_user, ssh_pass,
                               f"cat {CONF} 2>/dev/null || true",
                               capture=True, key_material=ssh_key)
    except Exception:
        existing_raw = ""

    lines = [l.strip() for l in existing_raw.splitlines() if l.strip()]

    # Parse existing entries
    existing_traddrs = set()
    iface_map = {}  # first-two-octets -> (iface, host_traddr)
    for line in lines:
        parts = {}
        for token in line.split():
            if "=" in token:
                k, v = token.split("=", 1)
                parts[k.lstrip("-")] = v
        traddr = parts.get("traddr", "")
        if traddr:
            existing_traddrs.add(traddr)
        host_traddr = parts.get("host-traddr", "")
        iface = parts.get("host-iface", "")
        if host_traddr and iface:
            prefix = ".".join(host_traddr.split(".")[:2])
            iface_map.setdefault(prefix, (iface, host_traddr))

    new_entries = []
    for lif_ip in lif_ips:
        if lif_ip in existing_traddrs:
            continue
        prefix = ".".join(lif_ip.split(".")[:2])
        if prefix not in iface_map:
            log.warning(f"[netapp_storage] ensure_nvme_discovery: no matching host interface "
                        f"for LIF {lif_ip} on {ssh_host} (known prefixes: {list(iface_map)})")
            continue
        iface, host_traddr = iface_map[prefix]
        entry = (f"--transport=tcp --traddr={lif_ip} "
                 f"--host-iface={iface} --host-traddr={host_traddr}")
        new_entries.append(entry)

    if not new_entries:
        return 0

    append_cmd = " && ".join(
        f"echo {shlex.quote(e)} >> {CONF}" for e in new_entries
    )
    try:
        ssh_run(ssh_host, ssh_user, ssh_pass, append_cmd, key_material=ssh_key)
        log.info(f"[netapp_storage] Added {len(new_entries)} entry/entries to "
                 f"{CONF} on {ssh_host}: {[e.split('--traddr=')[1].split()[0] for e in new_entries]}")
    except Exception as exc:
        log.warning(f"[netapp_storage] ensure_nvme_discovery: write failed on {ssh_host}: {exc}")
        return 0
    return len(new_entries)


def nvme_disconnect_by_vg(ssh_host, ssh_user, ssh_pass, ssh_key, vg_name):
    """Disconnects the NVMe controller that backs the given VG.

    Finds the PV device (e.g. /dev/nvme0n1), derives the controller (/dev/nvme0),
    and runs nvme disconnect on it.
    """
    try:
        out = ssh_run(ssh_host, ssh_user, ssh_pass,
                      f"pvs --noheadings -o pv_name --select 'vgname={vg_name}' 2>/dev/null || true",
                      capture=True, key_material=ssh_key, timeout=15)
        for line in out.splitlines():
            pv = line.strip()
            if not pv:
                continue
            # /dev/nvme0n1 or /dev/nvme0n1p1 → /dev/nvme0
            import re as _re
            m = _re.match(r'(/dev/nvme\d+)', pv)
            if m:
                ctrl = m.group(1)
                ssh_run(ssh_host, ssh_user, ssh_pass,
                        f"timeout 10 nvme disconnect --device {shlex.quote(ctrl)} 2>/dev/null; true",
                        key_material=ssh_key, timeout=20)
                log.info(f"[netapp_storage] nvme disconnect {ctrl} on {ssh_host}")
                return
    except Exception as exc:
        log.warning(f"[netapp_storage] nvme_disconnect_by_vg {ssh_host}: {exc}")


def nvme_disconnect_by_subsystem_name(ssh_host, ssh_user, ssh_pass, ssh_key, subsystem_name):
    """Fallback disconnect: finds the NQN for subsystem_name in nvme list-subsys and disconnects it.

    Used when nvme_disconnect_by_vg finds no VG (VG never created or already removed).
    """
    if not subsystem_name:
        return
    try:
        import re as _re
        out = ssh_run(ssh_host, ssh_user, ssh_pass,
                      "nvme list-subsys 2>/dev/null",
                      capture=True, key_material=ssh_key, timeout=15)
        for line in out.splitlines():
            m = _re.search(r'NQN=([\S]+)', line)
            if m:
                nqn = m.group(1)
                if f":subsystem.{subsystem_name}" in nqn:
                    ssh_run(ssh_host, ssh_user, ssh_pass,
                            f"timeout 10 nvme disconnect -n {shlex.quote(nqn)} 2>/dev/null; true",
                            key_material=ssh_key, timeout=20)
                    log.info(f"[netapp_storage] nvme disconnect subsystem '{subsystem_name}' on {ssh_host}")
                    return
    except Exception as exc:
        log.warning(f"[netapp_storage] nvme_disconnect_by_subsystem_name {ssh_host}: {exc}")


# ── NVMe-oF: temp-subsystem clone lifecycle (Clone / Single-VM Restore / SFR) ─

def nvme_clone_and_map_temp_subsystem(client, main_ns_uuid, snap_name, vol_name,
                                       clone_name, svm_name,
                                       ssh_host, ssh_user, ssh_pass, ssh_key,
                                       job_id, jlog=None, out=None):
    """Clones an NVMe namespace from a snapshot and maps it to a brand-new,
    host-scoped NVMe subsystem — mirrors the isolation the iSCSI clone path
    already gets via a temporary igroup, so other hosts sharing the
    production subsystem never see the clone.

    On ASA, clone_namespace()'s CLI-bridge fallback needs to map the clone
    into the SOURCE's subsystem just to be able to discover its UUID at all
    (see ontap_client._clone_namespace_via_cli_volume_clone) — this function
    undoes that mapping immediately after discovery, before mapping the
    clone into the new temp subsystem instead.

    out: an existing dict to fill in place, instead of a fresh one — pass
    the SAME dict the caller will later hand to nvme_clone_cleanup(), so
    that a partial failure here (e.g. clone created but device discovery
    times out) still leaves the caller's dict populated with whatever did
    get created. A fresh local dict discarded on exception would otherwise
    orphan those objects — the caller never even learns their identifiers
    to clean them up.

    Returns the dict (same object as `out`, if given) — pass it to
    nvme_clone_cleanup() for teardown:
      device:          block device path on ssh_host once found
      ns_uuid:         clone namespace uuid
      clone_vol_uuid, clone_vol_name, svm_name: '' unless the clone is backed
                       by a whole FlexClone volume (ASA fallback) — see
                       clone_namespace(). clone_vol_name/svm_name must be
                       used for the delete, not a fresh lookup by uuid alone
                       — that was observed live to sometimes return no name
                       for this kind of volume, silently skipping the
                       CLI-bridge fallback delete needed on ASA.
      subsystem_uuid, subsystem_name: the new temp subsystem

    Raises RuntimeError/OntapError on failure.
    """
    def _log(msg):
        log.info(f"[netapp_storage] {msg}")
        if jlog:
            jlog.log(msg)

    info = out if out is not None else {}
    info.update({"device": "", "ns_uuid": "", "clone_vol_uuid": "", "clone_vol_name": "",
                "svm_name": svm_name, "subsystem_uuid": "", "subsystem_name": ""})

    devices_before = nvme_list_devices(ssh_host, ssh_user, ssh_pass, ssh_key)

    _log(f"Cloning NVMe namespace from snapshot '{snap_name}' …")
    ns_uuid, clone_vol_uuid, clone_vol_name, ns_job = client.clone_namespace(
        main_ns_uuid, snap_name, vol_name, clone_name, svm_name)
    if ns_job:
        client.poll_job(ns_job, interval_s=3, timeout_s=300)
    if not ns_uuid:
        raise RuntimeError("clone_namespace returned no UUID — check ONTAP logs")
    info["ns_uuid"], info["clone_vol_uuid"], info["clone_vol_name"] = ns_uuid, clone_vol_uuid, clone_vol_name

    existing = client.get_nvme_subsystem_for_namespace(ns_uuid, svm_name=svm_name)
    if existing and existing.get("uuid"):
        _log(f"Clone namespace was auto-mapped to '{existing.get('name')}' — unmapping for isolation …")
        client.remove_nvme_namespace_from_subsystem(existing["uuid"], ns_uuid)

    host_nqn = get_nvme_host_nqn(ssh_host, ssh_user, ssh_pass, ssh_key)
    if not host_nqn:
        raise RuntimeError(f"Cannot determine NVMe host NQN of {ssh_host}")

    subsystem_name = f"nsclone-{job_id[:8]}"
    _log(f"Creating temporary NVMe subsystem '{subsystem_name}' for {ssh_host} only …")
    subsystem_uuid = client.create_nvme_subsystem(svm_name, subsystem_name)
    info["subsystem_uuid"], info["subsystem_name"] = subsystem_uuid, subsystem_name
    client.add_nvme_host_to_subsystem(subsystem_uuid, host_nqn)
    client.add_nvme_namespace_to_subsystem(subsystem_uuid, ns_uuid, svm_name=svm_name)

    sub_info      = client.get_nvme_subsystem(subsystem_uuid)
    subsystem_nqn = sub_info.get("target_nqn", "")
    lif_ips       = [ip for ip in client.get_nvme_lifs_for_svm(svm_name) if ip]
    if subsystem_nqn and lif_ips:
        _log(f"Connecting {ssh_host} to clone subsystem via {lif_ips} …")
        nvme_connect_to_subsystem(ssh_host, ssh_user, ssh_pass, ssh_key, lif_ips, subsystem_nqn)
    else:
        _log("WARNING: subsystem NQN/LIF unavailable — waiting for auto-discovery")

    _log("Waiting for clone namespace device …")
    info["device"] = find_new_nvme_device(ssh_host, ssh_user, ssh_pass, ssh_key,
                                          devices_before, timeout_s=60)
    return info


def nvme_clone_cleanup(client, clone_info, ssh_host, ssh_user, ssh_pass, ssh_key, jlog=None):
    """Best-effort teardown of everything nvme_clone_and_map_temp_subsystem
    created — including a partially-filled clone_info from a job that failed
    partway through, so a clone that got as far as being mapped still gets
    disconnected/unmapped/deleted instead of silently orphaned.

    Every step is independent (its own try/except) so one failure never
    skips the rest — unlike delegating the whole sequence to a single
    delete_namespace() call, which used to abort the entire cleanup (and
    leave the clone volume behind) the moment any one ONTAP call failed.
    Never raises.
    """
    def _log(msg):
        log.info(f"[netapp_storage] {msg}")
        if jlog:
            jlog.log(msg)

    ns_uuid        = clone_info.get("ns_uuid", "")
    clone_vol_uuid = clone_info.get("clone_vol_uuid", "")
    clone_vol_name = clone_info.get("clone_vol_name", "")
    clone_svm_name = clone_info.get("svm_name", "")
    subsystem_uuid = clone_info.get("subsystem_uuid", "")
    subsystem_name = clone_info.get("subsystem_name", "")

    if subsystem_name and ssh_host:
        try:
            nvme_disconnect_by_subsystem_name(ssh_host, ssh_user, ssh_pass, ssh_key, subsystem_name)
        except Exception as exc:
            log.warning(f"[netapp_storage] nvme clone cleanup: host disconnect failed: {exc}")

    if ns_uuid and subsystem_uuid:
        try:
            client.remove_nvme_namespace_from_subsystem(subsystem_uuid, ns_uuid)
        except Exception as exc:
            log.warning(f"[netapp_storage] nvme clone cleanup: unmap failed: {exc}")

    if clone_vol_uuid:
        try:
            client.unmount_volume(clone_vol_uuid)
        except Exception:
            pass
        try:
            # Deletes via the name/svm captured at clone time, not a fresh
            # lookup by uuid alone (delete_volume()'s own fallback does that,
            # and it was observed live to sometimes come back empty for this
            # kind of volume — see nvme_clone_and_map_temp_subsystem).
            client._delete_clone_volume(clone_vol_uuid, clone_vol_name, clone_svm_name)
            _log("Clone volume removed.")
        except Exception as exc:
            log.warning(f"[netapp_storage] nvme clone cleanup: delete clone volume failed: {exc}")
    elif ns_uuid:
        try:
            client.delete_namespace(ns_uuid)
            _log("Clone namespace removed.")
        except Exception as exc:
            log.warning(f"[netapp_storage] nvme clone cleanup: delete namespace failed: {exc}")

    if subsystem_uuid:
        try:
            client.delete_nvme_subsystem(subsystem_uuid)
            _log("Temp NVMe subsystem removed.")
        except Exception as exc:
            log.warning(f"[netapp_storage] nvme clone cleanup: delete subsystem failed: {exc}")


def vg_rescan_and_activate(ssh_host, ssh_user, ssh_pass, ssh_key, vg_name):
    """Rescans PVs and activates the VG (after volume revert on ONTAP).

    pvscan --cache refreshes the LVM cache so the reverted
    device is read with snapshot metadata.
    """
    vg_q = shlex.quote(vg_name)
    ssh_run(ssh_host, ssh_user, ssh_pass,
            f"pvscan --cache 2>/dev/null; vgchange -ay {vg_q}",
            key_material=ssh_key)
    log.info(f"[netapp_storage] VG '{vg_name}' reactivated")
