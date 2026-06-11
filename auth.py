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


def create_session(username: str) -> str:
    from db import get_db
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).isoformat()
    get_db().execute(
        "INSERT OR REPLACE INTO np_sessions (token, username, expires_at) VALUES (?,?,?)",
        (token, username, expires)
    )
    return token


def get_session(token: str) -> dict | None:
    from db import get_db
    if not token:
        return None
    row = get_db().query_one(
        "SELECT username, expires_at FROM np_sessions WHERE token=?", (token,)
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
        pwd = os.environ.get('NAPPROX_ADMIN_PASSWORD', 'admin')
        get_db().execute(
            "INSERT INTO np_users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
            ('admin', hash_password(pwd), 'admin', datetime.now(timezone.utc).isoformat())
        )
        logging.info("Default admin user created (set NAPPROX_ADMIN_PASSWORD to change)")


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('napprox_session')
        sess = get_session(token) if token else None
        if not sess:
            return jsonify({'error': 'Unauthorized'}), 401
        g._napprox_session = sess
        return f(*args, **kwargs)
    return decorated
