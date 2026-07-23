"""
Settings API — SMTP / email notification configuration + DB export/import + plugin updater.

  settings/smtp           GET   – load SMTP config (password omitted)
  settings/smtp/save      POST  – save SMTP config
  settings/smtp/test      POST  – test SMTP connection with stored config
  settings/export         GET   – download all netapp_* tables as JSON
  settings/import         POST  – restore from exported JSON (idempotent upsert)
  settings/update/info    GET   – check GitHub for latest release / branch commits
  settings/update/apply   POST  – download and apply a plugin update from GitHub

  Periodic digest report config/routes live in reporting.py; send_digest_report()
  here builds/sends the actual email (reuses _send_smtp() and the HTML conventions
  used by send_job_notification/send_schedule_consolidated_notification below).
  All three "live" email builders (_build_notification_email, send_schedule_
  consolidated_notification, send_digest_report) plus the "Send test email"
  button share their HTML building blocks via email_common.py.
"""

import os
import smtplib
import ssl
import email.mime.text
import email.mime.multipart
import json
import logging
import threading
import time
from datetime import datetime, timezone

# Plugin directory (two levels up from this file: api/ → netapp_storage/)
_PLUGIN_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GITHUB_REPO = "custosonlinux/netapp_storage"
_GITHUB_API  = "https://api.github.com"

from flask import request, jsonify, Response
from nasnap_core.core.db import get_db
from nasnap_core.api.plugins import register_plugin_route

log = logging.getLogger(__name__)
from ..core._helpers import PLUGIN_ID  # noqa: F401


def _require_admin():
    if request.session.get("role") != "admin":
        return {"error": "Admin access required"}, 403
    return None


def _ensure_smtp_row(db):
    existing = db.query_one("SELECT id FROM netapp_smtp_config WHERE id='default'")
    if not existing:
        db.execute(
            "INSERT INTO netapp_smtp_config (id, updated_at) VALUES ('default', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )


def _smtp_get():
    db = get_db()
    _ensure_smtp_row(db)
    row = db.query_one("SELECT * FROM netapp_smtp_config WHERE id='default'")
    d = dict(row)
    return jsonify({
        'host':         d.get('host', ''),
        'port':         d.get('port', 587),
        'username':     d.get('username', ''),
        'from_address': d.get('from_address', ''),
        'encryption':   d.get('encryption', 'starttls'),
        'enabled':      bool(d.get('enabled', 0)),
        'has_password': bool(d.get('password_encrypted', '')),
    })


def _smtp_save():
    err = _require_admin()
    if err:
        return err
    db = get_db()
    _ensure_smtp_row(db)
    data = request.get_json() or {}
    now = datetime.now(timezone.utc).isoformat()

    host         = data.get('host', '').strip()
    port         = int(data.get('port') or 587)
    username     = data.get('username', '').strip()
    from_address = data.get('from_address', '').strip()
    encryption   = data.get('encryption', 'starttls')
    enabled      = 1 if data.get('enabled') else 0

    if encryption not in ('starttls', 'ssl', 'none'):
        return jsonify({'error': 'Invalid encryption value'}), 400

    if data.get('password'):
        pw_enc = db._encrypt(data['password'])
        db.execute(
            "UPDATE netapp_smtp_config "
            "SET host=?,port=?,username=?,password_encrypted=?,"
            "from_address=?,encryption=?,enabled=?,updated_at=? WHERE id='default'",
            (host, port, username, pw_enc, from_address, encryption, enabled, now),
        )
    else:
        db.execute(
            "UPDATE netapp_smtp_config "
            "SET host=?,port=?,username=?,from_address=?,encryption=?,enabled=?,updated_at=? "
            "WHERE id='default'",
            (host, port, username, from_address, encryption, enabled, now),
        )
    log.info("[netapp_storage] SMTP config saved")
    return jsonify({'success': True})


def _smtp_test():
    err = _require_admin()
    if err:
        return err
    db = get_db()
    _ensure_smtp_row(db)
    row = db.query_one("SELECT * FROM netapp_smtp_config WHERE id='default'")
    d = dict(row)
    host       = d.get('host', '').strip()
    port       = int(d.get('port') or 587)
    username   = d.get('username', '').strip()
    password   = db._decrypt(d.get('password_encrypted', ''))
    encryption = d.get('encryption', 'starttls')

    if not host:
        return jsonify({'success': False, 'error': 'SMTP host not configured'})

    try:
        _test_smtp_connection(host, port, username, password, encryption)
        return jsonify({'success': True})
    except Exception as exc:
        log.warning(f"[netapp_storage] SMTP test failed: {exc}")
        return jsonify({'success': False, 'error': str(exc)})


def _test_smtp_connection(host, port, username, password, encryption):
    ctx = ssl.create_default_context()
    if encryption == 'ssl':
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=10) as s:
            if username and password:
                s.login(username, password)
    elif encryption == 'starttls':
        with smtplib.SMTP(host, port, timeout=10) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            if username and password:
                s.login(username, password)
    else:
        with smtplib.SMTP(host, port, timeout=10) as s:
            s.ehlo()
            if username and password:
                s.login(username, password)


def _notify_on_permits(notify_on, is_failed, is_success):
    """Check a schedule's notify_on ('all'|'failed'|'success') filter against an outcome."""
    if notify_on == 'failed' and not is_failed:
        return False
    if notify_on == 'success' and not is_success:
        return False
    return True


def _format_lag(s):
    """Convert ISO 8601 duration (P0DT4H23M5S) to a human-readable string."""
    import re
    if not s:
        return "–"
    m = re.match(r'P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?', s)
    if not m:
        return s
    parts = []
    if m.group(1): parts.append(f"{m.group(1)}d")
    if m.group(2): parts.append(f"{m.group(2)}h")
    if m.group(3): parts.append(f"{m.group(3)}m")
    if m.group(4): parts.append(f"{int(float(m.group(4)))}s")
    return " ".join(parts) if parts else "0s"


def _build_notification_email(subject, schedule_name, snap_name, job_status, log_lines=None,
                               extra_rows=None, vm_list=None, datastore=None):
    """
    Returns (html_body, plain_body) for a single-job notification, built from the
    same banner -> card -> log-terminal -> footer blocks as the consolidated
    Protection Plan and digest report emails (see email_common.py) so all three
    "live" notification emails look like one family instead of drifting apart.
    """
    from . import email_common as ec

    entries = ec.log_entries(log_lines or [])
    has_err  = any(sev == "err"  for _, sev, _ in entries)
    has_warn = any(sev == "warn" for _, sev, _ in entries)
    is_done  = job_status == 'done'

    if not is_done or has_err:
        overall = "err"
    elif has_warn:
        overall = "warn"
    else:
        overall = "ok"

    # ── Visual config per overall status ─────────────────────────────────────
    _cfg = {
        "ok":   dict(banner=ec.GREEN, icon="✓", label="Snapshot Successful", dot_label="Success"),
        "warn": dict(banner=ec.AMBER, icon="⚠", label="Snapshot Completed with Warnings",
                     dot_label="Success (with warnings)"),
        "err":  dict(banner=ec.RED,   icon="✗", label="Snapshot Failed", dot_label="Failed"),
    }
    cfg = _cfg[overall]
    status_label = "Success" if is_done else "Failed"

    # ── Summary rows ─────────────────────────────────────────────────────────
    summary_rows = [
        ("Schedule",  ec.esc(schedule_name)),
        ("Snapshot",  ec.esc(snap_name)),
        ("Datastore", ec.esc(datastore)) if datastore else None,
        ("Status",    f'<span style="color:{cfg["banner"]};font-weight:700">● {cfg["dot_label"]}</span>'),
    ]
    summary_rows = [r for r in summary_rows if r is not None]
    if vm_list:
        summary_rows.append(("VMs", ec.render_vm_cell(vm_list)))
    if extra_rows:
        summary_rows.extend(extra_rows)

    summary_html = "".join(
        f'<tr>'
        f'<td style="padding:7px 12px 7px 0;color:#6b7280;white-space:nowrap;vertical-align:top;font-size:13px">{k}</td>'
        f'<td style="padding:7px 0;font-weight:500;word-break:break-all;font-size:13px">{v}</td>'
        f'</tr>'
        for k, v in summary_rows
    )

    body_html = (
        '<div style="background:#fff;padding:24px 28px;border-left:1px solid #e5e7eb;'
        'border-right:1px solid #e5e7eb">'
        f'<table style="width:100%;border-collapse:collapse">{summary_html}</table>'
        '</div>'
    )
    logs_section_html = (
        '<div style="background:#0f172a;border-radius:0 0 8px 8px;padding:20px 24px">'
        '<div style="font-size:11px;font-weight:700;color:#64748b;letter-spacing:.08em;'
        'text-transform:uppercase;margin-bottom:12px">Job Log</div>'
        '<div style="font-family:\'Courier New\',Courier,monospace;font-size:11.5px;line-height:1.65">'
        f'{ec.render_log_html(log_lines or [])}'
        '</div></div>'
    )

    inner = (
        ec.render_banner(cfg["banner"], cfg["icon"], cfg["label"],
                          "NaSnap — NetApp ONTAP Snapshot Management for Proxmox")
        + body_html
        + logs_section_html
        + ec.render_footer()
    )
    html = ec.wrap_shell(inner)

    # ── Plain-text fallback ───────────────────────────────────────────────────
    plain_lines = [
        subject,
        "=" * len(subject),
        "",
        f"Schedule  : {schedule_name}",
        f"Snapshot  : {snap_name}",
    ]
    if datastore:
        plain_lines.append(f"Datastore : {datastore}")
    plain_lines.append(f"Status    : {status_label}")
    if vm_list:
        vm_labels = [
            f"{(v.get('vm_type') or 'qemu').upper()} {v.get('vmid','?')}"
            + (f" ({v['name']})" if v.get('name') else "")
            for v in vm_list
        ]
        plain_lines.append(f"VMs      : {', '.join(vm_labels)}")
    plain_lines.append("")
    if entries:
        plain_lines.append("--- Log ---")
        plain_lines.append(ec.render_log_plain(log_lines or []))

    return html, "\n".join(plain_lines)


def send_job_notification(schedule_name, job_status, snap_name,
                          recipients_csv, notify_on, log_lines=None, vm_list=None,
                          datastore=None, snapmirror_info=None):
    """Send a snapshot job result notification email.

    Called from the snapshot engine after a scheduled job finishes.
    Recipients is a comma-separated string.  notify_on is 'all', 'failed', or 'success'.

    snapmirror_info: optional dict with keys exists, dest_cluster, dest_svm, dest_volume,
                     state, healthy, lag_time, last_transfer_time  (from the DB relationship
                     row for this volume).  Shown as an extra summary row in the email.
    """
    if not recipients_csv or not recipients_csv.strip():
        return
    if not _notify_on_permits(notify_on, job_status == 'failed', job_status == 'done'):
        return

    try:
        db = get_db()
        _ensure_smtp_row(db)
        row = db.query_one("SELECT * FROM netapp_smtp_config WHERE id='default'")
        d = dict(row)
        if not d.get('enabled'):
            return
        host       = d.get('host', '').strip()
        port       = int(d.get('port') or 587)
        username   = d.get('username', '').strip()
        password   = db._decrypt(d.get('password_encrypted', ''))
        encryption = d.get('encryption', 'starttls')
        from_addr  = d.get('from_address', '') or username
        if not host:
            return

        status_str = 'Success' if job_status == 'done' else job_status.capitalize()
        subject    = f"[NaSnap] Snapshot {status_str}: {schedule_name} — {snap_name}"

        # Build SnapMirror extra row for the summary card
        extra_rows = []
        if snapmirror_info and snapmirror_info.get("exists"):
            sm = snapmirror_info
            dest     = sm.get("dest_cluster") or sm.get("dest_svm") or "?"
            state    = sm.get("state") or "?"
            lag_str  = _format_lag(sm.get("lag_time") or "")
            last_t   = (sm.get("last_transfer_time") or "")[:16].replace("T", " ")
            if not sm.get("healthy") or state in ("broken_off", "broken-off"):
                color, icon = "#dc2626", "✗"
            elif state == "snapmirrored":
                color, icon = "#16a34a", "⟳"
            else:
                color, icon = "#d97706", "⚠"
            sm_val = (
                f'<span style="color:{color};font-weight:700">{icon} {dest}</span>'
                f' <span style="color:#6b7280;font-size:12px">({state}, lag: {lag_str}'
                f'{", last: " + last_t if last_t else ""})</span>'
            )
            extra_rows.append(("SnapMirror®", sm_val))
        elif snapmirror_info and not snapmirror_info.get("exists"):
            extra_rows.append(("SnapMirror®", '<span style="color:#6b7280">– not configured</span>'))

        html_body, plain_body = _build_notification_email(
            subject, schedule_name, snap_name, job_status, log_lines,
            extra_rows=extra_rows if extra_rows else None,
            vm_list=vm_list, datastore=datastore)

        recipients = [r.strip() for r in recipients_csv.split(',') if r.strip()]
        msg = email.mime.multipart.MIMEMultipart('alternative')
        msg['From']    = from_addr
        msg['To']      = ', '.join(recipients)
        msg['Subject'] = subject
        msg.attach(email.mime.text.MIMEText(plain_body, 'plain', 'utf-8'))
        msg.attach(email.mime.text.MIMEText(html_body,  'html',  'utf-8'))

        _send_smtp(host, port, username, password, encryption, from_addr, recipients, msg.as_string())
        log.info(f"[netapp_storage] Notification sent for schedule '{schedule_name}' ({job_status})")
    except Exception as exc:
        log.warning(f"[netapp_storage] Notification send failed: {exc}")


def send_schedule_consolidated_notification(schedule_name, overall_status,
                                            ds_results, recipients_csv, notify_on):
    """Send one consolidated email for a multi-datastore schedule run.

    ds_results is a list of dicts: {job_id, status, pve_storage_id, snap_name, log_lines}.
    """
    if not recipients_csv or not recipients_csv.strip():
        return
    if not _notify_on_permits(notify_on, overall_status == 'failed', overall_status == 'done'):
        return

    try:
        db = get_db()
        _ensure_smtp_row(db)
        row = db.query_one("SELECT * FROM netapp_smtp_config WHERE id='default'")
        d = dict(row)
        if not d.get('enabled'):
            return
        host       = d.get('host', '').strip()
        port       = int(d.get('port') or 587)
        username   = d.get('username', '').strip()
        password   = db._decrypt(d.get('password_encrypted', ''))
        encryption = d.get('encryption', 'starttls')
        from_addr  = d.get('from_address', '') or username
        if not host:
            return

        from . import email_common as ec

        status_str   = 'Success' if overall_status == 'done' else 'Failed'
        ds_count     = len(ds_results)
        failed_count = sum(1 for r in ds_results if r.get("status") != "done")
        ok_count     = ds_count - failed_count
        subject = (f"[NaSnap] Protection Plan {status_str}: {schedule_name} "
                   f"— {ds_count} datastores ({failed_count} failed)")

        banner_color = ec.GREEN if overall_status == "done" else ec.RED
        banner_icon  = "✓" if overall_status == "done" else "✗"
        banner_label = "All datastores protected successfully" if overall_status == "done" \
            else f"{failed_count} of {ds_count} datastore(s) failed"

        # KPI tiles: datastores OK, VMs protected, SnapMirror health (only if any SM configured)
        vm_total = sum(len(r.get("vm_list") or []) for r in ds_results)
        sm_results  = [r.get("snapmirror_info") for r in ds_results if (r.get("snapmirror_info") or {}).get("exists")]
        sm_total    = len(sm_results)
        sm_healthy  = sum(1 for sm in sm_results if sm.get("healthy"))
        kpi_tiles = [
            (f"{ok_count}/{ds_count}", "Datastores OK", ec.GREEN if failed_count == 0 else ec.RED),
            (str(vm_total), "VMs protected", None),
            (f"{sm_healthy}/{sm_total}", "SnapMirror healthy",
             ec.GREEN if sm_healthy == sm_total else ec.AMBER) if sm_total else None,
        ]

        # Split results: failures surface first in an action box, successes stay compact.
        failed_results  = [r for r in ds_results if r.get("status") != "done"]
        success_results = [r for r in ds_results if r.get("status") == "done"]

        action_html = ""
        if failed_results:
            items = ""
            for r in failed_results:
                ds_name = r.get("pve_storage_id") or r.get("job_id", "?")
                snippets = ec.worst_log_snippets(r.get("log_lines", []))
                detail = "<br>".join(ec.esc(s) for s in snippets) if snippets else ""
                items += ec.render_action_item(
                    f'<strong>{ec.esc(ds_name)}</strong> '
                    f'<span style="color:#6b7280;font-family:monospace;font-size:12px">'
                    f'{ec.esc(r.get("snap_name") or "—")}</span>',
                    detail,
                )
            action_html = ec.render_action_box(f"Action needed — {failed_count} datastore(s) failed", items)

        success_cards_html = ""
        success_plain_parts = []
        for r in success_results:
            ds_name = r.get("pve_storage_id") or r.get("job_id", "?")
            snap    = r.get("snap_name") or "—"
            header = (
                f'<div style="display:flex;justify-content:space-between;align-items:center">'
                f'<div style="font-size:13px"><span style="color:{ec.GREEN};font-weight:700">●</span> '
                f'<strong>{ec.esc(ds_name)}</strong> '
                f'<span style="color:#6b7280;font-family:monospace;font-size:12px">{ec.esc(snap)}</span></div>'
                f'</div>'
            )
            body = (
                f'<div style="margin-top:4px">{ec.render_vm_cell(r.get("vm_list") or [])} '
                f'{ec.render_sm_pill(r.get("snapmirror_info"))}</div>'
            )
            success_cards_html += ec.render_card(ec.GREEN, header, body)
            success_plain_parts.append(f"  [OK] {ds_name}  ({snap})")

        failed_plain_parts = []
        for r in failed_results:
            ds_name = r.get("pve_storage_id") or r.get("job_id", "?")
            snap    = r.get("snap_name") or "—"
            failed_plain_parts.append(
                f"  [FAILED] {ds_name}  ({snap})\n{ec.render_log_plain(r.get('log_lines', []))}"
            )

        # Full logs only for the failed datastores — successes stay noise-free (see dashboard for their logs).
        failed_logs_html = ""
        for r in failed_results:
            ds_name = r.get("pve_storage_id") or r.get("job_id", "?")
            failed_logs_html += (
                f'<div style="margin-top:20px">'
                f'  <div style="font-size:12px;font-weight:700;color:#94a3b8;text-transform:uppercase;'
                f'letter-spacing:.06em;margin-bottom:6px">{ec.esc(ds_name)}'
                f'  <span style="color:#f87171;margin-left:8px">● Failed</span></div>'
                f'  <div style="font-family:\'Courier New\',Courier,monospace;font-size:11.5px;line-height:1.65">'
                f'    {ec.render_log_html(r.get("log_lines", []))}'
                f'  </div>'
                f'</div>'
            )

        logs_section_html = ""
        if failed_logs_html:
            logs_section_html = (
                f'<div style="background:#0f172a;border-radius:0 0 8px 8px;padding:20px 24px">'
                f'<div style="font-size:11px;font-weight:700;color:#64748b;letter-spacing:.08em;'
                f'text-transform:uppercase;margin-bottom:4px">Failure Logs</div>'
                f'{failed_logs_html}</div>'
            )

        card_radius = "0 0 0 0" if logs_section_html else "0 0 8px 8px"
        body_html = (
            f'<div style="background:#fff;padding:20px 24px;border-left:1px solid #e5e7eb;'
            f'border-right:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;border-radius:{card_radius}">'
            f'{action_html}'
        )
        if success_cards_html:
            body_html += (
                f'<div style="font-size:12px;font-weight:700;color:#94a3b8;text-transform:uppercase;'
                f'letter-spacing:.06em;margin-bottom:8px">'
                f'{"✓ " if failed_results else ""}{ok_count} datastore(s) succeeded</div>'
                f'{success_cards_html}'
            )
        body_html += '</div>'

        inner = (
            ec.render_banner(banner_color, banner_icon, schedule_name, f"{banner_label} — NaSnap Protection Plan")
            + ec.render_kpi_row(kpi_tiles)
            + body_html
            + logs_section_html
            + ec.render_footer()
        )
        html_body = ec.wrap_shell(inner)

        plain_body = (
            f"NaSnap Protection Plan Report: {schedule_name}\n"
            f"Overall: {status_str}\n\n"
            f"Results ({ds_count} datastores):\n\n"
            + "\n\n".join(failed_plain_parts + success_plain_parts)
        )

        recipients = [r.strip() for r in recipients_csv.split(',') if r.strip()]
        msg = email.mime.multipart.MIMEMultipart('alternative')
        msg['From']    = from_addr
        msg['To']      = ', '.join(recipients)
        msg['Subject'] = subject
        msg.attach(email.mime.text.MIMEText(plain_body, 'plain', 'utf-8'))
        msg.attach(email.mime.text.MIMEText(html_body,  'html',  'utf-8'))
        _send_smtp(host, port, username, password, encryption, from_addr, recipients, msg.as_string())
        log.info(f"[netapp_storage] Consolidated notification sent for '{schedule_name}' ({overall_status})")
    except Exception as exc:
        log.warning(f"[netapp_storage] Consolidated notification failed: {exc}")


def send_digest_report(db, cfg, period_start_iso, period_end_iso, is_test=False):
    """Build and send the periodic (daily/weekly) digest report email.

    cfg: dict from netapp_report_config (recipients, warn_pct, critical_pct, mode, ...).
    """
    from .reporting import _build_digest_data

    recipients_csv = cfg.get('recipients', '')
    if not recipients_csv or not recipients_csv.strip():
        return

    try:
        _ensure_smtp_row(db)
        row = db.query_one("SELECT * FROM netapp_smtp_config WHERE id='default'")
        d = dict(row)
        if not d.get('enabled'):
            return
        host       = d.get('host', '').strip()
        port       = int(d.get('port') or 587)
        username   = d.get('username', '').strip()
        password   = db._decrypt(d.get('password_encrypted', ''))
        encryption = d.get('encryption', 'starttls')
        from_addr  = d.get('from_address', '') or username
        if not host:
            return

        warn_pct     = int(cfg.get('warn_pct') or 80)
        critical_pct = int(cfg.get('critical_pct') or 95)
        period_label = 'Weekly' if cfg.get('mode') == 'weekly' else 'Daily'

        data = _build_digest_data(db, period_start_iso, period_end_iso, warn_pct, critical_pct)
        plans      = data['plans']
        capacity   = data['capacity']
        any_fail   = any(p['fail_count'] > 0 for p in plans) or data['snap_failed'] > 0
        any_crit   = any(c['level'] == 'critical' for c in capacity)
        any_warn   = any(c['level'] == 'warn' for c in capacity)

        if any_fail or any_crit:
            banner_color, banner_icon, banner_label = "#dc2626", "✗", "Issues detected — action needed"
            overall_str = "Issues Detected"
        elif any_warn:
            banner_color, banner_icon, banner_label = "#d97706", "⚠", "Everything OK, with warnings"
            overall_str = "Warnings"
        else:
            banner_color, banner_icon, banner_label = "#16a34a", "✓", "Everything OK"
            overall_str = "All OK"

        subject = f"[NaSnap] {period_label} Report — {overall_str}"

        from . import email_common as ec
        _esc = ec.esc

        # Datastore name lookup for friendlier capacity rows
        ds_names = {}
        for r in (db.query("SELECT pve_storage_id, name FROM netapp_provisioned_datastores") or []):
            rd = dict(r)
            ds_names[rd.get('pve_storage_id', '')] = rd.get('name') or rd.get('pve_storage_id', '')

        at_risk = [c for c in capacity if c['level'] in ('warn', 'critical')]
        failing_plans = [p for p in plans if p['fail_count'] > 0]

        # KPI tiles: snapshots OK, SnapMirror health (if any configured), datastores at risk
        kpi_tiles = [
            (f"{data['snap_done']}/{data['snap_total']}", "Snapshots OK",
             ec.GREEN if data['snap_failed'] == 0 else ec.RED),
            (f"{data['sm_healthy']}/{data['sm_total']}", "SnapMirror healthy",
             ec.GREEN if data['sm_healthy'] == data['sm_total'] else ec.AMBER) if data['sm_total'] else None,
            (str(len(at_risk)), "Datastores at risk",
             ec.RED if any_crit else (ec.AMBER if any_warn else ec.GREEN)),
        ]

        # Action box: failing plans + critical datastores surfaced before anything else.
        action_html = ""
        if failing_plans or any_crit:
            items = ""
            for p in failing_plans:
                first_fail = p.get('failures') or []
                detail = _esc((first_fail[0].get('error') or '')[:200]) if first_fail else ""
                items += ec.render_action_item(
                    f'<strong>{_esc(p["schedule_name"])}</strong> '
                    f'<span style="color:#6b7280">— {p["fail_count"]} failed snapshot(s)</span>',
                    detail,
                )
            for c in [c for c in capacity if c['level'] == 'critical']:
                name = ds_names.get(c['storage_id'], c['storage_id'])
                items += ec.render_action_item(
                    f'<strong>{_esc(name)}</strong> '
                    f'<span style="color:#6b7280">— {c["used_pct"]:.1f}% used (critical)</span>'
                )
            action_html = ec.render_action_box("Action needed", items)

        # Protection plan cards
        plan_cards_html = ""
        plan_plain_parts = []
        if plans:
            for p in plans:
                ok = p['fail_count'] == 0
                col   = ec.GREEN if ok else ec.RED
                label = "OK" if ok else f"{p['fail_count']} failed"
                header = (
                    '<div style="display:flex;justify-content:space-between;align-items:center;font-size:13px">'
                    f'<span>{_esc(p["schedule_name"])}</span>'
                    f'<span><span style="color:{ec.GREEN}">{p["ok_count"]} ok</span>'
                    f'<span style="color:{col};font-weight:700;margin-left:8px">{_esc(label)}</span></span>'
                    '</div>'
                )
                body = ""
                if p.get('failures'):
                    rows = "".join(
                        f'<div style="margin-top:3px">{_esc(f.get("snap_name",""))} — {_esc((f.get("error") or "")[:200])}</div>'
                        for f in p['failures']
                    )
                    body = f'<div style="font-family:monospace;font-size:11.5px;color:#b91c1c;margin-top:6px">{rows}</div>'
                plan_cards_html += ec.render_card(col, header, body)
                plan_plain_parts.append(f"  {p['schedule_name']}: {p['ok_count']} ok, {label}")
        else:
            plan_cards_html = '<div style="color:#6b7280;font-style:italic;font-size:13px">No protection plan runs in this period.</div>'

        # Datastore-at-risk cards with a capacity meter
        cap_cards_html = ""
        cap_plain_parts = []
        if at_risk:
            for c in at_risk:
                col  = ec.RED if c['level'] == 'critical' else ec.AMBER
                name = ds_names.get(c['storage_id'], c['storage_id'])
                header = (
                    '<div style="display:flex;justify-content:space-between;align-items:center;font-size:13px">'
                    f'<span>{_esc(name)}</span>'
                    f'<span style="color:{col};font-weight:700">{c["used_pct"]:.1f}% · {c["level"].upper()}</span>'
                    '</div>'
                )
                body = ec.render_meter(c['used_pct'], col)
                cap_cards_html += ec.render_card(col, header, body)
                cap_plain_parts.append(f"  {name}: {c['used_pct']:.1f}% ({c['level']})")
        else:
            cap_cards_html = '<div style="color:#6b7280;font-style:italic;font-size:13px">All datastores below warning threshold.</div>'

        sm_line = f"{data['sm_healthy']} / {data['sm_total']} SnapMirror relationships healthy" if data['sm_total'] else "No SnapMirror relationships configured"

        body_html = (
            '<div style="background:#fff;padding:20px 24px;border-left:1px solid #e5e7eb;'
            'border-right:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;border-radius:0 0 8px 8px">'
            f'{action_html}'
            '<div style="font-size:12px;font-weight:700;color:#94a3b8;text-transform:uppercase;'
            'letter-spacing:.06em;margin-bottom:8px">Protection Plans</div>'
            f'{plan_cards_html}'
            '<div style="font-size:12px;font-weight:700;color:#94a3b8;text-transform:uppercase;'
            'letter-spacing:.06em;margin:20px 0 8px">Datastores at Risk</div>'
            f'{cap_cards_html}'
            '</div>'
        )

        inner = (
            ec.render_banner(banner_color, banner_icon, f"{period_label} Report", f"{banner_label} — NaSnap")
            + ec.render_kpi_row(kpi_tiles)
            + body_html
            + ec.render_footer(' (test report)' if is_test else '')
        )
        html_body = ec.wrap_shell(inner)

        plain_body = (
            f"NaSnap {period_label} Report — {overall_str}\n\n"
            f"{data['snap_total']} snapshot(s) — {data['snap_done']} succeeded, {data['snap_failed']} failed.\n"
            f"{sm_line}.\n\n"
            f"Protection Plans:\n" + ("\n".join(plan_plain_parts) if plan_plain_parts else "  (no runs in this period)") + "\n\n"
            f"Datastores at Risk:\n" + ("\n".join(cap_plain_parts) if cap_plain_parts else "  (none)")
        )

        recipients = [r.strip() for r in recipients_csv.split(',') if r.strip()]
        msg = email.mime.multipart.MIMEMultipart('alternative')
        msg['From']    = from_addr
        msg['To']      = ', '.join(recipients)
        msg['Subject'] = subject
        msg.attach(email.mime.text.MIMEText(plain_body, 'plain', 'utf-8'))
        msg.attach(email.mime.text.MIMEText(html_body,  'html',  'utf-8'))
        _send_smtp(host, port, username, password, encryption, from_addr, recipients, msg.as_string())
        log.info(f"[netapp_storage] {period_label} digest report sent ({overall_str}){' [test]' if is_test else ''}")

        if not is_test:
            db.execute(
                "UPDATE netapp_report_config SET last_run_at=? WHERE id='default'",
                (datetime.now(timezone.utc).isoformat(),),
            )
    except Exception as exc:
        log.warning(f"[netapp_storage] Digest report failed: {exc}")


def _send_smtp(host, port, username, password, encryption, from_addr, recipients, raw_message):
    ctx = ssl.create_default_context()
    if encryption == 'ssl':
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as s:
            if username and password:
                s.login(username, password)
            s.sendmail(from_addr, recipients, raw_message)
    elif encryption == 'starttls':
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            if username and password:
                s.login(username, password)
            s.sendmail(from_addr, recipients, raw_message)
    else:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            if username and password:
                s.login(username, password)
            s.sendmail(from_addr, recipients, raw_message)


def _notify_test():
    """Send a test notification to the supplied recipients."""
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    recipients_csv = data.get('recipients', '').strip()
    if not recipients_csv:
        return jsonify({'success': False, 'error': 'No recipients provided'})

    db = get_db()
    _ensure_smtp_row(db)
    row = db.query_one("SELECT * FROM netapp_smtp_config WHERE id='default'")
    d = dict(row)
    host       = d.get('host', '').strip()
    port       = int(d.get('port') or 587)
    username   = d.get('username', '').strip()
    password   = db._decrypt(d.get('password_encrypted', ''))
    encryption = d.get('encryption', 'starttls')
    from_addr  = d.get('from_address', '').strip() or username

    if not host:
        return jsonify({'success': False, 'error': 'SMTP host not configured'})

    recipients = [r.strip() for r in recipients_csv.split(',') if r.strip()]
    now_str = datetime.now(timezone.utc).isoformat()

    subject = '[NaSnap] Test notification — NetApp ONTAP Snapshot Management'
    fake_log = [
        {"ts": now_str, "msg": "SMTP connection test initiated"},
        {"ts": now_str, "msg": "If you received this email, notifications are configured correctly."},
    ]
    fake_vms = [
        {"vmid": 100, "name": "web-prod-01",  "vm_type": "qemu"},
        {"vmid": 101, "name": "db-prod-01",   "vm_type": "qemu"},
        {"vmid": 200, "name": "alpine-proxy", "vm_type": "lxc"},
    ]
    html_body, plain_body = _build_notification_email(
        subject, "— test —", "— test —", "done", fake_log,
        extra_rows=[("Sent", now_str)], vm_list=fake_vms,
        datastore="nfs-prod-01",
    )
    msg = email.mime.multipart.MIMEMultipart('alternative')
    msg['From']    = from_addr
    msg['To']      = ', '.join(recipients)
    msg['Subject'] = subject
    msg.attach(email.mime.text.MIMEText(plain_body, 'plain', 'utf-8'))
    msg.attach(email.mime.text.MIMEText(html_body,  'html',  'utf-8'))

    try:
        _send_smtp(host, port, username, password, encryption, from_addr, recipients, msg.as_string())
        log.info(f"[netapp_storage] Test notification sent to {recipients_csv}")
        return jsonify({'success': True})
    except Exception as exc:
        log.warning(f"[netapp_storage] Test notification failed: {exc}")
        return jsonify({'success': False, 'error': str(exc)})


# Tables exported in dependency order (parents before children so import doesn't
# hit FK constraints on a fresh DB).
_EXPORT_TABLES = [
    'netapp_endpoints',
    'netapp_pve_hosts',
    'netapp_smtp_config',
    'netapp_report_config',
    'netapp_volume_mapping',
    'netapp_provisioned_datastores',
    'netapp_snapshot_schedules',
    'netapp_snapmirror_relationships',
    'netapp_dr_sites',
    'netapp_dr_plans',
    'netapp_dr_plan_entries',
    'netapp_dr_vm_groups',
    'netapp_dr_vm_assignments',
]


def build_export_payload():
    """Build and return the export payload dict (usable by sync and download)."""
    db = get_db()
    payload = {
        'version': '1',
        'plugin':  'netapp_storage',
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'tables': {},
    }
    for table in _EXPORT_TABLES:
        rows = db.query(f"SELECT * FROM {table}")
        payload['tables'][table] = [dict(r) for r in rows]
    return payload


def _db_export():
    err = _require_admin()
    if err:
        return err
    payload = build_export_payload()
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    filename = f'netapp_storage_backup_{ts}.json'
    return Response(
        json.dumps(payload, indent=2, ensure_ascii=False),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


def apply_import_payload(payload):
    """Apply an export payload dict to the local DB. Returns stats dict."""
    if payload.get('plugin') != 'netapp_storage':
        raise ValueError('Backup file is not from the netapp_storage plugin')
    if str(payload.get('version')) != '1':
        raise ValueError(f"Unsupported backup version: {payload.get('version')}")
    tables = payload.get('tables', {})
    db = get_db()
    stats = {}
    for table in _EXPORT_TABLES:
        rows = tables.get(table, [])
        if not rows:
            stats[table] = 0
            continue
        inserted = 0
        for row in rows:
            cols = ', '.join(row.keys())
            placeholders = ', '.join(['?' for _ in row])
            sql = f'INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})'
            try:
                db.execute(sql, list(row.values()))
                inserted += 1
            except Exception as exc:
                log.warning(f'[netapp_storage] import: skipped row in {table}: {exc}')
        stats[table] = inserted
    total = sum(stats.values())
    log.info(f'[netapp_storage] DB import: {total} rows restored — {stats}')
    return {'success': True, 'rows_imported': total, 'per_table': stats}


def _load_payload_bytes(raw: bytes):
    """Decompress if gzip, then JSON-parse. Returns payload dict."""
    import gzip as _gzip
    if raw[:2] == b'\x1f\x8b':
        raw = _gzip.decompress(raw)
    return json.loads(raw)


def _decrypt_backup_cfg(row, db):
    cfg = dict(row)
    if cfg.get('sftp_password_enc'):
        cfg['sftp_password'] = db._decrypt(cfg['sftp_password_enc'])
    if cfg.get('cifs_password_enc'):
        cfg['cifs_password'] = db._decrypt(cfg['cifs_password_enc'])
    return cfg


def _db_import():
    err = _require_admin()
    if err:
        return err

    # Accept multipart file upload (JSON or gzip JSON) or raw JSON body
    if request.content_type and 'multipart' in request.content_type:
        f = request.files.get('file')
        if not f:
            return jsonify({'error': 'No file uploaded'}), 400
        try:
            payload = _load_payload_bytes(f.read())
        except Exception as exc:
            return jsonify({'error': f'Invalid file: {exc}'}), 400
    else:
        try:
            payload = request.get_json(force=True) or {}
        except Exception as exc:
            return jsonify({'error': f'Invalid JSON: {exc}'}), 400

    try:
        result = apply_import_payload(payload)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400


def _backup_list_remote():
    err = _require_admin()
    if err:
        return err
    db = get_db()
    _ensure_backup_config_row(db)
    row = db.query_one("SELECT * FROM netapp_db_backup_config WHERE id='default'")
    if not row or not dict(row).get('target_type'):
        return jsonify({'error': 'No backup target configured'}), 400
    cfg = _decrypt_backup_cfg(dict(row), db)
    from ..core.db_backup import DbBackupRunner
    try:
        files = DbBackupRunner().list(cfg, db)
        return jsonify({'files': files})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


def _backup_restore_from_remote():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    filename = data.get('filename', '').strip()
    if not filename:
        return jsonify({'error': 'filename required'}), 400
    db = get_db()
    _ensure_backup_config_row(db)
    row = db.query_one("SELECT * FROM netapp_db_backup_config WHERE id='default'")
    if not row or not dict(row).get('target_type'):
        return jsonify({'error': 'No backup target configured'}), 400
    cfg = _decrypt_backup_cfg(dict(row), db)
    from ..core.db_backup import DbBackupRunner
    try:
        raw = DbBackupRunner().fetch(cfg, db, filename)
        payload = _load_payload_bytes(raw)
        result = apply_import_payload(payload)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ── Plugin Updater ────────────────────────────────────────────────────────────

def _get_current_version():
    """Read version from manifest.json."""
    manifest = os.path.join(_PLUGIN_DIR, 'manifest.json')
    try:
        with open(manifest) as f:
            return json.load(f).get('version', 'unknown')
    except Exception:
        return 'unknown'


def _gh_request(path):
    """Make a GitHub API GET request, returns parsed JSON or raises."""
    import urllib.request
    url = f"{_GITHUB_API}{path}"
    req = urllib.request.Request(
        url,
        headers={
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'netapp-ontap-plugin/updater',
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _update_info():
    """GET — return current version + GitHub release/branch info."""
    result = {
        'current_version': _get_current_version(),
        'release': None,
        'branches': {},
    }

    # Latest release
    try:
        data = _gh_request(f"/repos/{_GITHUB_REPO}/releases/latest")
        result['release'] = {
            'tag':          data.get('tag_name', ''),
            'name':         data.get('name', ''),
            'published_at': data.get('published_at', ''),
            'url':          data.get('html_url', ''),
            'body':         (data.get('body') or '')[:600],
        }
    except Exception as exc:
        result['release'] = {'error': str(exc)}

    # Latest commit on main and dev branches
    for branch in ('main', 'dev'):
        try:
            data = _gh_request(f"/repos/{_GITHUB_REPO}/commits/{branch}")
            result['branches'][branch] = {
                'sha':     data['sha'][:8],
                'sha_full': data['sha'],
                'date':    data['commit']['committer']['date'],
                'message': data['commit']['message'].splitlines()[0][:120],
            }
        except Exception as exc:
            result['branches'][branch] = {'error': str(exc)}

    return jsonify(result)


def _update_apply():
    """POST {branch: 'main'|'dev'} — download ZIP from GitHub and overwrite plugin files."""
    err = _require_admin()
    if err:
        return err

    import urllib.request
    import urllib.error
    import zipfile
    import tempfile
    import shutil

    data    = request.get_json() or {}
    branch  = data.get('branch', 'main')
    if branch not in ('main', 'dev'):
        return jsonify({'error': 'Invalid branch — must be main or dev'}), 400

    zip_url = f"https://github.com/{_GITHUB_REPO}/archive/refs/heads/{branch}.zip"
    tmp_zip = None

    try:
        # ── 1. Download archive ──────────────────────────────────────────────
        log.info(f"[netapp_storage] Downloading update from {zip_url}")
        req = urllib.request.Request(zip_url, headers={'User-Agent': 'netapp-ontap-plugin/updater'})
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            tmp_zip = tmp.name
            with urllib.request.urlopen(req, timeout=90) as r:
                shutil.copyfileobj(r, tmp)

        # ── 2. Extract + copy ────────────────────────────────────────────────
        # Files / dirs that must never be overwritten (user data)
        _SKIP_FILES = {'config.json'}
        _SKIP_DIRS  = {'__pycache__'}

        copied = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(tmp_zip) as zf:
                zf.extractall(tmp_dir)

            # GitHub zips always have one top-level folder (repo-branch/)
            entries = os.listdir(tmp_dir)
            if not entries:
                return jsonify({'error': 'Downloaded archive is empty'}), 500
            src_root = os.path.join(tmp_dir, entries[0])

            for root, dirs, files in os.walk(src_root):
                # Prune dirs we don't want to recurse into
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]

                rel_root  = os.path.relpath(root, src_root)
                dest_root = os.path.join(_PLUGIN_DIR, rel_root) if rel_root != '.' else _PLUGIN_DIR
                os.makedirs(dest_root, exist_ok=True)

                for fname in files:
                    if fname in _SKIP_FILES or fname.endswith('.pyc'):
                        continue
                    src_f  = os.path.join(root, fname)
                    dest_f = os.path.join(dest_root, fname)
                    shutil.copy2(src_f, dest_f)
                    rel_path = os.path.join(rel_root, fname) if rel_root != '.' else fname
                    copied.append(rel_path)

        log.info(f"[netapp_storage] Update applied from '{branch}': {len(copied)} files replaced")
        return jsonify({
            'success':       True,
            'branch':        branch,
            'files_updated': len(copied),
            'message':       (
                f'Plugin updated from branch \'{branch}\'. '
                'Please restart NaSnap to activate the new version.'
            ),
        })

    except Exception as exc:
        log.error(f"[netapp_storage] Update apply failed: {exc}")
        return jsonify({'error': str(exc)}), 500
    finally:
        if tmp_zip and os.path.exists(tmp_zip):
            try:
                os.unlink(tmp_zip)
            except Exception:
                pass


# ── Automated DB Backup ───────────────────────────────────────────────────────

def _ensure_backup_config_row(db):
    if not db.query_one("SELECT id FROM netapp_db_backup_config WHERE id='default'"):
        db.execute("INSERT INTO netapp_db_backup_config (id) VALUES ('default')")


def _backup_config_get():
    err = _require_admin()
    if err:
        return err
    db = get_db()
    _ensure_backup_config_row(db)
    row = dict(db.query_one("SELECT * FROM netapp_db_backup_config WHERE id='default'"))

    # Compute next scheduled run via croniter (if available)
    cron_next = ''
    try:
        from croniter import croniter
        cron_next = croniter(row.get('cron_expr', '0 2 * * *'),
                             datetime.now()).get_next(datetime).strftime('%Y-%m-%d %H:%M')
    except Exception:
        pass

    return jsonify({
        'enabled':           bool(row.get('enabled')),
        'cron_expr':         row.get('cron_expr', '0 2 * * *'),
        'cron_next':         cron_next,
        'target_type':       row.get('target_type', ''),
        'sftp_host':         row.get('sftp_host', ''),
        'sftp_port':         row.get('sftp_port', 22),
        'sftp_user':         row.get('sftp_user', ''),
        'sftp_path':         row.get('sftp_path', ''),
        'has_sftp_password': bool(row.get('sftp_password_enc')),
        'cifs_host':         row.get('cifs_host', ''),
        'cifs_share':        row.get('cifs_share', ''),
        'cifs_user':         row.get('cifs_user', ''),
        'cifs_domain':       row.get('cifs_domain', ''),
        'cifs_path':         row.get('cifs_path', ''),
        'has_cifs_password': bool(row.get('cifs_password_enc')),
        'nfs_ds_id':         row.get('nfs_ds_id', ''),
        'nfs_subdir':        row.get('nfs_subdir', '.nasnap/db_backups'),
        'keep_copies':       row.get('keep_copies', 7),
        'last_run_at':       row.get('last_run_at', ''),
        'last_run_status':   row.get('last_run_status', ''),
        'last_run_error':    row.get('last_run_error', ''),
    })


def _backup_config_save():
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    db = get_db()
    _ensure_backup_config_row(db)

    cron_expr = data.get('cron_expr', '0 2 * * *').strip()
    try:
        from croniter import croniter
        croniter(cron_expr)
    except Exception:
        return jsonify({'error': f'Invalid cron expression: {cron_expr}'}), 400

    fields = {
        'enabled':     1 if data.get('enabled') else 0,
        'cron_expr':   cron_expr,
        'target_type': data.get('target_type', '').strip(),
        'sftp_host':   data.get('sftp_host', '').strip(),
        'sftp_port':   int(data.get('sftp_port') or 22),
        'sftp_user':   data.get('sftp_user', '').strip(),
        'sftp_path':   data.get('sftp_path', '').strip(),
        'cifs_host':   data.get('cifs_host', '').strip(),
        'cifs_share':  data.get('cifs_share', '').strip(),
        'cifs_user':   data.get('cifs_user', '').strip(),
        'cifs_domain': data.get('cifs_domain', '').strip(),
        'cifs_path':   data.get('cifs_path', '').strip(),
        'nfs_ds_id':   data.get('nfs_ds_id', '').strip(),
        'nfs_subdir':  data.get('nfs_subdir', '.nasnap/db_backups').strip() or '.nasnap/db_backups',
        'keep_copies': max(1, int(data.get('keep_copies') or 7)),
    }

    if data.get('sftp_password'):
        fields['sftp_password_enc'] = db._encrypt(data['sftp_password'])
    if data.get('cifs_password'):
        fields['cifs_password_enc'] = db._encrypt(data['cifs_password'])

    set_clause = ', '.join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE netapp_db_backup_config SET {set_clause} WHERE id='default'",
               list(fields.values()))
    log.info("[netapp_storage] DB backup config saved")
    return jsonify({'success': True})


def _backup_run_now():
    err = _require_admin()
    if err:
        return err
    db = get_db()
    _ensure_backup_config_row(db)
    row = dict(db.query_one("SELECT * FROM netapp_db_backup_config WHERE id='default'"))

    if not row.get('target_type'):
        return jsonify({'success': False, 'error': 'No backup target configured'}), 400

    cfg = dict(row)
    if cfg.get('sftp_password_enc'):
        cfg['sftp_password'] = db._decrypt(cfg['sftp_password_enc'])
    if cfg.get('cifs_password_enc'):
        cfg['cifs_password'] = db._decrypt(cfg['cifs_password_enc'])

    from ..core.db_backup import DbBackupRunner
    result = DbBackupRunner().run(cfg, db)

    now_iso = datetime.now(timezone.utc).isoformat()
    status  = 'success' if result.get('success') else 'failed'
    db.execute(
        "UPDATE netapp_db_backup_config "
        "SET last_run_at=?, last_run_status=?, last_run_error=? WHERE id='default'",
        (now_iso, status, result.get('error', '')),
    )
    return jsonify(result)


def _check_and_run_backup():
    """Called from the backup scheduler thread every minute."""
    try:
        from nasnap_core.core.db import get_db as _get_db
        db = _get_db()
        row = db.query_one("SELECT * FROM netapp_db_backup_config WHERE id='default'")
        if not row:
            return
        cfg = dict(row)
        if not cfg.get('enabled') or not cfg.get('target_type'):
            return

        from croniter import croniter
        now  = datetime.now()
        prev = croniter(cfg.get('cron_expr', '0 2 * * *'), now).get_prev(datetime)

        last_run = cfg.get('last_run_at', '')
        if last_run:
            last_dt = datetime.fromisoformat(last_run.rstrip('Z').split('+')[0])
            if prev <= last_dt:
                return  # already ran for this slot

        if cfg.get('sftp_password_enc'):
            cfg['sftp_password'] = db._decrypt(cfg['sftp_password_enc'])
        if cfg.get('cifs_password_enc'):
            cfg['cifs_password'] = db._decrypt(cfg['cifs_password_enc'])

        from ..core.db_backup import DbBackupRunner
        result  = DbBackupRunner().run(cfg, db)
        now_iso = datetime.now(timezone.utc).isoformat()
        status  = 'success' if result.get('success') else 'failed'
        db.execute(
            "UPDATE netapp_db_backup_config "
            "SET last_run_at=?, last_run_status=?, last_run_error=? WHERE id='default'",
            (now_iso, status, result.get('error', '')),
        )
        log.info(f"[netapp_storage] Scheduled DB backup: {status} — {result.get('filename','')}")
    except Exception as exc:
        log.warning(f"[netapp_storage] DB backup scheduler error: {exc}")


def start_db_backup_scheduler():
    def _loop():
        while True:
            time.sleep(60)
            _check_and_run_backup()
    threading.Thread(target=_loop, daemon=True, name="nasnap-db-backup").start()
    log.info("[netapp_storage] DB backup scheduler started")


def _pve_health_check():
    """Scan all configured PVE hosts for stale NFS mounts, leftover SFR temp dirs and VGs."""
    err = _require_admin()
    if err:
        return err

    import shlex
    from ..core._helpers import ssh_run, build_pve_client

    db = get_db()
    hosts = db.query("SELECT id, name, host FROM netapp_pve_hosts ORDER BY name") or []
    results = []

    for h in hosts:
        rec  = {
            "host_id":    h["id"],
            "host_name":  h["name"],
            "host":       h["host"],
            "stale_nfs":  [],
            "sfr_dirs":   [],
            "nasnap_vgs": [],
            "error":      "",
        }
        try:
            pve  = build_pve_client(db, h["id"])
            host = pve.host
            user = pve.ssh_user
            pw   = pve.ssh_password

            # Stale NFS: df stderr emits "df: <path>: Stale file handle"
            out = ssh_run(host, user, pw,
                "timeout 30 df 2>&1 | grep 'Stale file handle' | sed 's/df: //;s/: Stale file handle//'",
                capture=True, timeout=40)
            for line in out.strip().splitlines():
                mp = line.strip()
                if mp:
                    rec["stale_nfs"].append(mp)

            # SFR leftover temp dirs
            out2 = ssh_run(host, user, pw,
                r"for d in /tmp/nasnap-sfr-*/; do [ -d \"$d\" ] || continue; "
                r"mountpoint -q \"${d}mnt\" 2>/dev/null && echo \"mounted:${d%/}\" || echo \"dir:${d%/}\"; "
                r"done 2>/dev/null || true",
                capture=True, timeout=15)
            for line in out2.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                mounted = line.startswith("mounted:")
                path    = line.split(":", 1)[1] if ":" in line else line
                rec["sfr_dirs"].append({"path": path, "mounted": mounted})

            # Leftover nasnap LVM VGs (from SFR clone sessions)
            out3 = ssh_run(host, user, pw,
                "vgs --noheadings -o vg_name 2>/dev/null | grep nasnap_sfr || true",
                capture=True, timeout=10)
            for line in out3.strip().splitlines():
                vg = line.strip()
                if vg:
                    rec["nasnap_vgs"].append(vg)

            # Orphaned NBD devices: sysfs pid present but process is dead.
            # qemu-nbd communicates via socket (not open fds on /dev/nbdX), so fuser is
            # unreliable. The kernel exposes the server PID at /sys/block/nbdX/pid.
            # After a clean disconnect the kernel clears the pid (even if size stays set),
            # so pid-first detection avoids false positives on already-disconnected devices.
            out4 = ssh_run(host, user, pw,
                "for d in /dev/nbd*; do "
                "  [[ \"$d\" =~ p[0-9]+$ ]] && continue; "
                "  base=$(basename \"$d\"); "
                "  pid=$(cat /sys/block/$base/pid 2>/dev/null | tr -d '[:space:]'); "
                "  [ -z \"$pid\" ] || [ \"$pid\" = \"0\" ] && continue; "
                "  sz=$(blockdev --getsize64 \"$d\" 2>/dev/null || echo 0); "
                "  [ \"$sz\" -gt 0 ] 2>/dev/null || continue; "
                "  kill -0 \"$pid\" 2>/dev/null || echo \"$d\"; "
                "done 2>/dev/null || true",
                capture=True, timeout=15)
            rec["orphan_nbds"] = [l.strip() for l in out4.strip().splitlines() if l.strip()]

            # Orphaned NVMe subsystems: registered in kernel but no active controllers.
            # Use nvme show-topology (preferred) or fall back to nvme list-subsys.
            # Both show subsystem headers; orphaned ones have no "+- " child lines.
            out5 = ssh_run(host, user, pw,
                "(nvme show-topology 2>/dev/null || nvme list-subsys 2>/dev/null) | "
                "awk '/^nvme-subsys[0-9]+ - NQN=/{if(nqn&&!h)print nqn; n=$0; "
                "sub(/^.*NQN=/,\"\",n); sub(/ .*/,\"\",n); nqn=n; h=0} "
                "/^[[:space:]]*[+]-/{h=1} END{if(nqn&&!h)print nqn}' 2>/dev/null || true",
                capture=True, timeout=15)
            rec["orphan_nvme_subsys"] = [l.strip() for l in out5.strip().splitlines() if l.strip()]

            # Package / service stack check
            stack_script = (
                "which mount.nfs >/dev/null 2>&1 && echo nfs_bin=ok || echo nfs_bin=missing;"
                "which iscsiadm >/dev/null 2>&1 && echo iscsi_bin=ok || echo iscsi_bin=missing;"
                "printf 'iscsid_active=%s\\n' \"$(systemctl is-active iscsid 2>/dev/null || echo inactive)\";"
                "{ [ -f /etc/iscsi/initiatorname.iscsi ] && grep -qE '^InitiatorName=.+' /etc/iscsi/initiatorname.iscsi 2>/dev/null; } && echo initiator=ok || echo initiator=missing;"
                "which nvme >/dev/null 2>&1 && echo nvme_bin=ok || echo nvme_bin=missing;"
                "lsmod 2>/dev/null | grep -qw nvme_tcp && echo nvme_tcp_loaded=yes || echo nvme_tcp_loaded=no;"
                "grep -rqw nvme_tcp /etc/modules /etc/modules-load.d/ 2>/dev/null && echo nvme_tcp_persist=yes || echo nvme_tcp_persist=no;"
                "which vgscan >/dev/null 2>&1 && echo lvm2=ok || echo lvm2=missing;"
                "which qemu-nbd >/dev/null 2>&1 && echo qemu_nbd=ok || echo qemu_nbd=missing;"
            )
            out6 = ssh_run(host, user, pw, stack_script, capture=True, timeout=20)
            kv = {}
            for line in out6.strip().splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    kv[k.strip()] = v.strip()
            iscsid_active = kv.get("iscsid_active", "inactive") in ("active", "activating")
            rec["stack"] = {
                "nfs":      {"ok": kv.get("nfs_bin") == "ok",
                             "fix_pkg": "apt-get install -y nfs-common"},
                "iscsi":    {"ok": kv.get("iscsi_bin") == "ok" and iscsid_active and kv.get("initiator") == "ok",
                             "bin_ok": kv.get("iscsi_bin") == "ok",
                             "svc_ok": iscsid_active,
                             "ini_ok": kv.get("initiator") == "ok",
                             "fix_pkg": "apt-get install -y open-iscsi",
                             "fix_svc": "iscsid_enable"},
                "nvme":     {"ok": kv.get("nvme_bin") == "ok" and kv.get("nvme_tcp_loaded") == "yes",
                             "bin_ok": kv.get("nvme_bin") == "ok",
                             "mod_loaded": kv.get("nvme_tcp_loaded") == "yes",
                             "mod_persist": kv.get("nvme_tcp_persist") == "yes",
                             "fix_pkg": "apt-get install -y nvme-cli",
                             "fix_mod": "nvme_tcp_load"},
                "lvm2":     {"ok": kv.get("lvm2") == "ok",
                             "fix_pkg": "apt-get install -y lvm2"},
                "qemu_nbd": {"ok": kv.get("qemu_nbd") == "ok",
                             "fix_pkg": "apt-get install -y qemu-utils"},
            }

        except Exception as e:
            rec["error"] = str(e)

        results.append(rec)

    return {"hosts": results}


def _pve_stack_fix():
    """Apply auto-fixable stack issues on a PVE host (enable iscsid, load nvme_tcp)."""
    err = _require_admin()
    if err:
        return err

    from ..core._helpers import ssh_run, build_pve_client

    data    = request.get_json() or {}
    host_id = str(data.get("host_id", "")).strip()
    fixes   = data.get("fixes", [])

    if not host_id:
        return {"error": "host_id required"}, 400

    db = get_db()
    if not db.query_one("SELECT id FROM netapp_pve_hosts WHERE id=?", (host_id,)):
        return {"error": "PVE host not found"}, 404

    pve  = build_pve_client(db, host_id)
    host = pve.host
    user = pve.ssh_user
    pw   = pve.ssh_password

    fix_cmds = {
        "iscsid_enable": "systemctl enable --now open-iscsi iscsid 2>&1; echo exit=$?",
        "nvme_tcp_load":  (
            "modprobe nvme_tcp 2>&1 && "
            "printf 'nvme_tcp\\n' > /etc/modules-load.d/nasnap-nvme.conf && "
            "echo ok || echo failed"
        ),
    }

    results = []
    for fix in fixes:
        cmd = fix_cmds.get(fix)
        if not cmd:
            continue
        try:
            out = ssh_run(host, user, pw, cmd, capture=True, timeout=30)
            results.append({"fix": fix, "ok": True, "output": out.strip()})
        except Exception as e:
            results.append({"fix": fix, "ok": False, "error": str(e)})

    return {"results": results}


def _pve_cleanup():
    """Clean up stale NFS mounts, SFR temp dirs and leftover VGs on a PVE host."""
    err = _require_admin()
    if err:
        return err

    import shlex
    from ..core._helpers import ssh_run, build_pve_client

    data               = request.get_json() or {}
    host_id            = str(data.get("host_id", "")).strip()
    stale_nfs          = data.get("stale_nfs",          [])
    sfr_dirs           = data.get("sfr_dirs",           [])
    nasnap_vgs         = data.get("nasnap_vgs",         [])
    orphan_nbds        = data.get("orphan_nbds",        [])
    orphan_nvme_subsys = data.get("orphan_nvme_subsys", [])

    if not host_id:
        return {"error": "host_id required"}, 400

    db = get_db()
    if not db.query_one("SELECT id FROM netapp_pve_hosts WHERE id=?", (host_id,)):
        return {"error": "PVE host not found"}, 404

    pve  = build_pve_client(db, host_id)
    host = pve.host
    user = pve.ssh_user
    pw   = pve.ssh_password
    items = []

    for mp in stale_nfs:
        ok, errmsg = True, ""
        try:
            ssh_run(host, user, pw,
                f"umount -l {shlex.quote(mp)} 2>/dev/null || true",
                timeout=10)
        except Exception as e:
            ok, errmsg = False, str(e)
        items.append({"type": "stale_nfs", "target": mp, "ok": ok, "error": errmsg})

    # SFR dirs: kill users → find nbd device → unmount → disconnect nbd → rm -rf
    for d in sfr_dirs:
        path = d if isinstance(d, str) else d.get("path", "")
        ok, errmsg = True, ""
        try:
            ssh_run(host, user, pw,
                f"mnt={shlex.quote(path)}/mnt; "
                f"fuser -km \"$mnt\" 2>/dev/null; "
                f"nbd=$(grep \" $mnt \" /proc/mounts 2>/dev/null | awk '{{print $1}}' | sed 's/p[0-9]*$//'); "
                f"umount \"$mnt\" 2>/dev/null || umount -l \"$mnt\" 2>/dev/null; "
                f"[ -n \"$nbd\" ] && qemu-nbd -d \"$nbd\" 2>/dev/null; "
                f"rm -rf {shlex.quote(path)} 2>/dev/null || true",
                timeout=30)
        except Exception as e:
            ok, errmsg = False, str(e)
        items.append({"type": "sfr_dir", "target": path, "ok": ok, "error": errmsg})

    # LVM VGs: deactivate all LVs then remove
    for vg in nasnap_vgs:
        ok, errmsg = True, ""
        try:
            ssh_run(host, user, pw,
                f"vgchange -an {shlex.quote(vg)} 2>/dev/null; "
                f"vgremove -f {shlex.quote(vg)} 2>/dev/null || true",
                timeout=20)
        except Exception as e:
            ok, errmsg = False, str(e)
        items.append({"type": "nasnap_vg", "target": vg, "ok": ok, "error": errmsg})

    # Orphaned NBD devices: kill server process (via sysfs pid) then disconnect kernel driver
    for dev in orphan_nbds:
        ok, errmsg = True, ""
        try:
            dev_q = shlex.quote(dev)
            ssh_run(host, user, pw,
                f"base=$(basename {dev_q}); "
                f"pid=$(cat /sys/block/$base/pid 2>/dev/null); "
                f"[ -n \"$pid\" ] && kill \"$pid\" 2>/dev/null; "
                f"sleep 0.5; "
                f"qemu-nbd --disconnect {dev_q} 2>/dev/null; true",
                timeout=20)
        except Exception as e:
            ok, errmsg = False, str(e)
        items.append({"type": "orphan_nbd", "target": dev, "ok": ok, "error": errmsg})

    # Orphaned NVMe subsystems: disconnect by NQN
    for nqn in orphan_nvme_subsys:
        ok, errmsg = True, ""
        try:
            ssh_run(host, user, pw,
                f"timeout 10 nvme disconnect -n {shlex.quote(nqn)} 2>/dev/null || true",
                timeout=20)
        except Exception as e:
            ok, errmsg = False, str(e)
        items.append({"type": "orphan_nvme_subsys", "target": nqn, "ok": ok, "error": errmsg})

    return {"results": items}


def _ensure_ldap_row(db):
    if not db.query_one("SELECT id FROM np_ldap_config WHERE id='default'"):
        db.execute("INSERT INTO np_ldap_config (id, updated_at) VALUES ('default', ?)",
                   (datetime.now(timezone.utc).isoformat(),))


def _ldap_get():
    db = get_db()
    _ensure_ldap_row(db)
    row = db.query_one("SELECT * FROM np_ldap_config WHERE id='default'")
    d = dict(row)
    return jsonify({
        'enabled':         bool(d.get('enabled', 0)),
        'server':          d.get('server', ''),
        'port':            d.get('port', 389),
        'use_ssl':         bool(d.get('use_ssl', 0)),
        'use_tls':         bool(d.get('use_tls', 1)),
        'bind_dn':         d.get('bind_dn', ''),
        'base_dn':         d.get('base_dn', ''),
        'user_filter':     d.get('user_filter', '(sAMAccountName={username})'),
        'admin_group_dn':  d.get('admin_group_dn', ''),
        'viewer_group_dn': d.get('viewer_group_dn', ''),
        'has_password':    bool(d.get('bind_password_enc', '')),
    })


def _ldap_save():
    err = _require_admin()
    if err:
        return err
    db = get_db()
    _ensure_ldap_row(db)
    data = request.get_json() or {}
    now = datetime.now(timezone.utc).isoformat()

    enabled      = 1 if data.get('enabled') else 0
    server       = data.get('server', '').strip()
    port         = int(data.get('port') or 389)
    use_ssl      = 1 if data.get('use_ssl') else 0
    use_tls      = 1 if data.get('use_tls') else 0
    bind_dn      = data.get('bind_dn', '').strip()
    base_dn      = data.get('base_dn', '').strip()
    user_filter  = data.get('user_filter', '').strip() or '(sAMAccountName={username})'
    admin_grp    = data.get('admin_group_dn', '').strip()
    viewer_grp   = data.get('viewer_group_dn', '').strip()

    if data.get('bind_password'):
        pw_enc = db._encrypt(data['bind_password'])
        db.execute(
            "UPDATE np_ldap_config SET enabled=?,server=?,port=?,use_ssl=?,use_tls=?,"
            "bind_dn=?,bind_password_enc=?,base_dn=?,user_filter=?,admin_group_dn=?,"
            "viewer_group_dn=?,updated_at=? WHERE id='default'",
            (enabled, server, port, use_ssl, use_tls, bind_dn, pw_enc, base_dn,
             user_filter, admin_grp, viewer_grp, now),
        )
    else:
        db.execute(
            "UPDATE np_ldap_config SET enabled=?,server=?,port=?,use_ssl=?,use_tls=?,"
            "bind_dn=?,base_dn=?,user_filter=?,admin_group_dn=?,viewer_group_dn=?,"
            "updated_at=? WHERE id='default'",
            (enabled, server, port, use_ssl, use_tls, bind_dn, base_dn,
             user_filter, admin_grp, viewer_grp, now),
        )
    log.info("[netapp_storage] LDAP config saved")
    return jsonify({'success': True})


def _ldap_test():
    err = _require_admin()
    if err:
        return err

    data = request.get_json() or {}
    db = get_db()
    _ensure_ldap_row(db)
    saved = dict(db.query_one("SELECT * FROM np_ldap_config WHERE id='default'") or {})

    server      = (data.get('server') or saved.get('server') or '').strip()
    port        = int(data.get('port') or saved.get('port') or 389)
    use_ssl     = bool(data.get('use_ssl') if 'use_ssl' in data else saved.get('use_ssl', False))
    use_tls     = bool(data.get('use_tls') if 'use_tls' in data else saved.get('use_tls', True))
    bind_dn     = (data.get('bind_dn') or saved.get('bind_dn') or '').strip()
    base_dn     = (data.get('base_dn') or saved.get('base_dn') or '').strip()
    user_filter = (data.get('user_filter') or saved.get('user_filter') or '(sAMAccountName={username})').strip()
    test_user   = (data.get('test_username') or '').strip()

    if data.get('bind_password'):
        bind_pw = data['bind_password']
    elif saved.get('bind_password_enc'):
        try:
            bind_pw = db._decrypt(saved['bind_password_enc'])
        except Exception:
            bind_pw = ''
    else:
        bind_pw = ''

    if not server:
        return jsonify({'success': False, 'error': 'LDAP server not configured'})

    try:
        import ldap3
        import ldap3.utils.conv
    except ImportError:
        return jsonify({'success': False, 'error': 'ldap3 library not installed in container'})

    try:
        srv = ldap3.Server(host=server, port=port, use_ssl=use_ssl,
                           connect_timeout=10, get_info=ldap3.ALL)
        conn = ldap3.Connection(srv, user=bind_dn, password=bind_pw,
                                authentication=ldap3.SIMPLE, auto_bind=False)
        conn.open()
        if use_tls and not use_ssl:
            conn.start_tls()
        if not conn.bind():
            conn.unbind()
            desc = conn.result.get('description', 'unknown') if conn.result else 'unknown'
            return jsonify({'success': False, 'error': f'Service account bind failed: {desc}'})

        result = {'success': True}
        if srv.info and srv.info.vendor_name:
            result['server_info'] = str(srv.info.vendor_name)

        if test_user and base_dn:
            safe_u = ldap3.utils.conv.escape_filter_chars(test_user)
            uf = user_filter.replace('{username}', safe_u)
            conn.search(base_dn, uf, attributes=['distinguishedName', 'memberOf', 'displayName'])
            if conn.entries:
                entry = conn.entries[0]
                groups = [str(m) for m in entry.memberOf] if hasattr(entry, 'memberOf') and entry.memberOf else []
                dn_attr = str(entry.displayName) if hasattr(entry, 'displayName') and entry.displayName else ''
                result['user_found'] = True
                result['user_dn'] = entry.entry_dn
                result['display_name'] = dn_attr
                result['groups'] = groups
            else:
                result['user_found'] = False

        conn.unbind()
        return jsonify(result)

    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)})


def _job_retention_get():
    db = get_db()
    row = db.query_one("SELECT job_log_retention_days FROM netapp_plugin_config WHERE id='default'")
    days = int((row or {}).get("job_log_retention_days") or 90)
    return {"job_log_retention_days": days}


def _job_retention_save():
    err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        days = int(data.get("job_log_retention_days", 90))
    except (ValueError, TypeError):
        return {"error": "Invalid value"}, 400
    if days < 0:
        return {"error": "Value must be 0 or greater"}, 400
    db = get_db()
    db.execute(
        "UPDATE netapp_plugin_config SET job_log_retention_days=? WHERE id='default'",
        (days,),
    )
    log.info(f"[netapp_storage] Job log retention set to {days} days.")
    return {"success": True, "job_log_retention_days": days}


def register_routes():
    register_plugin_route(PLUGIN_ID, 'settings/smtp',              _smtp_get)
    register_plugin_route(PLUGIN_ID, 'settings/smtp/save',         _smtp_save)
    register_plugin_route(PLUGIN_ID, 'settings/smtp/test',         _smtp_test)
    register_plugin_route(PLUGIN_ID, 'settings/notify-test',       _notify_test)
    register_plugin_route(PLUGIN_ID, 'settings/export',            _db_export)
    register_plugin_route(PLUGIN_ID, 'settings/import',            _db_import)
    register_plugin_route(PLUGIN_ID, 'settings/update/info',       _update_info)
    register_plugin_route(PLUGIN_ID, 'settings/update/apply',      _update_apply)
    register_plugin_route(PLUGIN_ID, 'settings/db-backup-config',         _backup_config_get)
    register_plugin_route(PLUGIN_ID, 'settings/db-backup-save',           _backup_config_save)
    register_plugin_route(PLUGIN_ID, 'settings/db-backup-run-now',        _backup_run_now)
    register_plugin_route(PLUGIN_ID, 'settings/db-backup-list',           _backup_list_remote)
    register_plugin_route(PLUGIN_ID, 'settings/db-backup-restore-remote', _backup_restore_from_remote)
    register_plugin_route(PLUGIN_ID, 'settings/pve-health',        _pve_health_check)
    register_plugin_route(PLUGIN_ID, 'settings/pve-cleanup',       _pve_cleanup)
    register_plugin_route(PLUGIN_ID, 'settings/pve-stack-fix',     _pve_stack_fix)
    register_plugin_route(PLUGIN_ID, 'settings/ldap',              _ldap_get)
    register_plugin_route(PLUGIN_ID, 'settings/ldap/save',         _ldap_save)
    register_plugin_route(PLUGIN_ID, 'settings/ldap/test',         _ldap_test)
    register_plugin_route(PLUGIN_ID, 'settings/job-retention',      _job_retention_get)
    register_plugin_route(PLUGIN_ID, 'settings/job-retention/save', _job_retention_save)
