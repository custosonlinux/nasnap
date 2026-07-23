"""
Shared HTML building blocks for NaSnap notification emails.

Used by _build_notification_email(), send_schedule_consolidated_notification(),
and send_digest_report() in settings.py to keep all "live" notification emails
(including the "Send test email" button) visually consistent: banner -> KPI
tiles / summary -> action-needed callout (only when something's wrong) ->
compact cards -> footer.
"""

GREEN = "#16a34a"
AMBER = "#d97706"
RED   = "#dc2626"
GRAY  = "#6b7280"

SEV_COLOR = {"err": "#f87171", "warn": "#fbbf24", "info": "#a3e4b0"}
SEV_TAG   = {"err": "[ERR] ", "warn": "[WARN]", "info": "[INFO]"}


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def log_severity(msg):
    """Classify a job log message as 'err', 'warn', or 'info'.

    Recognizes both log-tag conventions used across the codebase: "ERROR: …" /
    "WARNING …" (snapshot_engine.py / recovery_engine.py) and "[ERR] …" /
    "[WARN] …" (dr.py) — the bracket-tag style doesn't contain the substring
    "error"/"warn" fully ([err] is missing the "o"), so it needs its own check.
    """
    ml = msg.lower().lstrip()
    if ml.startswith("[err]") or ml.startswith("error:") or ml.startswith("err:") or "error" in ml[:12]:
        return "err"
    if ml.startswith("[warn]") or ml.startswith("warning:") or ml.startswith("warn:") or "warn" in ml[:12]:
        return "warn"
    return "info"


def log_entries(log_lines, limit=50):
    entries = []
    for entry in (log_lines or [])[-limit:]:
        ts  = entry.get('ts', '')[:19].replace('T', ' ')
        msg = entry.get('msg', str(entry))
        entries.append((ts, log_severity(msg), msg))
    return entries


def render_log_html(log_lines, limit=50):
    """Dark-terminal-style [ts] [SEV] msg lines, color-coded by severity."""
    entries = log_entries(log_lines, limit)
    if not entries:
        return '<div style="color:#6b7280;font-style:italic">No log entries.</div>'
    rows = ""
    for ts, sev, msg in entries:
        col = SEV_COLOR[sev]
        tag = SEV_TAG[sev]
        rows += (
            f'<div style="margin:1px 0">'
            f'<span style="color:#6b7280;user-select:none">{esc(ts)} </span>'
            f'<span style="color:{col};font-weight:700;user-select:none">{tag} </span>'
            f'<span style="color:{col if sev != "info" else "#d1fae5"}">{esc(msg)}</span>'
            f'</div>'
        )
    return rows


def render_log_plain(log_lines, limit=50):
    entries = log_entries(log_lines, limit)
    if not entries:
        return "    (no log entries)"
    return "\n".join(f"    {ts}  {SEV_TAG[sev]}  {msg}" for ts, sev, msg in entries)


def worst_log_snippets(log_lines, max_lines=3):
    """Pick the most relevant lines for an at-a-glance failure summary:
    err lines if any, else warn lines, else the last line."""
    entries = log_entries(log_lines, limit=200)
    err  = [msg for _, sev, msg in entries if sev == "err"]
    warn = [msg for _, sev, msg in entries if sev == "warn"]
    picked = err or warn or ([entries[-1][2]] if entries else [])
    return picked[-max_lines:]


def vm_badge(vm):
    vmid  = vm.get("vmid", "?")
    name  = vm.get("name", "")
    vtype = (vm.get("vm_type") or "qemu").upper()
    label = f"{vtype} {vmid}" + (f" — {name}" if name else "")
    bg    = "#1d4ed8" if vtype == "QEMU" else "#6d28d9"
    return (f'<span style="display:inline-block;background:{bg};color:#fff;'
            f'border-radius:4px;padding:1px 6px;font-size:11px;margin:1px 2px 1px 0">'
            f'{esc(label)}</span>')


def render_vm_cell(vm_list):
    if not vm_list:
        return '<span style="color:#9ca3af;font-size:11px">—</span>'
    return "".join(vm_badge(v) for v in vm_list)


def render_sm_pill(sm_info):
    if not sm_info or not sm_info.get("exists"):
        return '<span style="color:#9ca3af;font-size:11px">—</span>'
    col   = GREEN if sm_info.get("healthy") else RED
    state = sm_info.get("state", "unknown")
    trig  = " · triggered" if sm_info.get("triggered") else ""
    return f'<span style="color:{col};font-size:11px">● {esc(state)}{esc(trig)}</span>'


def render_banner(color, icon, title, subtitle):
    return (
        f'<div style="background:{color};border-radius:8px 8px 0 0;padding:22px 28px;color:#fff">'
        f'<div style="font-size:22px;font-weight:700">{icon}&nbsp; {esc(title)}</div>'
        f'<div style="font-size:13px;opacity:.85;margin-top:4px">{esc(subtitle)}</div>'
        f'</div>'
    )


def render_kpi_row(tiles):
    """tiles: list of (value, label, color|None). Rendered as a light strip of stat tiles
    directly under the banner, so the outcome reads before any table/card does."""
    tiles = [t for t in tiles if t is not None]
    if not tiles:
        return ""
    width_pct = 100.0 / len(tiles)
    cells = ""
    for value, label, color in tiles:
        col = color or "#111827"
        cells += (
            f'<td style="width:{width_pct:.2f}%;text-align:center;padding:14px 8px">'
            f'<div style="font-size:24px;font-weight:700;color:{col}">{esc(value)}</div>'
            f'<div style="font-size:11px;color:#6b7280;text-transform:uppercase;'
            f'letter-spacing:.04em;margin-top:2px">{esc(label)}</div>'
            f'</td>'
        )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f9fafb;border-left:1px solid #e5e7eb;border-right:1px solid #e5e7eb;'
        'border-bottom:1px solid #e5e7eb">'
        f'<tr>{cells}</tr></table>'
    )


def render_action_box(title, rows_html):
    """Red callout box for 'this needs a look' items — surfaced above everything
    else so failures never hide inside a longer table/card list."""
    return (
        '<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;'
        'padding:14px 16px;margin:0 0 18px">'
        f'<div style="font-size:12px;font-weight:700;color:{RED};text-transform:uppercase;'
        f'letter-spacing:.06em;margin-bottom:10px">⚠ {esc(title)}</div>'
        f'{rows_html}'
        '</div>'
    )


def render_action_item(title_html, detail_html=""):
    return (
        '<div style="margin-bottom:10px">'
        f'<div style="font-size:13px;color:#111827">{title_html}</div>'
        f'{f"<div style=\'font-family:monospace;font-size:12px;color:#b91c1c;margin-top:2px\'>{detail_html}</div>" if detail_html else ""}'
        '</div>'
    )


def render_card(border_color, header_html, body_html=""):
    """A single compact card — colored left border carries status, replacing a dense
    table row so each entry reads on its own instead of blending into a grid."""
    return (
        f'<div style="border-left:3px solid {border_color};background:#fff;'
        f'border-top:1px solid #f3f4f6;border-right:1px solid #f3f4f6;border-bottom:1px solid #f3f4f6;'
        f'border-radius:0 6px 6px 0;padding:10px 14px;margin-bottom:8px">'
        f'{header_html}{body_html}'
        f'</div>'
    )


def render_meter(pct, color):
    pct_clamped = max(0.0, min(100.0, pct))
    return (
        '<div style="background:#e5e7eb;border-radius:3px;height:6px;width:100%;'
        f'overflow:hidden;margin-top:6px"><div style="background:{color};height:6px;'
        f'width:{pct_clamped:.0f}%"></div></div>'
    )


def render_footer(note=""):
    return (
        '<div style="text-align:center;font-size:11px;color:#9ca3af;margin-top:14px">'
        f'NaSnap — NetApp ONTAP Snapshot Management for Proxmox{esc(note)}'
        '</div>'
    )


def wrap_shell(inner_html):
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif">
<div style="max-width:680px;margin:0 auto">
{inner_html}
</div>
</body></html>"""
