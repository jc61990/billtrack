"""Authentication, authorization (roles + permissions), sessions, API tokens, throttling, audit."""
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request

from . import crypto, settings
from .db import tx

ph = PasswordHasher()  # Argon2id
_DUMMY = ph.hash(secrets.token_hex(16))
COOKIE = "bt_session"

PERMS = ["finance:read", "finance:write", "trackers:read", "trackers:write",
         "investments:read", "investments:write", "reminders", "self", "admin"]
FINANCE = {"finance:read", "finance:write", "trackers:read", "trackers:write", "investments:read",
           "investments:write", "reminders"}
ROLE_PERMS = {
    "admin": {"admin", "self"},          # runs the system; never sees anyone's money
    "user": FINANCE | {"self"},          # their own private finances; no server settings
}
DENYABLE: list = []
SCOPES = {
    "read": {"finance:read", "trackers:read", "investments:read"},
    "write": {"finance:read", "finance:write", "trackers:read", "trackers:write",
              "investments:read", "investments:write"},
}
MIN_PW = 12
LOCK_AFTER, LOCK_MIN = 8, 15


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------- passwords ----------
def hash_pw(pw: str) -> str:
    if len(pw) < MIN_PW:
        raise ValueError(f"Password must be at least {MIN_PW} characters")
    if len(pw) > 256:
        raise ValueError("Password too long")
    return ph.hash(pw)


def check_pw(pw_hash: str | None, pw: str) -> bool:
    try:
        ph.verify(pw_hash or _DUMMY, pw)
        return pw_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def effective_perms(user) -> set[str]:
    perms = set(ROLE_PERMS.get(user["role"], set()))
    if user["role"] != "admin":
        try:
            perms -= set(json.loads(user["perms_deny"] or "[]"))
        except ValueError:
            pass
    if "finance:read" not in perms:
        perms.discard("finance:write")
    for area in ("trackers", "investments"):
        if f"{area}:read" not in perms:
            perms.discard(f"{area}:write")
    return perms


# ---------- principal ----------
@dataclass
class Principal:
    user_id: int
    username: str
    role: str
    perms: set = field(default_factory=set)
    via: str = "session"
    token_id: int | None = None
    session_hash: str | None = None
    mfa_setup_required: bool = False

    def can(self, perm: str) -> bool:
        return perm in self.perms


# credential hash -> (time zone name, expiry). Lets the middleware set the user's zone before any endpoint runs.
_tz_cache: dict[str, tuple[str, float]] = {}


def clear_tz_cache():
    _tz_cache.clear()


def context_for_request(headers) -> tuple[str, int | None, int | None]:
    """(time zone, owner id if a finance user, week start) for this request's credential. Cached 60 s.
    Only used to set scope; authorization is still done by require() on every endpoint."""
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        h, q = crypto.token_hash(auth[7:].strip()), ("SELECT u.id, u.role, u.prefs, u.status, u.disabled FROM api_tokens t "
                                                     "JOIN users u ON u.id=t.user_id WHERE t.token_hash=? AND t.revoked=0")
    else:
        tok = None
        for part in headers.get("cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                tok = v
        if not tok or len(tok) > 200:
            return "", None, None
        h, q = crypto.token_hash(tok), ("SELECT u.id, u.role, u.prefs, u.status, u.disabled FROM sessions s "
                                        "JOIN users u ON u.id=s.user_id WHERE s.token_hash=?")
    hit = _tz_cache.get(h)
    if hit and hit[1] > time.monotonic():
        return hit[0]
    ctx = ("", None, None)
    try:
        with tx() as con:
            r = con.execute(q, (h,)).fetchone()
        if r:
            pr = json.loads(r["prefs"] or "{}")
            ws = pr.get("week_start")
            owner = r["id"] if r["role"] == "user" and r["status"] == "active" and not r["disabled"] else None
            ctx = (settings.user_tz_name(pr), owner, ws if isinstance(ws, int) and 0 <= ws <= 6 else None)
    except Exception:
        pass
    if len(_tz_cache) > 5000:
        _tz_cache.clear()
    _tz_cache[h] = (ctx, time.monotonic() + 60)
    return ctx


def _from_bearer(tok: str) -> Principal:
    h = crypto.token_hash(tok)
    with tx() as con:
        r = con.execute("""SELECT t.id tid, t.scope, t.expires_at, u.* FROM api_tokens t JOIN users u ON u.id=t.user_id
                           WHERE t.token_hash=? AND t.revoked=0""", (h,)).fetchone()
        if not r or r["disabled"] or (r["expires_at"] and r["expires_at"] < iso(now_utc())):
            raise HTTPException(401, "Invalid or expired API token")
        con.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (iso(now_utc()), r["tid"]))
    perms = effective_perms(r) & SCOPES.get(r["scope"], set())
    return Principal(r["id"], r["username"], r["role"], perms, "token", r["tid"])


def _from_session(tok: str) -> Principal:
    h = crypto.token_hash(tok)
    n = now_utc()
    with tx() as con:
        r = con.execute("""SELECT s.expires_at, s.last_seen, s.created_at AS s_created, u.* FROM sessions s
                           JOIN users u ON u.id=s.user_id WHERE s.token_hash=?""", (h,)).fetchone()
        idle = timedelta(minutes=settings.get("idle_minutes"))
        hard = timedelta(minutes=settings.get("session_minutes"))   # also caps sessions made under a longer limit
        if (not r or r["disabled"] or r["expires_at"] < iso(n)
                or (r["s_created"] and datetime.fromisoformat(r["s_created"]) + hard < n)
                or (r["last_seen"] and datetime.fromisoformat(r["last_seen"]) + idle < n)):
            if r:
                con.execute("DELETE FROM sessions WHERE token_hash=?", (h,))
            raise HTTPException(401, "Sign in required")
        if not r["last_seen"] or datetime.fromisoformat(r["last_seen"]) < n - timedelta(seconds=30):
            con.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (iso(n), h))
    p = Principal(r["id"], r["username"], r["role"], effective_perms(r), "session", None, h)
    p.mfa_setup_required = bool(settings.get("require_2fa")) and not r["totp_enabled"]
    return p


def principal(request: Request) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached:
        return cached
    auth = request.headers.get("authorization", "")
    if auth:  # bearer requests never fall back to cookies (keeps CSRF exemption sound)
        if not auth.lower().startswith("bearer ") or len(auth) > 300:
            raise HTTPException(401, "Malformed Authorization header")
        p = _from_bearer(auth[7:].strip())
    else:
        tok = request.cookies.get(COOKIE)
        if not tok or len(tok) > 200:
            raise HTTPException(401, "Sign in required")
        p = _from_session(tok)
    request.state.principal = p
    return p


def require(perm: str, session_only: bool = False):
    def dep(request: Request) -> Principal:
        p = principal(request)
        if session_only and p.via != "session":
            raise HTTPException(403, "Not available with API tokens")
        if p.mfa_setup_required and perm != "self":
            raise HTTPException(403, "Two-factor authentication is required. Set it up in Settings.")
        if not p.can(perm):
            raise HTTPException(403, "You don't have permission for this")
        return p
    return dep


# ---------- sessions ----------
def new_session(con, user_id: int, ip: str, ua: str) -> str:
    tok = crypto.new_token()
    n = now_utc()
    con.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(n),))
    con.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at,last_seen,ip,user_agent) "
                "VALUES (?,?,?,?,?,?,?)", (crypto.token_hash(tok), user_id,
                                           iso(n + timedelta(minutes=settings.get("session_minutes"))),
                                           iso(n), iso(n), ip[:64], ua[:200]))
    return tok


# ---------- throttling ----------
class Throttle:
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def _prune(self, key, now):
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        with self.lock:
            return len(self._prune(key, time.monotonic())) >= self.limit

    def hit(self, key: str) -> bool:
        """Record a hit; return True if now over the limit."""
        with self.lock:
            now = time.monotonic()
            q = self._prune(key, now)
            q.append(now)
            if len(self.hits) > 10000:  # bound memory
                for k in [k for k, v in self.hits.items() if not v][:5000]:
                    del self.hits[k]
            return len(q) > self.limit


login_ip = Throttle(10, 900)      # failed logins per IP / 15 min
mfa_ip = Throttle(10, 900)
forgot_ip = Throttle(5, 3600)

# Pending second-factor logins: hash(mfa_token) -> (user_id, expires_monotonic, attempts)
_mfa: dict[str, list] = {}
_mfa_lock = threading.Lock()


def mfa_start(user_id: int) -> str:
    tok = crypto.new_token()
    with _mfa_lock:
        now = time.monotonic()
        for k in [k for k, v in _mfa.items() if v[1] < now]:
            del _mfa[k]
        _mfa[crypto.token_hash(tok)] = [user_id, now + 300, 0]
    return tok


def mfa_user(tok: str, consume: bool = False) -> int | None:
    h = crypto.token_hash(tok or "")
    with _mfa_lock:
        v = _mfa.get(h)
        if not v or v[1] < time.monotonic() or v[2] >= 5:
            _mfa.pop(h, None)
            return None
        v[2] += 1
        if consume:
            del _mfa[h]
        return v[0]


def mfa_done(tok: str):
    with _mfa_lock:
        _mfa.pop(crypto.token_hash(tok or ""), None)


# ---------- audit ----------
def client_ip(request: Request) -> str:
    return (request.client.host if request.client else "?")[:64]


def audit(con, request: Request | None, action: str, target: str = "", detail: str = "",
          user_id: int | None = None, username: str | None = None):
    p = getattr(request.state, "principal", None) if request else None
    if p and user_id is None:
        user_id, username = p.user_id, p.username + (" (api)" if p.via == "token" else "")
    con.execute("INSERT INTO audit_log(ts,user_id,username,action,target,ip,detail) VALUES (?,?,?,?,?,?,?)",
                (iso(now_utc()), user_id, username or "", action, str(target)[:200],
                 client_ip(request) if request else "", str(detail)[:500]))
