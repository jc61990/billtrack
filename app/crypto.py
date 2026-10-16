"""Field-level encryption for secrets stored inside the (already SQLCipher-encrypted) database.

Defense in depth: TOTP secrets, SMTP password, and ntfy token are additionally sealed with AES-256-GCM using a
key derived (HKDF-SHA256) from the database key. A passphrase backup handed to someone therefore still doesn't
expose them without the server key."""
import base64
import hashlib
import hmac
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_cache: dict[str, bytes] = {}


def _field_key(db_key_hex: str | None = None) -> bytes:
    if db_key_hex is None:
        from .db import load_key
        db_key_hex = load_key()
    if db_key_hex not in _cache:
        _cache.clear()
        _cache[db_key_hex] = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"billtrack-field-v1",
                                  info=b"field-encryption").derive(bytes.fromhex(db_key_hex))
    return _cache[db_key_hex]


def seal(plain: str, aad: str, db_key_hex: str | None = None) -> str:
    if plain == "":
        return ""
    nonce = os.urandom(12)
    ct = AESGCM(_field_key(db_key_hex)).encrypt(nonce, plain.encode(), aad.encode())
    return "v1:" + base64.urlsafe_b64encode(nonce + ct).decode()


def unseal(token: str, aad: str, db_key_hex: str | None = None) -> str:
    if not token:
        return ""
    if not token.startswith("v1:"):
        raise ValueError("Unknown sealed format")
    raw = base64.urlsafe_b64decode(token[3:])
    try:
        return AESGCM(_field_key(db_key_hex)).decrypt(raw[:12], raw[12:], aad.encode()).decode()
    except InvalidTag:
        raise ValueError("Sealed value failed authentication (wrong key or tampered)") from None


def token_hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def new_token(prefix: str = "") -> str:
    t = secrets.token_urlsafe(32)
    return f"{prefix}_{t}" if prefix else t


def eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
