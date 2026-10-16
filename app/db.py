"""SQLCipher (AES-256 encrypted SQLite) storage. All data queries are parameterized."""
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path

import sqlcipher3 as sqlite

from . import config

PLAIN_HEADER = b"SQLite format 3\x00"
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE, pw_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL DEFAULT '#3b6ea5');
CREATE TABLE IF NOT EXISTS bills (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount_cents INTEGER NOT NULL,
  account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
  start_date TEXT NOT NULL, recurrence TEXT NOT NULL, end_date TEXT,
  autopay INTEGER NOT NULL DEFAULT 0, remind_days INTEGER NOT NULL DEFAULT 3,
  category TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
  due_date TEXT NOT NULL, paid_on TEXT NOT NULL, amount_cents INTEGER NOT NULL,
  UNIQUE(bill_id, due_date));
CREATE TABLE IF NOT EXISTS income_sources (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount_cents INTEGER NOT NULL,
  account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
  start_date TEXT NOT NULL, recurrence TEXT NOT NULL, end_date TEXT,
  hourly_rate_cents INTEGER NOT NULL DEFAULT 0, ot_multiplier REAL NOT NULL DEFAULT 1.5,
  active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS income_received (
  id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES income_sources(id) ON DELETE CASCADE,
  pay_date TEXT NOT NULL, amount_cents INTEGER NOT NULL, UNIQUE(source_id, pay_date));
CREATE TABLE IF NOT EXISTS income_extra (
  id INTEGER PRIMARY KEY, date TEXT NOT NULL, kind TEXT NOT NULL,
  source_id INTEGER REFERENCES income_sources(id) ON DELETE SET NULL,
  account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
  hours REAL, amount_cents INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY, date TEXT NOT NULL, name TEXT NOT NULL, amount_cents INTEGER NOT NULL,
  account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL, category TEXT NOT NULL DEFAULT '');

-- v2
CREATE TABLE IF NOT EXISTS occurrence_overrides (
  kind TEXT NOT NULL, item_id INTEGER NOT NULL, orig_date TEXT NOT NULL,
  new_date TEXT, skipped INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (kind, item_id, orig_date));
CREATE TABLE IF NOT EXISTS recovery_codes (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code_hash TEXT NOT NULL, used_at TEXT);
CREATE TABLE IF NOT EXISTS api_tokens (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL, scope TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT, last_used_at TEXT, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS invites (
  id INTEGER PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, email TEXT NOT NULL, role TEXT NOT NULL,
  created_by INTEGER, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT);
CREATE TABLE IF NOT EXISTS reset_tokens (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL, used_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, user_id INTEGER, username TEXT, action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', next_at TEXT NOT NULL, recurrence TEXT NOT NULL,
  email INTEGER NOT NULL DEFAULT 1, push INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ts TEXT NOT NULL, level TEXT NOT NULL DEFAULT 'info', title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
  read_at TEXT);
CREATE TABLE IF NOT EXISTS alert_log (
  user_id INTEGER NOT NULL, ref TEXT NOT NULL, kind TEXT NOT NULL, sent_at TEXT NOT NULL,
  PRIMARY KEY (user_id, ref, kind));
CREATE TABLE IF NOT EXISTS trackers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, target_cents INTEGER NOT NULL DEFAULT 0,
  start_cents INTEGER NOT NULL DEFAULT 0, target_date TEXT, account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
  color TEXT NOT NULL DEFAULT '#3b6ea5', notes TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tracker_entries (
  id INTEGER PRIMARY KEY, tracker_id INTEGER NOT NULL REFERENCES trackers(id) ON DELETE CASCADE,
  date TEXT NOT NULL, amount_cents INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS inv_accounts (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, institution TEXT NOT NULL DEFAULT '',
  cash_cents INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS holdings (
  id INTEGER PRIMARY KEY, inv_account_id INTEGER NOT NULL REFERENCES inv_accounts(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', quantity REAL NOT NULL DEFAULT 0,
  cost_basis_cents INTEGER NOT NULL DEFAULT 0, price_cents INTEGER NOT NULL DEFAULT 0, price_updated TEXT);
CREATE TABLE IF NOT EXISTS inv_snapshots (
  inv_account_id INTEGER NOT NULL REFERENCES inv_accounts(id) ON DELETE CASCADE, date TEXT NOT NULL,
  value_cents INTEGER NOT NULL, PRIMARY KEY (inv_account_id, date));
CREATE TABLE IF NOT EXISTS account_history (
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, date TEXT NOT NULL,
  balance_cents INTEGER NOT NULL, PRIMARY KEY (account_id, date));
CREATE INDEX IF NOT EXISTS ix_extra_date ON income_extra(date);
CREATE INDEX IF NOT EXISTS ix_exp_date ON expenses(date);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS ix_notif_user ON notifications(user_id, read_at);
CREATE INDEX IF NOT EXISTS ix_tentries ON tracker_entries(tracker_id, date);
"""

# Columns added after v1 (table, column, DDL). Applied only when missing, so fresh and upgraded DBs converge.
ADD_COLUMNS = [
    ("users", "role", "TEXT NOT NULL DEFAULT 'member'"),
    ("users", "email", "TEXT NOT NULL DEFAULT ''"),
    ("users", "display_name", "TEXT NOT NULL DEFAULT ''"),
    ("users", "disabled", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "totp_secret_enc", "TEXT NOT NULL DEFAULT ''"),
    ("users", "totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "totp_last_step", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "failed_logins", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "locked_until", "TEXT"),
    ("users", "created_at", "TEXT"),
    ("users", "pw_changed_at", "TEXT"),
    ("users", "prefs", "TEXT NOT NULL DEFAULT '{}'"),
    ("users", "perms_deny", "TEXT NOT NULL DEFAULT '[]'"),
    ("sessions", "created_at", "TEXT"),
    ("sessions", "last_seen", "TEXT"),
    ("sessions", "ip", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "user_agent", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "kind", "TEXT NOT NULL DEFAULT 'checking'"),
    ("accounts", "balance_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("accounts", "balance_updated", "TEXT"),
    # v2.4 bank accounts + credit cards
    ("accounts", "institution", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "last4", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "credit_limit_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("accounts", "low_balance_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("accounts", "util_alert_pct", "INTEGER NOT NULL DEFAULT 30"),
    ("accounts", "apr_bps", "INTEGER NOT NULL DEFAULT 0"),
    ("accounts", "due_day", "INTEGER"),
    ("accounts", "min_payment_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("accounts", "notes", "TEXT NOT NULL DEFAULT ''"),
]
SCHEMA_VERSION = 2


def migrate(con) -> None:
    con.executescript(SCHEMA)
    for table, col, ddl in ADD_COLUMNS:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # v1 -> v2: per-user alert tables replace the old global ones
    con.execute("DROP TABLE IF EXISTS notices")
    con.execute("DROP TABLE IF EXISTS digests")
    # Make sure an admin exists (the original v1 user becomes admin)
    if not con.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        con.execute("UPDATE users SET role='admin' WHERE id=(SELECT min(id) FROM users)")
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


class KeyError_(RuntimeError):
    pass


def new_key() -> str:
    return secrets.token_hex(32)


def load_key() -> str:
    key = ""
    if config.DB_KEY_FILE:
        try:
            key = Path(config.DB_KEY_FILE).read_text().strip()
        except OSError as e:
            raise KeyError_(f"Cannot read BT_DB_KEY_FILE: {e.strerror}") from None
    key = key or config.DB_KEY.strip()
    if not key:
        raise KeyError_("No database key. Set BT_DB_KEY_FILE (preferred) or BT_DB_KEY. "
                        "Generate one with: python -m app.manage gen-db-key")
    if not _HEX64.match(key):  # strict format = nothing else can reach the PRAGMA string
        raise KeyError_("Database key must be exactly 64 hex characters (256-bit)")
    return key


def is_plaintext(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == PLAIN_HEADER
    except FileNotFoundError:
        return False


def _apply_key(con, key: str):
    # Raw 256-bit key (x'..'): no per-connection PBKDF2 cost. Key is validated hex above.
    con.execute(f"PRAGMA key = \"x'{key}'\"")
    con.execute("PRAGMA temp_store = MEMORY")   # never spill plaintext temp files to disk
    con.execute("PRAGMA foreign_keys = ON")
    try:
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite.DatabaseError:
        con.close()
        raise KeyError_("Wrong database key, or the file is not a BillTrack database") from None


def connect(path: str | None = None, key: str | None = None):
    path = path or config.DB_PATH
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, mode=0o700, exist_ok=True)
    if is_plaintext(path):
        raise KeyError_("Database is not encrypted yet. Run: python -m app.manage encrypt-db")
    con = sqlite.connect(path, timeout=10)
    con.row_factory = sqlite.Row
    _apply_key(con, key or load_key())
    con.execute("PRAGMA journal_mode = WAL")      # WAL pages are encrypted too
    return con


def init() -> None:
    con = connect()
    try:
        migrate(con)
        con.commit()
    finally:
        con.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(config.DB_PATH + suffix, 0o600)
        except OSError:
            pass


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


# ---------- migration / backup ----------
def encrypt_plaintext(path: str, key: str) -> str:
    """Convert a v1.0 plaintext DB in place. Returns path of the plaintext copy kept aside."""
    plain_bak = path + ".plain.bak"
    con = sqlite.connect(path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tmp = path + ".enc.tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        con.execute(f"ATTACH DATABASE ? AS enc KEY \"x'{key}'\"", (tmp,))
        con.execute("SELECT sqlcipher_export('enc')")
        con.execute("DETACH DATABASE enc")
    finally:
        con.close()
    os.replace(path, plain_bak)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    os.chmod(plain_bak, 0o600)
    connect(path, key).close()  # verify
    return plain_bak


def backup_bytes(passphrase: str) -> bytes:
    """Encrypted copy of the DB under a separate passphrase (SQLCipher PBKDF2-HMAC-SHA512, 256k iters)."""
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "billtrack-backup.db")
        con = connect()
        try:
            con.execute("ATTACH DATABASE ? AS bk KEY ?", (out, passphrase))
            con.execute("SELECT sqlcipher_export('bk')")
            con.execute("DETACH DATABASE bk")
        finally:
            con.close()
        return Path(out).read_bytes()


def restore_backup(backup_path: str, passphrase: str, target: str, key: str) -> None:
    """Re-encrypt a passphrase backup under the server key into `target` (must not exist)."""
    if os.path.exists(target):
        raise FileExistsError(target)
    con = sqlite.connect(target)
    try:
        _apply_key(con, key)
        try:
            con.execute("ATTACH DATABASE ? AS bk KEY ?", (backup_path, passphrase))
            con.execute("SELECT count(*) FROM bk.sqlite_master").fetchone()
        except sqlite.DatabaseError:
            raise KeyError_("Wrong backup passphrase or not a BillTrack backup") from None
        con.execute("SELECT sqlcipher_export('main', 'bk')")
        con.execute("DETACH DATABASE bk")
        con.commit()
    except Exception:
        con.close()
        if os.path.exists(target):
            os.remove(target)
        raise
    con.close()
    os.chmod(target, 0o600)
