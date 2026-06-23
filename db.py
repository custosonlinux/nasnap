import os
import base64
import logging
import sqlite3
import threading

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ENCRYPTION_AVAILABLE = True
except ImportError:
    ENCRYPTION_AVAILABLE = False

DATA_DIR = os.environ.get('NASNAP_DATA', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
DB_FILE  = os.path.join(DATA_DIR, 'nasnap.db')
KEY_FILE = os.path.join(DATA_DIR, '.nasnap_aes256.key')

_PLUGIN_SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'plugins', 'netapp_storage', 'db', 'schema.sql')


class NaSnapDB:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._local = threading.local()
        self.aesgcm = None
        self._init_encryption()
        self._init_schema()
        self._initialized = True
        logging.info(f"NaSnap DB initialized: {DB_FILE}")

    def _init_encryption(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(KEY_FILE):
            with open(KEY_FILE, 'rb') as f:
                key = f.read()
            if len(key) != 32:
                logging.warning("Invalid AES key length — regenerating")
                key = os.urandom(32)
                with open(KEY_FILE, 'wb') as f:
                    f.write(key)
        else:
            key = os.urandom(32)
            with open(KEY_FILE, 'wb') as f:
                f.write(key)
            try:
                os.chmod(KEY_FILE, 0o600)
            except Exception as e:
                raise RuntimeError(f"Cannot secure encryption key file: {e}")
            logging.info("Generated new AES-256-GCM key")
        if ENCRYPTION_AVAILABLE:
            self.aesgcm = AESGCM(key)
        else:
            logging.error("cryptography package not available — encryption disabled!")

    def _get_conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            os.makedirs(DATA_DIR, exist_ok=True)
            conn = sqlite3.connect(DB_FILE, check_same_thread=False,
                                   timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            try:
                os.chmod(DB_FILE, 0o600)
            except Exception:
                pass
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self):
        conn = self._get_conn()
        # Auth tables (standalone — not in plugin schema)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS np_users (
                username   TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role       TEXT NOT NULL DEFAULT 'admin',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS np_sessions (
                token      TEXT PRIMARY KEY,
                username   TEXT NOT NULL,
                role       TEXT NOT NULL DEFAULT 'viewer',
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS np_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS np_ldap_config (
                id                TEXT PRIMARY KEY DEFAULT 'default',
                enabled           INTEGER NOT NULL DEFAULT 0,
                server            TEXT NOT NULL DEFAULT '',
                port              INTEGER NOT NULL DEFAULT 389,
                use_ssl           INTEGER NOT NULL DEFAULT 0,
                use_tls           INTEGER NOT NULL DEFAULT 1,
                bind_dn           TEXT NOT NULL DEFAULT '',
                bind_password_enc TEXT NOT NULL DEFAULT '',
                base_dn           TEXT NOT NULL DEFAULT '',
                user_filter       TEXT NOT NULL DEFAULT '(sAMAccountName={username})',
                admin_group_dn    TEXT NOT NULL DEFAULT '',
                viewer_group_dn   TEXT NOT NULL DEFAULT '',
                updated_at        TEXT NOT NULL DEFAULT ''
            );
        """)
        # Migrations — safe to run repeatedly (errors are ignored)
        for migration in [
            "ALTER TABLE np_sessions ADD COLUMN role TEXT NOT NULL DEFAULT 'viewer'",
            "UPDATE np_sessions SET role='admin' WHERE username IN (SELECT username FROM np_users WHERE role='admin')",
            "ALTER TABLE netapp_sfr_sessions ADD COLUMN job_id TEXT NOT NULL DEFAULT ''",
        ]:
            try:
                conn.execute(migration)
                conn.commit()
            except Exception:
                pass
        # Plugin tables
        if os.path.exists(_PLUGIN_SCHEMA):
            with open(_PLUGIN_SCHEMA) as f:
                sql = f.read()
            for stmt in sql.split(';'):
                stmt = stmt.strip()
                if stmt:
                    try:
                        conn.execute(stmt)
                    except Exception as e:
                        logging.debug(f"Schema: {e}")
        conn.commit()

    def execute(self, sql: str, params: tuple = ()):
        conn = self._get_conn()
        conn.execute(sql, params)
        conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list:
        conn = self._get_conn()
        return conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()):
        conn = self._get_conn()
        return conn.execute(sql, params).fetchone()

    def _encrypt(self, data: str) -> str:
        if not data or not self.aesgcm:
            return data
        nonce = os.urandom(12)
        ct = self.aesgcm.encrypt(nonce, data.encode(), None)
        return 'aes256:' + base64.b64encode(nonce + ct).decode()

    def _decrypt(self, data: str) -> str:
        if not data:
            return data
        if data.startswith('aes256:'):
            if not self.aesgcm:
                raise RuntimeError("AES-256-GCM data found but encryption not initialized")
            try:
                raw = base64.b64decode(data[7:])
                nonce, ct = raw[:12], raw[12:]
                return self.aesgcm.decrypt(nonce, ct, None).decode('utf-8')
            except Exception as e:
                detail = str(e) or f"{type(e).__name__} (wrong key or corrupted data)"
                raise RuntimeError(f"AES-256-GCM decryption failed: {detail}")
        return data


_db: NaSnapDB | None = None


def get_db() -> NaSnapDB:
    global _db
    if _db is None:
        _db = NaSnapDB()
    return _db
