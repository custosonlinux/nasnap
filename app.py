import os
import sys
import logging

# pegaprox_compat must be importable before the plugin loads
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, jsonify, send_file, g

from db import get_db
from auth import (
    verify_password, create_session, delete_session,
    ensure_default_admin, get_session, ROLE_ADMIN,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)

_HERE       = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.join(_HERE, 'plugins', 'netapp_storage')
_UI_FILE    = os.path.join(_PLUGIN_DIR, 'ui.html')
_LOGIN_FILE = os.path.join(_HERE, 'login.html')


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get('SECRET_KEY', os.urandom(32).hex())

    # ── Custom Request: add .session for plugin compat ────────────────
    from flask import Request as _FlaskRequest

    class NaSnapRequest(_FlaskRequest):
        @property
        def session(self):
            return getattr(g, '_nasnap_session', {})

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

    # ── Plugin registration ───────────────────────────────────────────
    sys.path.insert(0, os.path.join(_HERE, 'plugins'))
    import netapp_storage
    netapp_storage.register(app)

    from pegaprox.api.plugins import get_all_routes, make_view
    for path, handler in get_all_routes('netapp_storage').items():
        route    = f'/api/plugins/netapp_storage/api/{path}'
        endpoint = 'plugin_netapp_' + path.replace('/', '_').replace('-', '__')
        app.add_url_rule(
            route, endpoint=endpoint,
            view_func=make_view(handler),
            methods=['GET', 'POST', 'DELETE', 'PUT'],
        )

    # ── UI ────────────────────────────────────────────────────────────
    @app.route('/')
    def _index():
        return send_file(_UI_FILE, mimetype='text/html')

    @app.route('/login')
    def _login_page():
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
