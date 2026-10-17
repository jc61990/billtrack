"""Key hierarchy (passphrase mode).

  DEK  = random 256-bit data key. SQLCipher encrypts the database with it (and field secrets derive from it).
  KEK_m = HKDF( Argon2id(passphrase) || key file )   -> wraps DEK for this server ("machine wrap")
  KEK_p = HKDF( Argon2id(passphrase) )               -> wraps DEK for disaster recovery ("portable wrap")
          the portable wrap is itself sealed with a key derived from the key file while it sits on this server,
          so on disk BOTH the passphrase and the key file are needed for either path.

The unlocked DEK is written only to RUNTIME_KEY (tmpfs, cleared at reboot), readable by the service user.
Changing the passphrase re-wraps the DEK; the database is not re-encrypted."""
import base64
import json
import os
import secrets
from pathlib import Path

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import config

KDF = {"alg": "argon2id", "t": 3, "m": 65536, "p": 2}
MIN_PASS = 12


class Locked(Exception):
    pass


class WrongKey(Exception):
    pass


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def stretch(passphrase: str, salt: bytes, p: dict = KDF) -> bytes:
    return hash_secret_raw(passphrase.encode(), salt, time_cost=p["t"], memory_cost=p["m"], parallelism=p["p"],
                           hash_len=32, type=Type.ID)


def _hkdf(ikm: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"billtrack-keys-v1", info=info).derive(ikm)


def _wrap(key: bytes, data: bytes, aad: bytes) -> str:
    n = os.urandom(12)
    return _b64(n + AESGCM(key).encrypt(n, data, aad))


def _unwrap(key: bytes, blob: str, aad: bytes) -> bytes:
    raw = _unb64(blob)
    try:
        return AESGCM(key).decrypt(raw[:12], raw[12:], aad)
    except InvalidTag:
        raise WrongKey("wrong passphrase or key file") from None


def check_passphrase(pw: str):
    if len(pw) < MIN_PASS:
        raise ValueError(f"Passphrase must be at least {MIN_PASS} characters")
    if len(pw) > 1024:
        raise ValueError("Passphrase too long")


def _keyfile_bytes(path: str | None = None) -> bytes:
    p = path or config.DB_KEY_FILE
    if not p:
        raise WrongKey("No key file configured (BT_DB_KEY_FILE)")
    try:
        k = Path(p).read_text().strip()
    except OSError as e:
        raise WrongKey(f"Cannot read key file: {e.strerror}") from None
    return bytes.fromhex(k)


# ---------- portable (DR) wrap: passphrase only ----------
def portable_wrap(dek: bytes, passphrase: str) -> dict:
    salt = os.urandom(16)
    k = _hkdf(stretch(passphrase, salt), b"portable")
    return {"v": 1, "kdf": KDF, "salt": _b64(salt), "wrap": _wrap(k, dek, b"billtrack-dek-portable")}


def portable_unwrap(blob: dict, passphrase: str) -> bytes:
    k = _hkdf(stretch(passphrase, _unb64(blob["salt"]), blob["kdf"]), b"portable")
    try:
        return _unwrap(k, blob["wrap"], b"billtrack-dek-portable")
    except WrongKey:
        raise WrongKey("Wrong database passphrase") from None


# ---------- keyring on this server ----------
def keyring_path() -> str:
    return config.KEYRING


def enabled() -> bool:
    return bool(config.KEYRING) and os.path.exists(config.KEYRING)


def create_keyring(passphrase: str, dek: bytes | None = None, keyfile: str | None = None, path: str | None = None) -> bytes:
    """New keyring for this server. Returns the DEK (generated unless given, e.g. on DR import)."""
    check_passphrase(passphrase)
    dek = dek or secrets.token_bytes(32)
    kf = _keyfile_bytes(keyfile)
    salt = os.urandom(16)
    st = stretch(passphrase, salt)
    ring = {
        "v": 1, "kdf": KDF, "salt": _b64(salt),
        "machine": _wrap(_hkdf(st + kf, b"machine"), dek, b"billtrack-dek-machine"),
        # portable wrap, sealed with the key file while it lives here
        "portable_sealed": _wrap(_hkdf(kf, b"portable-seal"), json.dumps(portable_wrap(dek, passphrase)).encode(),
                                 b"billtrack-portable-seal"),
        "fingerprint": _hkdf(dek, b"fingerprint").hex()[:16],
    }
    _atomic_write(path or config.KEYRING, json.dumps(ring, indent=1), 0o640)
    return dek


def _atomic_write(path: str, data: str | bytes, mode: int):
    tmp = f"{path}.tmp{secrets.token_hex(4)}"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _ring(path: str | None = None) -> dict:
    return json.loads(Path(path or config.KEYRING).read_text())


def unlock_dek(passphrase: str, keyfile: str | None = None, path: str | None = None) -> bytes:
    ring = _ring(path)
    kf = _keyfile_bytes(keyfile)
    st = stretch(passphrase, _unb64(ring["salt"]), ring["kdf"])
    try:
        return _unwrap(_hkdf(st + kf, b"machine"), ring["machine"], b"billtrack-dek-machine")
    except WrongKey:
        raise WrongKey("Wrong passphrase (or this isn't the key file the keyring was made with)") from None


def sealed_portable(keyfile: str | None = None, path: str | None = None) -> dict:
    """The passphrase-wrapped DEK for a DR bundle (needs the key file to unseal; still needs the passphrase to use)."""
    ring = _ring(path)
    return json.loads(_unwrap(_hkdf(_keyfile_bytes(keyfile), b"portable-seal"), ring["portable_sealed"],
                              b"billtrack-portable-seal"))


def fingerprint(dek: bytes) -> str:
    return _hkdf(dek, b"fingerprint").hex()[:16]


def write_runtime(dek: bytes, path: str | None = None):
    p = path or config.RUNTIME_KEY
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    _atomic_write(p, dek.hex(), 0o400)


def clear_runtime(path: str | None = None):
    p = path or config.RUNTIME_KEY
    if os.path.exists(p):
        with open(p, "r+b") as f:          # best effort overwrite (tmpfs: no disk remanence anyway)
            f.write(b"0" * os.path.getsize(p))
        os.remove(p)


def runtime_dek_hex() -> str:
    try:
        return Path(config.RUNTIME_KEY).read_text().strip()
    except FileNotFoundError:
        raise Locked("BillTrack is locked. On the server run: sudo billtrack-manage unlock") from None
    except PermissionError:
        raise Locked("Unlocked key isn't readable by the service user") from None


def change_passphrase(old: str, new: str, keyfile: str | None = None, path: str | None = None):
    check_passphrase(new)
    dek = unlock_dek(old, keyfile, path)
    create_keyring(new, dek, keyfile, path)
