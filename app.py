import os
import sys
import json as _json
import logging
from datetime import datetime, timezone

# nasnap_core must be importable before the plugin loads
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, jsonify, send_file, redirect, g, Response

from db import get_db
from auth import (
    verify_password, create_session, delete_session,
    ensure_default_admin, get_session, ROLE_ADMIN,
    list_users, create_user, delete_user, change_password,
    require_admin, require_auth,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)

NASNAP_VERSION = '1.0.0'
_START_TIME    = datetime.now(timezone.utc)

_HERE          = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR    = os.path.join(_HERE, 'plugins', 'netapp_storage')
_UI_FILE       = os.path.join(_PLUGIN_DIR, 'ui.html')
_LOGIN_FILE    = os.path.join(_HERE, 'login.html')
_ADMIN_FILE    = os.path.join(_HERE, 'admin.html')
_SETTINGS_FILE = os.path.join(_HERE, 'settings.html')


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get('SECRET_KEY', os.urandom(32).hex())

    # ── Custom Request: add .session for plugin compat ────────────────
    from flask import Request as _FlaskRequest

    class NaSnapRequest(_FlaskRequest):
        @property
        def session(self):
            sess = getattr(g, '_nasnap_session', {}) or {}
            # Plugin uses "user" key; NaSnap session stores "username"
            if 'username' in sess and 'user' not in sess:
                sess = dict(sess, user=sess['username'])
            return sess

    app.request_class = NaSnapRequest

    # ── Session middleware ────────────────────────────────────────────
    @app.before_request
    def _load_session():
        token = request.cookies.get('nasnap_session')
        g._nasnap_session = get_session(token) if token else {}

    # ── Auth API ──────────────────────────────────────────────────────
    @app.route('/api/auth/login', methods=['POST'])
    def _login():
        data = request.get_json() or {}
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        if not username or not password:
            return jsonify({'error': 'Username and password required'}), 400
        row = get_db().query_one(
            "SELECT password_hash, role FROM np_users WHERE username=?", (username,)
        )
        if not row or not verify_password(row['password_hash'], password):
            return jsonify({'error': 'Invalid credentials'}), 401
        token = create_session(username)
        resp = jsonify({'ok': True, 'username': username, 'role': row['role']})
        resp.set_cookie('nasnap_session', token, httponly=True, samesite='Lax', max_age=8 * 3600)
        return resp

    @app.route('/api/auth/logout', methods=['POST'])
    def _logout():
        token = request.cookies.get('nasnap_session')
        if token:
            delete_session(token)
        resp = jsonify({'ok': True})
        resp.delete_cookie('nasnap_session')
        return resp

    @app.route('/api/auth/me')
    def _me():
        sess = getattr(g, '_nasnap_session', {})
        if not sess:
            return jsonify({'authenticated': False}), 401
        return jsonify({'authenticated': True, 'username': sess.get('username'), 'role': sess.get('role', ROLE_ADMIN)})

    # ── User management API (admin only) ─────────────────────────────
    @app.route('/api/auth/users')
    @require_admin
    def _users_list():
        return jsonify(list_users())

    @app.route('/api/auth/users', methods=['POST'])
    @require_admin
    def _users_create():
        data = request.get_json() or {}
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        role = data.get('role', 'viewer')
        if not username or not password:
            return jsonify({'error': 'Username and password required'}), 400
        if len(password) < 6:
            return jsonify({'error': 'Password must be at least 6 characters'}), 400
        if role not in ('admin', 'viewer'):
            return jsonify({'error': 'Invalid role'}), 400
        try:
            create_user(username, password, role)
        except Exception as e:
            if 'UNIQUE' in str(e):
                return jsonify({'error': 'Username already exists'}), 409
            raise
        return jsonify({'ok': True})

    @app.route('/api/auth/users/<username>', methods=['DELETE'])
    @require_admin
    def _users_delete(username):
        sess = getattr(g, '_nasnap_session', {})
        if username == sess.get('username'):
            return jsonify({'error': 'Cannot delete your own account'}), 400
        delete_user(username)
        return jsonify({'ok': True})

    @app.route('/api/auth/users/<username>/password', methods=['POST'])
    @require_admin
    def _users_change_password(username):
        data = request.get_json() or {}
        password = data.get('password') or ''
        if len(password) < 6:
            return jsonify({'error': 'Password must be at least 6 characters'}), 400
        change_password(username, password)
        return jsonify({'ok': True})

    # ── Admin UI ──────────────────────────────────────────────────────
    @app.route('/admin')
    def _admin():
        sess = getattr(g, '_nasnap_session', {})
        if not sess:
            return redirect('/login')
        row = get_db().query_one("SELECT role FROM np_users WHERE username=?", (sess.get('username'),))
        if not row or row['role'] != ROLE_ADMIN:
            return redirect('/')
        return send_file(_ADMIN_FILE, mimetype='text/html')

    # ── Change own password ───────────────────────────────────────────
    @app.route('/api/auth/me/password', methods=['POST'])
    @require_auth
    def _me_change_password():
        sess = getattr(g, '_nasnap_session', {})
        data = request.get_json() or {}
        current = data.get('current_password') or ''
        new_pw  = data.get('new_password') or ''
        if len(new_pw) < 6:
            return jsonify({'error': 'New password must be at least 6 characters'}), 400
        row = get_db().query_one(
            "SELECT password_hash FROM np_users WHERE username=?", (sess['username'],)
        )
        if not row or not verify_password(row['password_hash'], current):
            return jsonify({'error': 'Current password is incorrect'}), 401
        change_password(sess['username'], new_pw)
        return jsonify({'ok': True})

    # ── System info ───────────────────────────────────────────────────
    @app.route('/api/system/info')
    @require_auth
    def _system_info():
        import platform, sys as _sys
        from db import DB_FILE
        sess  = getattr(g, '_nasnap_session', {})
        urow  = get_db().query_one(
            "SELECT role, created_at FROM np_users WHERE username=?", (sess['username'],)
        )
        info = {
            'version':    NASNAP_VERSION,
            'username':   sess.get('username'),
            'role':       urow['role']       if urow else 'viewer',
            'created_at': urow['created_at'] if urow else '',
        }
        if urow and urow['role'] == ROLE_ADMIN:
            uptime_s = int((datetime.now(timezone.utc) - _START_TIME).total_seconds())
            h, rem   = divmod(uptime_s, 3600)
            m        = rem // 60
            db_size  = os.path.getsize(DB_FILE) if os.path.exists(DB_FILE) else 0
            tables   = get_db().query(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            table_stats = []
            for t in tables:
                cnt = get_db().query_one(f"SELECT COUNT(*) AS c FROM \"{t['name']}\"")
                table_stats.append({'table': t['name'], 'rows': cnt['c'] if cnt else 0})
            info.update({
                'started_at':     _START_TIME.isoformat(),
                'uptime':         f'{h}h {m:02d}m',
                'python_version': _sys.version.split()[0],
                'platform':       platform.system() + ' ' + platform.release(),
                'data_dir':       os.environ.get('NASNAP_DATA', os.path.dirname(DB_FILE)),
                'debug':          os.environ.get('DEBUG', '').lower() in ('1', 'true', 'yes'),
                'db_path':        DB_FILE,
                'db_size_bytes':  db_size,
                'tables':         table_stats,
            })
        return jsonify(info)

    # ── Settings UI ───────────────────────────────────────────────────
    @app.route('/settings')
    def _settings():
        if not getattr(g, '_nasnap_session', {}):
            return redirect('/login')
        return send_file(_SETTINGS_FILE, mimetype='text/html')

    # ── Plugin registration ───────────────────────────────────────────
    sys.path.insert(0, os.path.join(_HERE, 'plugins'))
    import netapp_storage
    netapp_storage.register(app)

    # These routes are replaced by NaSnap-specific versions below.
    # settings/update/* references the plugin GitHub repo — not applicable here.
    _NS_ROUTE_SKIP = {
        'settings/export',
        'settings/import',
        'settings/update/info',
        'settings/update/apply',
    }

    from nasnap_core.api.plugins import get_all_routes, make_view
    for path, handler in get_all_routes('netapp_storage').items():
        if path in _NS_ROUTE_SKIP:
            continue
        route    = f'/api/plugins/netapp_storage/api/{path}'
        endpoint = 'plugin_netapp_' + path.replace('/', '_').replace('-', '__')
        app.add_url_rule(
            route, endpoint=endpoint,
            view_func=make_view(handler),
            methods=['GET', 'POST', 'DELETE', 'PUT'],
        )

    # ── DB Export / Import (NaSnap-extended) ─────────────────────────
    # Extends the plugin's export with np_users so a backup file is
    # sufficient to fully restore a NaSnap installation on a new server.
    from netapp_storage.api.settings import build_export_payload, apply_import_payload

    @app.route('/api/plugins/netapp_storage/api/settings/export')
    @require_admin
    def _ns_db_export():
        payload = build_export_payload()
        payload['nasnap'] = {
            'np_users': [dict(r) for r in get_db().query(
                "SELECT username, password_hash, role, created_at FROM np_users"
            )]
        }
        ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        return Response(
            _json.dumps(payload, indent=2, ensure_ascii=False),
            mimetype='application/json',
            headers={'Content-Disposition': f'attachment; filename="nasnap_backup_{ts}.json"'},
        )

    @app.route('/api/plugins/netapp_storage/api/settings/import', methods=['POST'])
    @require_admin
    def _ns_db_import():
        try:
            if request.content_type and 'multipart' in request.content_type:
                f = request.files.get('file')
                if not f:
                    return jsonify({'error': 'No file uploaded'}), 400
                payload = _json.load(f)
            else:
                payload = request.get_json(force=True) or {}
        except Exception as exc:
            return jsonify({'error': f'Invalid JSON: {exc}'}), 400

        try:
            result = apply_import_payload(payload)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400

        # Restore NaSnap users if present in the backup
        user_count = 0
        for u in payload.get('nasnap', {}).get('np_users', []):
            try:
                get_db().execute(
                    "INSERT OR REPLACE INTO np_users "
                    "(username, password_hash, role, created_at) VALUES (?,?,?,?)",
                    (u['username'], u['password_hash'],
                     u.get('role', 'admin'), u.get('created_at', '')),
                )
                user_count += 1
            except Exception as exc:
                logging.warning(f"[nasnap] import: skipped user {u.get('username')}: {exc}")
        result['np_users_imported'] = user_count

        return jsonify(result)

    # ── Debug: auto-login (only in DEBUG mode) ───────────────────────
    if os.environ.get('DEBUG', '').lower() in ('1', 'true', 'yes'):
        @app.route('/dev/autologin')
        def _dev_autologin():
            token = create_session('admin')
            resp = redirect('/')
            resp.set_cookie('nasnap_session', token, httponly=True, samesite='Lax', max_age=3600)
            return resp

    # ── UI ────────────────────────────────────────────────────────────
    # ── UI patches: hide PegaProx-specific sections ───────────────────
    # Deploy Wizard  — only relevant when deploying into PegaProx
    # Plugin Update  — updates happen via ./build-docker.sh in NaSnap
    _UI_PATCHES = [
        # Deploy Wizard: remove the sub-header block (5 lines, unique SVG path)
        (
            '<!-- ═══════════════════════ DEPLOY WIZARD ═══════════════════════ -->\n'
            '      <div class="sub-header">\n'
            '        <span class="sub-header-title"><svg class="tab-icon" width="14" height="14"'
            ' fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round"'
            ' stroke-linejoin="round" stroke-width="2" d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286'
            ' 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z"/></svg>'
            'Deploy Wizard</span>\n'
            '        <button class="btn btn-primary btn-sm" onclick="setupWizardOpen()">'
            '+ Initial Setup</button>\n'
            '      </div>',
            '',
        ),
        # Plugin Update: wrap section in display:none (start marker)
        (
            '      <!-- ═══════════════ PLUGIN UPDATE ═══════════════ -->',
            '      <div style="display:none"><!-- ═══════════════ PLUGIN UPDATE ═══════════════ -->',
        ),
        # Plugin Update: close the display:none wrapper after the card's closing </div>
        # and rewrite the stale "restart PegaProx" hint to something accurate
        (
            'After applying an update, restart PegaProx to activate the new version.\n'
            '          Your configuration (<code>config.json</code>) and database are never overwritten.\n'
            '        </p>\n'
            '      </div>',
            'To update NaSnap: <code>./build-docker.sh &amp;&amp; docker compose up -d</code>\n'
            '        </p>\n'
            '      </div></div>',
        ),
        # Data Backup description: mention NaSnap user accounts are included
        (
            'Export plugin configuration (endpoints, PVE hosts, volume mappings, schedules,'
            ' provisioned datastores, SMTP config) as a JSON file.',
            'Export full NaSnap configuration (endpoints, PVE hosts, volume mappings, schedules,'
            ' provisioned datastores, SMTP config, and user accounts) as a JSON file.',
        ),
        # Export button label
        (
            '>Export Plugin Data</a>',
            '>Export NaSnap Backup</a>',
        ),
        # localStorage key for language preference
        (
            "localStorage.getItem('pegaprox-language')",
            "localStorage.getItem('nasnap-language')",
        ),
        # Default ONTAP account name in endpoint form (JS reset)
        ("document.getElementById('ep_user').value = 'pegaprox'",
         "document.getElementById('ep_user').value = 'nasnap'"),
        # Default ONTAP account name in endpoint form (JS fallback)
        (".value.trim() || 'pegaprox'", ".value.trim() || 'nasnap'"),
        # Default ONTAP account name in wizard input
        ('<input class="form-control" id="wiz_ep_user" value="pegaprox">',
         '<input class="form-control" id="wiz_ep_user" value="nasnap">'),
        # Default ONTAP account name in endpoint input
        ('<input id="ep_user" value="pegaprox">',
         '<input id="ep_user" value="nasnap">'),
        # Help text: ONTAP account suggestion
        ('create a dedicated <code>pegaprox</code> account on ONTAP and register',
         'create a dedicated <code>nasnap</code> account on ONTAP and register'),
    ]

    # Enterprise Blue palette — replaces the plugin's orange-dark defaults.
    # _THEME_SUBS patches both the static :root CSS block AND the JS runtime
    # theme system (which overrides CSS vars at page load when running standalone).
    _THEME_SUBS = [
        # Static :root block (lines 11-19 of ui.html)
        ('--bg:      #0F1419', '--bg:      #1A252F'),
        ('--card:    #161B22', '--card:    #243542'),
        ('--border:  #30363D', '--border:  #344955'),
        ('--hover:   #1C2128', '--hover:   #29414E'),
        ('--primary: #E57000', '--primary: #005EB8'),
        ('--text:    #E6EDF3', '--text:    #E9ECEF'),
        ('--muted:   #8B949E', '--muted:   #728B9A'),
        # JS runtime theme default (standalone fallback — no parent PegaProx frame)
        ("|| 'proxmoxDark'",           "|| 'enterpriseBlue'"),
        ("|| themes['proxmoxDark']",   "|| themes['enterpriseBlue']"),
    ]

    # Injected into ui.html: auth guard + logout button
    _AUTH_GUARD = """
<script>
(function () {
  /* intercept fetch globally — redirect to /login on 401 */
  var _fetch = window.fetch;
  window.fetch = function () {
    return _fetch.apply(this, arguments).then(function (r) {
      if (r.status === 401 &&
          !r.url.endsWith('/api/auth/me') &&
          !r.url.endsWith('/api/auth/login')) {
        window.location.replace('/login');
      }
      return r;
    });
  };

  window.nasnapLogout = function () {
    fetch('/api/auth/logout', { method: 'POST', credentials: 'include' })
      .finally(function () { window.location.replace('/login'); });
  };

  /* populate username in topbar; show admin link for admin role */
  document.addEventListener('DOMContentLoaded', function () {
    fetch('/api/auth/me', { credentials: 'include' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var el = document.getElementById('ns-username');
        if (el && d.username) el.textContent = d.username;
        if (d.role === 'admin') {
          var lnk = document.getElementById('ns-admin-link');
          if (lnk) lnk.style.display = 'flex';
        }
      });
  });
})();
</script>
"""

    # Logout button + username chip injected at the right end of .subtabs
    _LOGOUT_BTN = (
        '<div id="ns-userbar" style="margin-left:auto;display:flex;align-items:center;'
        'gap:8px;padding-bottom:4px;">'
        '<span id="ns-username" style="font-size:11px;color:var(--muted);'
        'white-space:nowrap;padding:0 4px;"></span>'
        # Settings link (all authenticated users)
        '<a href="/settings" title="Settings" '
        'style="display:flex;align-items:center;gap:5px;padding:5px 10px;'
        'font-size:11px;font-weight:500;color:var(--muted);background:none;'
        'border:1px solid var(--border);border-radius:6px;cursor:pointer;'
        'text-decoration:none;transition:color .15s,border-color .15s,background .15s;" '
        'onmouseover="this.style.color=\'var(--text)\';this.style.borderColor=\'var(--text)\';this.style.background=\'var(--hover)\'" '
        'onmouseout="this.style.color=\'var(--muted)\';this.style.borderColor=\'var(--border)\';this.style.background=\'none\'">'
        '<svg width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z"/>'
        '</svg>'
        '</a>'
        # Users admin link (hidden until /api/auth/me confirms admin role)
        '<a id="ns-admin-link" href="/admin" title="User Management" '
        'style="display:none;align-items:center;gap:5px;padding:5px 10px;'
        'font-size:11px;font-weight:500;color:var(--muted);background:none;'
        'border:1px solid var(--border);border-radius:6px;cursor:pointer;'
        'text-decoration:none;transition:color .15s,border-color .15s,background .15s;" '
        'onmouseover="this.style.color=\'var(--text)\';this.style.borderColor=\'var(--text)\';this.style.background=\'var(--hover)\'" '
        'onmouseout="this.style.color=\'var(--muted)\';this.style.borderColor=\'var(--border)\';this.style.background=\'none\'">'
        '<svg width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/>'
        '<circle cx="9" cy="7" r="4"/>'
        '<path d="M23 21v-2a4 4 0 00-3-3.87"/>'
        '<path d="M16 3.13a4 4 0 010 7.75"/>'
        '</svg>'
        'Users'
        '</a>'
        '<button onclick="nasnapLogout()" title="Sign out" '
        'style="display:flex;align-items:center;gap:5px;padding:5px 10px;'
        'font-size:11px;font-weight:500;color:var(--muted);background:none;'
        'border:1px solid var(--border);border-radius:6px;cursor:pointer;'
        'transition:color .15s,border-color .15s,background .15s;white-space:nowrap;" '
        'onmouseover="this.style.color=\'var(--text)\';this.style.borderColor=\'var(--text)\';this.style.background=\'var(--hover)\'" '
        'onmouseout="this.style.color=\'var(--muted)\';this.style.borderColor=\'var(--border)\';this.style.background=\'none\'">'
        '<svg width="12" height="12" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/>'
        '<polyline points="16 17 21 12 16 7"/>'
        '<line x1="21" y1="12" x2="9" y2="12"/>'
        '</svg>'
        'Sign out'
        '</button>'
        '</div>'
    )

    @app.route('/')
    def _index():
        sess = getattr(g, '_nasnap_session', {})
        if not sess:
            return redirect('/login')
        with open(_UI_FILE, 'r', encoding='utf-8') as f:
            html = f.read()
        # Hide PegaProx-specific UI sections (Deploy Wizard, Plugin Update)
        for old, new in _UI_PATCHES:
            html = html.replace(old, new)
        # Apply Enterprise Blue theme by replacing CSS variable values directly
        for old, new in _THEME_SUBS:
            html = html.replace(old, new)
        # Auth guard before any other JS
        html = html.replace('<head>', '<head>' + _AUTH_GUARD, 1)
        # Logout button at right end of .subtabs bar
        html = html.replace(
            '</div>\n\n  <!-- Scrollable content -->',
            _LOGOUT_BTN + '</div>\n\n  <!-- Scrollable content -->',
            1,
        )
        return Response(html, mimetype='text/html')

    @app.route('/login')
    def _login_page():
        # Already logged in → skip login page
        if getattr(g, '_nasnap_session', {}):
            return redirect('/')
        return send_file(_LOGIN_FILE, mimetype='text/html')

    # ── Startup ───────────────────────────────────────────────────────
    get_db()
    ensure_default_admin()

    return app


if __name__ == '__main__':
    application = create_app()
    application.run(
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000)),
        debug=os.environ.get('DEBUG', '').lower() in ('1', 'true', 'yes'),
    )
