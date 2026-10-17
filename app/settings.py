"""System settings stored in the encrypted DB (edited in Admin > System settings). Secrets additionally AES-GCM sealed."""
import json
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, crypto
from .db import tx

SECRET = {"smtp_pass", "ntfy_token", "plaid_secret"}
TYPES = {"plaid_sync_hours": int, "week_start": int, "digest_weekday": int, "digest_hour": int, "smtp_port": int,
         "require_2fa": bool, "session_minutes": int, "idle_minutes": int}
_cache: dict | None = None
_lock = threading.Lock()


def _load() -> dict:
    global _cache
    c = _cache
    if c is not None:
        return c
    vals = dict(config.DEFAULTS)
    with tx() as con:   # outside the lock: a slow DB must never block other readers on the lock
        for r in con.execute("SELECT key, value FROM settings"):
            if r["key"] in vals:
                v = json.loads(r["value"])
                vals[r["key"]] = crypto.unseal(v, f"setting:{r['key']}") if r["key"] in SECRET else v
    with _lock:
        _cache = vals
    return vals


def get(key: str):
    return _load()[key]


def all_public() -> dict:
    """Everything except secret values (those report only whether they are set)."""
    v = dict(_load())
    for k in SECRET:
        v[k + "_set"] = bool(v.pop(k))
    return v


def validate(key: str, value):
    if key not in config.DEFAULTS:
        raise ValueError(f"Unknown setting {key}")
    t = TYPES.get(key, str)
    if t is bool:
        value = bool(value)
    elif t is int:
        value = int(value)
    else:
        value = str(value).strip()
        if len(value) > 300 or any(not ch.isprintable() for ch in value):
            raise ValueError(f"{key}: invalid text")
    rng = {"week_start": (0, 6), "digest_weekday": (0, 6), "digest_hour": (0, 23), "smtp_port": (1, 65535),
           "session_minutes": (5, 43200), "idle_minutes": (5, 43200), "plaid_sync_hours": (1, 24)}
    if key in rng and not rng[key][0] <= value <= rng[key][1]:
        raise ValueError(f"{key} must be between {rng[key][0]} and {rng[key][1]}")
    if key == "timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown timezone (use e.g. America/Chicago)") from None
    if key == "plaid_env" and value not in ("production", "sandbox"):
        raise ValueError("plaid_env must be production or sandbox")
    if key == "plaid_client_id" and value and not value.isalnum():
        raise ValueError("Plaid client ID should be letters and numbers only")
    if key == "registration_mode" and value not in ("approval", "invite_only", "closed"):
        raise ValueError("registration_mode must be approval, invite_only or closed")
    if key == "smtp_auth" and value not in ("password", "none"):
        raise ValueError("smtp_auth must be password or none")
    if key == "smtp_helo" and value and not __import__("re").fullmatch(
            r"[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+", value):
        raise ValueError("EHLO name must be a full hostname like bills.example.com")
    if key == "smtp_tls" and value not in ("starttls", "ssl"):
        raise ValueError("smtp_tls must be starttls or ssl")
    if key in ("base_url", "ntfy_server") and value and not value.startswith(("https://", "http://")):
        raise ValueError(f"{key} must start with https://")
    if key == "base_url":
        value = value.rstrip("/")
    return value


def update(values: dict) -> list[str]:
    global _cache
    clean = {k: validate(k, v) for k, v in values.items()}
    with tx() as con:
        for k, v in clean.items():
            stored = crypto.seal(v, f"setting:{k}") if k in SECRET else v
            con.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (k, json.dumps(stored)))
    with _lock:
        _cache = None
    return list(clean)


def reset_cache():
    global _cache
    with _lock:
        _cache = None


import contextvars

_user_tz: contextvars.ContextVar = contextvars.ContextVar("bt_user_tz", default=None)


def valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
        return bool(name) and len(name) <= 64
    except (ZoneInfoNotFoundError, ValueError):
        return False


def system_tz() -> ZoneInfo:
    return ZoneInfo(get("timezone"))


def tz() -> ZoneInfo:
    """The time zone for whoever this work is for: the signed-in user's (set per request / per alert), else the
    system default from Admin > System settings."""
    u = _user_tz.get()
    return ZoneInfo(u) if u else system_tz()


def user_tz_name(prefs: dict) -> str:
    n = (prefs or {}).get("timezone") or ""
    return n if valid_tz(n) else ""


def use_tz(name: str | None):
    """Set the zone for the current request/task; returns a token for reset_tz."""
    return _user_tz.set(name if name and valid_tz(name) else None)


def reset_tz(token):
    _user_tz.reset(token)
