import os
import secrets
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import request, jsonify, g

try:
    from argon2 import PasswordHasher
    _ph = PasswordHasher()
    ARGON2_AVAILABLE = True
except ImportError:
    import hashlib
    ARGON2_AVAILABLE = False
    logging.warning("argon2-cffi not available — falling back to SHA-256 (install argon2-cffi for production)")

ROLE_ADMIN = 'admin'
SESSION_HOURS = int(os.environ.get('SESSION_HOURS', '8'))


def hash_password(password: str) -> str:
    if ARGON2_AVAILABLE:
        return _ph.hash(password)
    salt = secrets.token_hex(16)
    h = __import__('hashlib').sha256((salt + password).encode()).hexdigest()
    return f"sha256:{salt}:{h}"


def verify_password(stored: str, password: str) -> bool:
    if stored.startswith('$argon2'):
        try:
            return _ph.verify(stored, password)
        except Exception:
            return False
    if stored.startswith('sha256:'):
        _, salt, h = stored.split(':', 2)
        import hashlib
        return secrets.compare_digest(
            h, hashlib.sha256((salt + password).encode()).hexdigest()
        )
    return False


def create_session(username: str, role: str = ROLE_ADMIN) -> str:
    from db import get_db
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).isoformat()
    get_db().execute(
        "INSERT OR REPLACE INTO np_sessions (token, username, role, expires_at) VALUES (?,?,?,?)",
        (token, username, role, expires)
    )
    return token


def get_session(token: str) -> dict | None:
    from db import get_db
    if not token:
        return None
    row = get_db().query_one(
        "SELECT username, role, expires_at FROM np_sessions WHERE token=?", (token,)
    )
    if not row:
        return None
    row = dict(row)
    expires = datetime.fromisoformat(row['expires_at'])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        get_db().execute("DELETE FROM np_sessions WHERE token=?", (token,))
        return None
    return row


def delete_session(token: str):
    from db import get_db
    get_db().execute("DELETE FROM np_sessions WHERE token=?", (token,))


def load_users() -> dict:
    from db import get_db
    rows = get_db().query("SELECT username, role FROM np_users")
    return {r['username']: {'role': r['role']} for r in rows}


def ensure_default_admin():
    from db import get_db
    row = get_db().query_one("SELECT COUNT(*) as c FROM np_users")
    if row and row['c'] == 0:
        pwd = os.environ.get('NASNAP_ADMIN_PASSWORD', 'admin')
        get_db().execute(
            "INSERT OR IGNORE INTO np_users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
            ('admin', hash_password(pwd), 'admin', datetime.now(timezone.utc).isoformat())
        )
        logging.info("Default admin user created (set NASNAP_ADMIN_PASSWORD to change)")


def list_users() -> list:
    from db import get_db
    rows = get_db().query("SELECT username, role, created_at FROM np_users ORDER BY username")
    return [dict(r) for r in rows]


def create_user(username: str, password: str, role: str = 'viewer') -> None:
    from db import get_db
    get_db().execute(
        "INSERT INTO np_users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
        (username, hash_password(password), role, datetime.now(timezone.utc).isoformat())
    )


def delete_user(username: str) -> None:
    from db import get_db
    get_db().execute("DELETE FROM np_users WHERE username=?", (username,))
    get_db().execute("DELETE FROM np_sessions WHERE username=?", (username,))


def change_password(username: str, new_password: str) -> None:
    from db import get_db
    get_db().execute(
        "UPDATE np_users SET password_hash=? WHERE username=?",
        (hash_password(new_password), username)
    )


def get_user_timezone(username: str) -> str:
    """Personal timezone preference for the interactive UI. Keyed by username
    rather than a np_users foreign key since LDAP-authenticated users never get
    a np_users row (see ldap_authenticate) — np_user_prefs works for both."""
    from db import get_db
    row = get_db().query_one("SELECT timezone FROM np_user_prefs WHERE username=?", (username,))
    return row['timezone'] if row else ''


def set_user_timezone(username: str, tz: str) -> None:
    from db import get_db
    get_db().execute(
        "INSERT INTO np_user_prefs (username, timezone) VALUES (?,?) "
        "ON CONFLICT(username) DO UPDATE SET timezone=excluded.timezone",
        (username, tz)
    )


def get_ldap_config() -> dict | None:
    from db import get_db
    db = get_db()
    row = db.query_one("SELECT * FROM np_ldap_config WHERE id='default'")
    if not row:
        return None
    d = dict(row)
    if d.get('bind_password_enc'):
        try:
            d['bind_password'] = db._decrypt(d['bind_password_enc'])
        except Exception:
            d['bind_password'] = ''
    else:
        d['bind_password'] = ''
    return d


def ldap_authenticate(username: str, password: str) -> str | None:
    """Authenticate via LDAP/AD. Returns role ('admin'/'viewer') or None."""
    try:
        import ldap3
        import ldap3.utils.conv
    except ImportError:
        logging.warning("LDAP: ldap3 not installed — LDAP auth unavailable")
        return None

    cfg = get_ldap_config()
    if not cfg:
        logging.info("LDAP: no config row in DB — skipping")
        return None
    if not cfg.get('enabled'):
        logging.info("LDAP: disabled in config — skipping")
        return None

    logging.info(f"LDAP: authenticating user '{username}' via {cfg['server']}:{cfg.get('port', 389)}")

    srv = ldap3.Server(
        host=cfg['server'],
        port=int(cfg.get('port') or 389),
        use_ssl=bool(cfg.get('use_ssl')),
        connect_timeout=10,
        get_info=ldap3.NONE,
    )

    try:
        # Step 1: Service-account bind to find user DN
        conn = ldap3.Connection(
            srv, user=cfg['bind_dn'], password=cfg['bind_password'],
            authentication=ldap3.SIMPLE, auto_bind=False,
        )
        conn.open()
        if cfg.get('use_tls') and not cfg.get('use_ssl'):
            conn.start_tls()
        if not conn.bind():
            logging.warning(f"LDAP: service bind failed: {conn.result.get('description', 'unknown')}")
            return None

        safe_user = ldap3.utils.conv.escape_filter_chars(username)
        user_filter = (cfg.get('user_filter') or '(sAMAccountName={username})').replace('{username}', safe_user)
        conn.search(cfg['base_dn'], user_filter, attributes=['distinguishedName', 'memberOf'])

        if not conn.entries:
            logging.warning(f"LDAP: user '{username}' not found with filter {user_filter}")
            conn.unbind()
            return None

        entry = conn.entries[0]
        user_dn = entry.entry_dn
        member_of = [str(m) for m in entry.memberOf] if hasattr(entry, 'memberOf') and entry.memberOf else []
        logging.info(f"LDAP: found user DN={user_dn}, memberOf={len(member_of)} groups")
        conn.unbind()

        # Step 2: Bind as the user to verify their password
        user_conn = ldap3.Connection(
            srv, user=user_dn, password=password,
            authentication=ldap3.SIMPLE, auto_bind=False,
        )
        user_conn.open()
        if cfg.get('use_tls') and not cfg.get('use_ssl'):
            user_conn.start_tls()
        if not user_conn.bind():
            logging.warning(f"LDAP: password bind failed for '{username}': {user_conn.result.get('description', 'unknown')}")
            user_conn.unbind()
            return None
        user_conn.unbind()

        # Step 3: Determine role via memberOf
        admin_dn  = (cfg.get('admin_group_dn') or '').strip()
        viewer_dn = (cfg.get('viewer_group_dn') or '').strip()
        member_lower = [m.lower() for m in member_of]

        if admin_dn and admin_dn.lower() in member_lower:
            logging.info(f"LDAP: '{username}' → role=admin")
            return 'admin'
        if viewer_dn and viewer_dn.lower() in member_lower:
            logging.info(f"LDAP: '{username}' → role=viewer")
            return 'viewer'

        logging.warning(f"LDAP: '{username}' authenticated but not in any configured group (admin={admin_dn!r}, viewer={viewer_dn!r})")
        logging.warning(f"LDAP: user groups: {member_of}")
        return None

    except Exception as exc:
        logging.warning(f"LDAP: authentication error for '{username}': {exc}")
        return None


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('nasnap_session')
        sess = get_session(token) if token else None
        if not sess:
            return jsonify({'error': 'Unauthorized'}), 401
        g._nasnap_session = sess
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('nasnap_session')
        sess = get_session(token) if token else None
        if not sess:
            return jsonify({'error': 'Unauthorized'}), 401
        if sess.get('role') != ROLE_ADMIN:
            return jsonify({'error': 'Forbidden'}), 403
        g._nasnap_session = sess
        return f(*args, **kwargs)
    return decorated
