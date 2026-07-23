"""
Periodic digest/report configuration and data gathering.

  settings/report          GET   – load report config
  settings/report/save     POST  – save report config
  settings/report/test     POST  – send a preview report immediately (doesn't touch last_run_at)

The actual email building/sending lives in settings.py (send_digest_report), next to
the other notification builders, to reuse _send_smtp()/the HTML color conventions.
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nasnap_core.core.db import get_db
from nasnap_core.api.plugins import register_plugin_route
from ..core._helpers import PLUGIN_ID

log = logging.getLogger(__name__)


def _require_admin():
    from flask import request
    if request.session.get("role") != "admin":
        return {"error": "Admin access required"}, 403
    return None


def _ensure_report_config_row(db):
    if not db.query_one("SELECT id FROM netapp_report_config WHERE id='default'"):
        db.execute("INSERT INTO netapp_report_config (id) VALUES ('default')")


def _report_config_get():
    from flask import jsonify
    err = _require_admin()
    if err:
        return err
    db = get_db()
    _ensure_report_config_row(db)
    row = dict(db.query_one("SELECT * FROM netapp_report_config WHERE id='default'"))
    return jsonify({
        'enabled':      bool(row.get('enabled')),
        'mode':         row.get('mode', 'daily'),
        'time_of_day':  row.get('time_of_day', '07:00'),
        'day_of_week':  int(row.get('day_of_week') or 0),
        'recipients':   row.get('recipients', ''),
        'warn_pct':     int(row.get('warn_pct') or 80),
        'critical_pct': int(row.get('critical_pct') or 95),
        'last_run_at':  row.get('last_run_at', ''),
    })


def _report_config_save():
    from flask import request, jsonify
    import re
    err = _require_admin()
    if err:
        return err
    data = request.get_json() or {}
    db = get_db()
    _ensure_report_config_row(db)

    mode = data.get('mode', 'daily')
    if mode not in ('daily', 'weekly'):
        return jsonify({'error': 'Invalid mode'}), 400

    time_of_day = (data.get('time_of_day') or '07:00').strip()
    if not re.match(r'^\d{2}:\d{2}$', time_of_day):
        return jsonify({'error': 'Invalid time_of_day, expected HH:MM'}), 400

    try:
        day_of_week = int(data.get('day_of_week') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid day_of_week'}), 400
    if not 0 <= day_of_week <= 6:
        return jsonify({'error': 'day_of_week must be 0-6'}), 400

    try:
        warn_pct     = int(data.get('warn_pct') or 80)
        critical_pct = int(data.get('critical_pct') or 95)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid threshold value'}), 400
    if not (1 <= warn_pct <= 100 and 1 <= critical_pct <= 100):
        return jsonify({'error': 'Thresholds must be between 1 and 100'}), 400
    if warn_pct >= critical_pct:
        return jsonify({'error': 'Warning threshold must be lower than critical threshold'}), 400

    db.execute(
        "UPDATE netapp_report_config SET enabled=?,mode=?,time_of_day=?,day_of_week=?,"
        "recipients=?,warn_pct=?,critical_pct=?,updated_at=? WHERE id='default'",
        (1 if data.get('enabled') else 0, mode, time_of_day, day_of_week,
         (data.get('recipients') or '').strip(), warn_pct, critical_pct,
         datetime.now(timezone.utc).isoformat()),
    )
    log.info("[netapp_storage] Report config saved")
    return jsonify({'success': True})


def _classify_capacity(stats, warn_pct, critical_pct):
    """Classify each datastore's used percentage as ok/warn/critical."""
    out = []
    for storage_id, s in (stats or {}).items():
        total = s.get('total_bytes') or 0
        used  = s.get('used_bytes') or 0
        if total <= 0:
            continue
        pct = used / total * 100
        if pct >= critical_pct:
            level = 'critical'
        elif pct >= warn_pct:
            level = 'warn'
        else:
            level = 'ok'
        out.append({'storage_id': storage_id, 'used_pct': pct, 'level': level,
                     'used_bytes': used, 'total_bytes': total})
    out.sort(key=lambda x: x['used_pct'], reverse=True)
    return out


def _build_digest_data(db, period_start_iso, period_end_iso, warn_pct, critical_pct):
    """Pure data-gathering for the digest report — no HTML built here."""
    from ..core._helpers import count_snapshots_in_range
    from .provisioning import _do_fetch_capacity

    snap_total  = count_snapshots_in_range(db, period_start_iso, period_end_iso)
    snap_done   = count_snapshots_in_range(db, period_start_iso, period_end_iso, status_filter='done')
    snap_failed = count_snapshots_in_range(db, period_start_iso, period_end_iso, status_filter=('failed', 'error'))

    plan_rows = db.query(
        "SELECT s.name AS schedule_name, s.id AS schedule_id, "
        "SUM(CASE WHEN ns.status='done' THEN 1 ELSE 0 END) AS ok_count, "
        "SUM(CASE WHEN ns.status IN ('failed','error') THEN 1 ELSE 0 END) AS fail_count "
        "FROM netapp_snapshots ns JOIN netapp_snapshot_schedules s ON s.id = ns.schedule_id "
        "WHERE ns.created_at >= ? AND ns.created_at < ? AND ns.schedule_id != '' "
        "GROUP BY s.id ORDER BY fail_count DESC, s.name",
        (period_start_iso, period_end_iso),
    ) or []

    plans = []
    for r in plan_rows:
        d = dict(r)
        failures = []
        if d.get('fail_count'):
            fail_rows = db.query(
                "SELECT snap_name, error, created_at FROM netapp_snapshots "
                "WHERE schedule_id=? AND status IN ('failed','error') "
                "AND created_at >= ? AND created_at < ? ORDER BY created_at DESC LIMIT 5",
                (d['schedule_id'], period_start_iso, period_end_iso),
            ) or []
            failures = [dict(fr) for fr in fail_rows]
        plans.append({
            'schedule_name': d.get('schedule_name', ''),
            'ok_count':      int(d.get('ok_count') or 0),
            'fail_count':    int(d.get('fail_count') or 0),
            'failures':      failures,
        })

    sm_total   = dict(db.query_one("SELECT COUNT(*) AS c FROM netapp_snapmirror_relationships") or {}).get('c', 0)
    sm_healthy = dict(db.query_one("SELECT COUNT(*) AS c FROM netapp_snapmirror_relationships WHERE healthy=1 AND state='snapmirrored'") or {}).get('c', 0)

    cap_result   = _do_fetch_capacity()
    capacity     = _classify_capacity(cap_result.get('stats', {}), warn_pct, critical_pct)

    return {
        'snap_total': snap_total, 'snap_done': snap_done, 'snap_failed': snap_failed,
        'plans': plans,
        'sm_total': int(sm_total), 'sm_healthy': int(sm_healthy),
        'capacity': capacity,
    }


def _check_and_run_report():
    """Called from the report scheduler thread every minute."""
    try:
        from .settings import send_digest_report
        db = get_db()
        row = db.query_one("SELECT * FROM netapp_report_config WHERE id='default'")
        if not row:
            return
        cfg = dict(row)
        if not cfg.get('enabled') or not cfg.get('recipients'):
            return

        try:
            tz = ZoneInfo(os.environ.get("TZ", "UTC"))
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        try:
            hh, mm = (cfg.get('time_of_day') or '07:00').split(':')
            slot_today = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except Exception:
            return
        if now < slot_today:
            return
        if cfg.get('mode') == 'weekly' and now.weekday() != int(cfg.get('day_of_week') or 0):
            return

        # last_run_at is stored as a UTC-aware timestamp (send_digest_report() in
        # settings.py, matches how the UI labels/displays it) — must be converted
        # into the same tz as slot_today before comparing, not string-stripped.
        last_run = cfg.get('last_run_at', '')
        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run.replace('Z', '+00:00'))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if last_dt.astimezone(tz) >= slot_today:
                    return  # already sent for this slot
            except Exception:
                pass

        period_end   = datetime.now(timezone.utc)
        period_start = period_end - (timedelta(days=7) if cfg.get('mode') == 'weekly' else timedelta(days=1))
        send_digest_report(db, cfg, period_start.isoformat(), period_end.isoformat())
    except Exception as exc:
        log.warning(f"[netapp_storage] Report scheduler error: {exc}")


def start_report_scheduler():
    import threading
    import time

    def _loop():
        while True:
            time.sleep(60)
            _check_and_run_report()
    threading.Thread(target=_loop, daemon=True, name="nasnap-report").start()
    log.info("[netapp_storage] Report scheduler started")


def _report_test():
    from flask import jsonify
    from .settings import send_digest_report
    err = _require_admin()
    if err:
        return err
    db = get_db()
    _ensure_report_config_row(db)
    row = dict(db.query_one("SELECT * FROM netapp_report_config WHERE id='default'"))

    recipients = row.get('recipients', '')
    if not recipients:
        return jsonify({'success': False, 'error': 'No recipients configured — save the recipient list first'})

    period_end   = datetime.now(timezone.utc)
    period_start = period_end - (timedelta(days=7) if row.get('mode') == 'weekly' else timedelta(days=1))
    try:
        send_digest_report(db, row, period_start.isoformat(), period_end.isoformat(), is_test=True)
        return jsonify({'success': True})
    except Exception as exc:
        log.warning(f"[netapp_storage] Test report failed: {exc}")
        return jsonify({'success': False, 'error': str(exc)})


def register_routes():
    register_plugin_route(PLUGIN_ID, 'settings/report',      _report_config_get)
    register_plugin_route(PLUGIN_ID, 'settings/report/save', _report_config_save)
    register_plugin_route(PLUGIN_ID, 'settings/report/test', _report_test)
