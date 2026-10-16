"""Encrypted browser channel (application-layer, on top of TLS).

Every page load: the browser makes a fresh ECDH P-256 key pair (private key non-extractable, memory only) and
POSTs its public key to /api/hs. The server answers with its own one-time public key + random salt; both sides
derive the same AES-256-GCM key with HKDF-SHA256. After that every API call is a single POST /api/x whose body is
base64(nonce || AES-GCM(ciphertext)) of {method, path+query, body, seq, time, random padding}, and every response
is encrypted the same way. Method, path, query, body, status and payload are all inside the ciphertext, nonces are
random, so identical data never looks the same twice. Replays are refused (strictly increasing seq).

Key exchange is anonymous by design - TLS authenticates the server; this layer keeps payloads unreadable in browser
dev tools, HAR files, proxy/CDN logs and TLS-inspecting middleboxes. Bearer-token API calls (scripts) and the ICS
feed stay on plain TLS."""
import base64
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import config, crypto

INFO = b"billtrack-channel-v1"
IDLE_S, MAX_AGE_S, MAX_CHANNELS = 30 * 60, 12 * 3600, 5000
CLOCK_SKEW_MS = 5 * 60 * 1000
MAX_ENVELOPE = 600 * 1024
COOKIE = "bt_session"


@dataclass
class Chan:
    key: AESGCM
    created: float
    used: float
    seq: int = 0
    cookie: str | None = None      # hash of the session cookie this channel is bound to ("" = signed out)
    lock: threading.Lock = field(default_factory=threading.Lock)


_chans: dict[str, Chan] = {}
_lock = threading.Lock()


def _b64d(s: str) -> bytes:
    return base64.b64decode(s, validate=True)


def handshake(client_pub_b64: str) -> dict:
    raw = _b64d(client_pub_b64)
    if len(raw) != 65 or raw[0] != 4:
        raise ValueError("bad public key")
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)  # validates point on curve
    priv = ec.generate_private_key(ec.SECP256R1())
    salt = os.urandom(32)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=INFO).derive(priv.exchange(ec.ECDH(), peer))
    cid = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _lock:
        _prune(now)
        if len(_chans) >= MAX_CHANNELS:
            for k in sorted(_chans, key=lambda k: _chans[k].used)[: MAX_CHANNELS // 10]:
                del _chans[k]
        _chans[cid] = Chan(AESGCM(key), now, now)
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"cid": cid, "pub": base64.b64encode(pub).decode(), "salt": base64.b64encode(salt).decode()}


def _prune(now):
    for k in [k for k, c in _chans.items() if now - c.used > IDLE_S or now - c.created > MAX_AGE_S]:
        del _chans[k]


def get(cid: str) -> Chan | None:
    with _lock:
        c = _chans.get(cid)
        now = time.monotonic()
        if c and (now - c.used > IDLE_S or now - c.created > MAX_AGE_S):
            del _chans[cid]
            return None
        return c


def open_request(c: Chan, cid: str, body: bytes) -> dict:
    raw = _b64d(body.decode("ascii"))
    if len(raw) < 29:
        raise ValueError("short")
    try:
        msg = json.loads(c.key.decrypt(raw[:12], raw[12:], f"{cid}|req".encode()))
    except InvalidTag:
        raise ValueError("auth") from None
    seq, t = int(msg["s"]), int(msg["t"])
    if abs(t - time.time() * 1000) > CLOCK_SKEW_MS:
        raise ValueError("stale")
    with c.lock:
        if seq <= c.seq:
            raise ValueError("replay")
        c.seq = seq
        c.used = time.monotonic()
    return msg


def seal_response(c: Chan, cid: str, seq: int, status: int, headers: dict, body: bytes) -> bytes:
    pt = json.dumps({"s": status, "h": headers, "b": base64.b64encode(body).decode(),
                     "p": secrets.token_urlsafe(secrets.randbelow(96) + 8)}, separators=(",", ":")).encode()
    nonce = os.urandom(12)
    return base64.b64encode(nonce + c.key.encrypt(nonce, pt, f"{cid}|res|{seq}".encode()))


def _cookie(scope) -> str:
    """Hash of the session cookie exactly as the app will see it (last occurrence wins, like Starlette).
    More than one distinct value = ambiguous, never matches a binding."""
    vals = []
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                n, _, val = part.strip().partition("=")
                if n == COOKIE:
                    vals.append(val)
    if not vals:
        return ""
    if len(set(vals)) > 1:
        return "ambiguous:" + secrets.token_hex(8)
    return crypto.token_hash(vals[-1])


async def _read(receive, limit) -> bytes | None:
    buf = b""
    while True:
        m = await receive()
        buf += m.get("body", b"")
        if len(buf) > limit:
            return None
        if not m.get("more_body"):
            return buf


async def _send_plain(send, status, obj, extra=()):
    data = json.dumps(obj).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                            (b"content-length", str(len(data)).encode()), *extra]})
    await send({"type": "http.response.body", "body": data})


class ChannelMiddleware:
    """Pure ASGI wrapper: terminates /api/hs and /api/x, and blocks plaintext cookie-auth API calls."""

    def __init__(self, app, origin_ok):
        self.app, self.origin_ok = app, origin_ok

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        hdrs = {k.decode().lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if path in ("/api/hs", "/api/x"):
            if scope["method"] != "POST":
                return await _send_plain(send, 405, {"detail": "POST only"})
            if hdrs.get("x-bt") != "1" or not self.origin_ok(scope, hdrs):
                return await _send_plain(send, 403, {"detail": "Forbidden"})
            body = await _read(receive, MAX_ENVELOPE)
            if body is None:
                return await _send_plain(send, 413, {"detail": "Too large"})
            if path == "/api/hs":
                try:
                    out = handshake(json.loads(body)["pub"])
                except Exception:
                    return await _send_plain(send, 400, {"detail": "Bad handshake"})
                return await _send_plain(send, 200, out)
            return await self._tunnel(scope, hdrs, body, send)
        if (config.REQUIRE_CHANNEL and path.startswith("/api/") and "authorization" not in hdrs
                and not scope.get("bt_channel")):
            return await _send_plain(send, 403, {"detail": "Encrypted channel required", "rehandshake": True})
        return await self.app(scope, receive, send)

    async def _tunnel(self, scope, hdrs, body, send):
        cid = hdrs.get("x-bt-c", "")
        c = get(cid) if len(cid) < 64 else None
        if not c:
            return await _send_plain(send, 419, {"detail": "Channel expired", "rehandshake": True})
        try:
            msg = open_request(c, cid, body)
            method, target = str(msg["m"]).upper(), str(msg["p"])
            inner_body = msg.get("b")
            inner_body = b"" if inner_body is None else json.dumps(inner_body).encode()
        except Exception:
            return await _send_plain(send, 419, {"detail": "Channel error", "rehandshake": True})
        if method not in ("GET", "POST", "PUT", "DELETE") or not target.startswith("/api/") or target.startswith(("/api/x", "/api/hs")):
            return await _send_plain(send, 400, {"detail": "Bad request"})
        ck = _cookie(scope)
        if c.cookie is not None and c.cookie != ck and c.cookie != "":
            return await _send_plain(send, 419, {"detail": "Channel/session mismatch", "rehandshake": True})
        if not c.cookie:
            c.cookie = ck

        path, _, qs = target.partition("?")
        headers = [(k, v) for k, v in scope["headers"]
                   if k not in (b"content-length", b"content-type", b"x-bt", b"x-bt-c", b"authorization", b"transfer-encoding")]
        headers += [(b"x-bt", b"1"), (b"content-length", str(len(inner_body)).encode())]
        if inner_body:
            headers.append((b"content-type", b"application/json"))
        inner = dict(scope, method=method, path=path, raw_path=path.encode(), query_string=qs.encode(),
                     headers=headers, bt_channel=True)
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": inner_body, "more_body": False}

        status, out_hdrs, chunks = 500, [], []

        async def capture(m):
            nonlocal status, out_hdrs
            if m["type"] == "http.response.start":
                status, out_hdrs = m["status"], m.get("headers", [])
            elif m["type"] == "http.response.body":
                chunks.append(m.get("body", b""))

        await self.app(inner, receive, capture)
        keep, passthru = {}, []
        for k, v in out_hdrs:
            lk = k.lower()
            if lk in (b"content-type", b"content-disposition", b"retry-after"):
                keep[lk.decode()] = v.decode("latin-1")
            elif lk == b"set-cookie":  # session cookie must stay a real HttpOnly cookie
                passthru.append((k, v))
                val = v.decode("latin-1").split(";", 1)[0].partition("=")[2]
                c.cookie = crypto.token_hash(val) if val and val != '""' else ""
            elif lk in (b"content-security-policy", b"strict-transport-security", b"x-content-type-options",
                        b"referrer-policy", b"x-frame-options", b"permissions-policy", b"cross-origin-opener-policy",
                        b"cross-origin-resource-policy"):
                passthru.append((k, v))
        sealed = seal_response(c, hdrs["x-bt-c"], int(msg["s"]), status, keep, b"".join(chunks))
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"application/octet-stream"), (b"cache-control", b"no-store"),
            (b"content-length", str(len(sealed)).encode()), *passthru]})
        await send({"type": "http.response.body", "body": sealed})
