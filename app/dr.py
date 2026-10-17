"""Passphrase migration and disaster-recovery bundles.

DR bundle file (".btdr"):
  magic "BTDR1\\n" | 4-byte header length | header JSON (kdf, salt, nonce prefix, chunk size)
  | repeated [4-byte length | AES-256-GCM chunk]   key = HKDF(Argon2id(backup password)), AAD = header + index + final
Plaintext is a tar with: manifest.json (version, hashes), db/billtrack.db (still SQLCipher-encrypted with the data
key), keys/dek.portable.json (data key wrapped by the DATABASE passphrase), config/*, tls/*.
So a bundle needs the backup password to open and the database passphrase to read any data."""
import hashlib
import io
import json
import os
import platform
import shutil
import struct
import tarfile
import tempfile
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import __version__, config, crypto, db, keys

MAGIC = b"BTDR1\n"
CHUNK = 1 << 20


class BundleError(Exception):
    pass


def _own(path):
    """When run as root (setup/CLI), hand files to the service user so the app can use them."""
    if os.geteuid() != 0:
        return
    try:
        import pwd
        pw = pwd.getpwnam("billtrack")
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except (KeyError, ImportError):
        pass


# ---------- sealed field secrets follow the data key ----------
def reseal_secrets(con, old_hex: str, new_hex: str):
    from .settings import SECRET
    for r in con.execute("SELECT id, totp_secret_enc FROM users WHERE totp_secret_enc<>''").fetchall():
        v = crypto.unseal(r["totp_secret_enc"], f"totp:{r['id']}", old_hex)
        con.execute("UPDATE users SET totp_secret_enc=? WHERE id=?", (crypto.seal(v, f"totp:{r['id']}", new_hex), r["id"]))
    for r in con.execute("SELECT key, value FROM settings").fetchall():
        if r["key"] in SECRET:
            v = crypto.unseal(json.loads(r["value"]), f"setting:{r['key']}", old_hex)
            con.execute("UPDATE settings SET value=? WHERE key=?",
                        (json.dumps(crypto.seal(v, f"setting:{r['key']}", new_hex)), r["key"]))
    try:
        for r in con.execute("SELECT item_id, access_token_enc FROM plaid_items").fetchall():
            v = crypto.unseal(r["access_token_enc"], f"plaid:{r['item_id']}", old_hex)
            con.execute("UPDATE plaid_items SET access_token_enc=? WHERE item_id=?",
                        (crypto.seal(v, f"plaid:{r['item_id']}", new_hex), r["item_id"]))
    except Exception as e:     # table absent on very old schemas
        if "no such table" not in str(e):
            raise


# ---------- legacy key file -> passphrase mode ----------
def migrate_to_passphrase(passphrase: str) -> dict:
    """Run with the service stopped. The DB (and server backups) move from the key-file key to a new data key
    wrapped by passphrase + key file."""
    keys.check_passphrase(passphrase)
    if keys.enabled():
        raise BundleError("Already using a passphrase")
    old = db.load_key()                                   # legacy: the key file itself
    dek = os.urandom(32)
    new = dek.hex()
    pending = config.KEYRING + ".pending"
    keys.create_keyring(passphrase, dek, path=pending)   # 1. keyring first, so the new key is never lost
    _own(pending)
    if os.path.exists(config.DB_PATH):
        con = db.connect(key=old)                         # 2. re-seal field secrets, then re-encrypt the DB
        try:
            reseal_secrets(con, old, new)
            con.commit()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute(f"PRAGMA rekey = \"x'{new}'\"")
        finally:
            con.close()
        db.connect(key=new).close()
    os.replace(pending, config.KEYRING)                   # 3. switch over
    converted, skipped = 0, 0
    bdir = db.backup_dir()
    if os.path.isdir(bdir):                               # 4. older on-server backups follow
        for f in os.listdir(bdir):
            if db.SNAP_RE.match(f):
                try:
                    db.rekey_file(os.path.join(bdir, f), old, new)
                    converted += 1
                except Exception:
                    skipped += 1
    keys.write_runtime(dek)
    _own(config.RUNTIME_KEY)
    return {"fingerprint": keys.fingerprint(dek), "backups_converted": converted, "backups_skipped": skipped}


def init_new(passphrase: str) -> str:
    keys.check_passphrase(passphrase)
    if keys.enabled():
        raise BundleError("Keyring already exists")
    dek = keys.create_keyring(passphrase)
    _own(config.KEYRING)
    keys.write_runtime(dek)
    _own(config.RUNTIME_KEY)
    return keys.fingerprint(dek)


def unlock(passphrase: str) -> str:
    dek = keys.unlock_dek(passphrase)
    keys.write_runtime(dek)
    _own(config.RUNTIME_KEY)
    return keys.fingerprint(dek)


# ---------- bundle crypto ----------
def _bundle_key(password: str, salt: bytes, kdf: dict) -> bytes:
    return keys._hkdf(keys.stretch(password, salt, kdf), b"dr-bundle")


def _encrypt(plain: bytes, password: str) -> bytes:
    salt, prefix = os.urandom(16), os.urandom(7)
    header = json.dumps({"kdf": keys.KDF, "salt": keys._b64(salt), "prefix": keys._b64(prefix), "chunk": CHUNK,
                         "app": __version__}, separators=(",", ":")).encode()
    aes = AESGCM(_bundle_key(password, salt, keys.KDF))
    out = io.BytesIO()
    out.write(MAGIC + struct.pack(">I", len(header)) + header)
    chunks = [plain[i:i + CHUNK] for i in range(0, len(plain), CHUNK)] or [b""]
    for i, c in enumerate(chunks):
        final = i == len(chunks) - 1
        nonce = prefix + struct.pack(">I", i) + (b"\x01" if final else b"\x00")
        ct = aes.encrypt(nonce, c, header + struct.pack(">I?", i, final))
        out.write(struct.pack(">I", len(ct)) + ct)
    return out.getvalue()


def _decrypt(blob: bytes, password: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise BundleError("Not a BillTrack disaster-recovery backup")
    hlen = struct.unpack(">I", blob[6:10])[0]
    if hlen > 4096:
        raise BundleError("Corrupt backup header")
    header = blob[10:10 + hlen]
    h = json.loads(header)
    aes = AESGCM(_bundle_key(password, keys._unb64(h["salt"]), h["kdf"]))
    prefix, pos, i, parts, done = keys._unb64(h["prefix"]), 10 + hlen, 0, [], False
    while pos < len(blob):
        if done:
            raise BundleError("Backup has extra data after the end (tampered?)")
        n = struct.unpack(">I", blob[pos:pos + 4])[0]
        ct = blob[pos + 4:pos + 4 + n]
        pos += 4 + n
        for final in (False, True):
            nonce = prefix + struct.pack(">I", i) + (b"\x01" if final else b"\x00")
            try:
                parts.append(aes.decrypt(nonce, ct, header + struct.pack(">I?", i, final)))
                done = final
                break
            except InvalidTag:
                continue
        else:
            raise BundleError("Wrong backup password, or the file is damaged") if i == 0 else \
                BundleError("Backup file is damaged or was modified")
        i += 1
    if not done:
        raise BundleError("Backup file is truncated")
    return b"".join(parts)


# ---------- export ----------
def _add(tar, name, data: bytes):
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime = len(data), 0o600, int(datetime.now().timestamp())
    tar.addfile(ti, io.BytesIO(data))


def export_bundle(backup_password: str, extra_files: dict | None = None, snapshot: str | None = None) -> bytes:
    """Everything needed to rebuild this install elsewhere. extra_files: {"config/billtrack.env": path, ...}
    (unreadable ones are skipped and listed in the manifest)."""
    keys.check_passphrase(backup_password)
    if not keys.enabled():
        raise BundleError("Set a database passphrase first (sudo billtrack-manage set-passphrase)")
    portable = keys.sealed_portable()
    dek_hex = db.load_key()
    with tempfile.TemporaryDirectory() as td:
        snap = os.path.join(td, "billtrack.db")
        src = db.connect(snapshot, dek_hex) if snapshot else db.connect(key=dek_hex)
        try:
            src.execute(f"ATTACH DATABASE ? AS bk KEY \"x'{dek_hex}'\"", (snap,))
            src.execute("SELECT sqlcipher_export('bk')")
            src.execute("DETACH DATABASE bk")
        finally:
            src.close()
        files = {"db/billtrack.db": open(snap, "rb").read(),
                 "keys/dek.portable.json": json.dumps(portable).encode()}
    skipped = []
    for name, path in (extra_files or {}).items():
        try:
            files[name] = open(path, "rb").read()
        except OSError:
            skipped.append(name)
    manifest = {"format": 1, "app_version": __version__, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "host": platform.node(), "fingerprint": keys.fingerprint(bytes.fromhex(dek_hex)),
                "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}, "not_included": skipped}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        _add(tar, "manifest.json", json.dumps(manifest, indent=1).encode())
        for n, d in files.items():
            _add(tar, n, d)
    return _encrypt(buf.getvalue(), backup_password)


def default_extra_files() -> dict:
    tls = os.getenv("BT_TLS_DIR", "/etc/billtrack/tls")
    return {"config/billtrack.env": os.getenv("BT_ENV_FILE", "/etc/billtrack/billtrack.env"),
            "config/install.conf": os.getenv("BT_INSTALL_CONF", "/etc/billtrack/install.conf"),
            "tls/ca.crt": os.path.join(tls, "ca.crt"), "tls/ca.key": os.path.join(tls, "ca.key")}


# ---------- import ----------
def open_bundle(blob: bytes, backup_password: str, db_passphrase: str) -> tuple[dict, dict, bytes]:
    """Decrypt + verify. Returns (manifest, files, dek). Raises on any wrong key or tampering."""
    plain = _decrypt(blob, backup_password)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r") as tar:
        for m in tar.getmembers():
            if not m.isfile() or m.name.startswith("/") or ".." in m.name.split("/"):
                raise BundleError("Unexpected entry in backup")
            files[m.name] = tar.extractfile(m).read()
    manifest = json.loads(files.pop("manifest.json"))
    for n, h in manifest["files"].items():
        if hashlib.sha256(files.get(n, b"")).hexdigest() != h:
            raise BundleError(f"Backup content check failed for {n}")
    dek = keys.portable_unwrap(json.loads(files["keys/dek.portable.json"]), db_passphrase)
    if keys.fingerprint(dek) != manifest["fingerprint"]:
        raise BundleError("Key fingerprint mismatch")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "t.db")
        open(p, "wb").write(files["db/billtrack.db"])
        c = db.connect(p, dek.hex())
        try:
            if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BundleError("Database in the backup failed its integrity check")
        finally:
            c.close()
    return manifest, files, dek


def import_bundle(blob: bytes, backup_password: str, db_passphrase: str, write_config: bool = True) -> dict:
    """Restore onto this server (service stopped). The key file of THIS server is used for the new keyring."""
    manifest, files, dek = open_bundle(blob, backup_password, db_passphrase)
    os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), mode=0o700, exist_ok=True)
    prev = None
    if os.path.exists(config.DB_PATH):
        prev = f"{config.DB_PATH}.before-import-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        os.replace(config.DB_PATH, prev)
    for s in ("-wal", "-shm"):
        if os.path.exists(config.DB_PATH + s):
            os.remove(config.DB_PATH + s)
    keys._atomic_write(config.DB_PATH, files["db/billtrack.db"], 0o600)
    _own(config.DB_PATH)
    if keys.enabled():
        os.replace(config.KEYRING, config.KEYRING + ".replaced")
    keys.create_keyring(db_passphrase, dek)               # wrapped for THIS server's key file
    _own(config.KEYRING)
    keys.write_runtime(dek)
    _own(config.RUNTIME_KEY)
    written = []
    if write_config:
        targets = default_extra_files()
        for name, data in files.items():
            if name in targets:
                dst = targets[name]
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                mode = 0o600 if name.endswith((".key", "install.conf")) else 0o640
                keys._atomic_write(dst, data, mode)
                if name in ("config/billtrack.env", "config/install.conf") and os.geteuid() == 0:
                    try:
                        import grp
                        os.chown(dst, 0, grp.getgrnam("billtrack").gr_gid)
                        os.chmod(dst, 0o640)
                    except KeyError:
                        pass
                written.append(dst)
    return {"from_host": manifest["host"], "created": manifest["created"], "app_version": manifest["app_version"],
            "previous_db": prev, "config_written": written, "not_included": manifest.get("not_included", [])}
