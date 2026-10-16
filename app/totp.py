"""RFC 6238 TOTP (SHA-1, 30 s, 6 digits) - compatible with Google Authenticator, Authy, 1Password, etc."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    h = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def verify(secret: str, code: str, last_step: int = 0, now: float | None = None) -> int | None:
    """Return the matched time step (for replay protection) or None. Accepts +/-1 step of clock drift."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != 6:
        return None
    cur = int((now or time.time()) // 30)
    for step in (cur - 1, cur, cur + 1):
        if step > last_step and hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def uri(secret: str, account: str, issuer: str = "BillTrack") -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"
            "&algorithm=SHA1&digits=6&period=30")
