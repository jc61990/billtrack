"""Bootstrap settings from environment. Most runtime settings live in the encrypted DB (admin portal);
env values here are only first-run defaults for those."""
import os


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


DB_PATH = os.getenv("BT_DB_PATH", "./data/billtrack.db")
DB_KEY_FILE = os.getenv("BT_DB_KEY_FILE", "")          # 64 hex chars (preferred)
DB_KEY = os.getenv("BT_DB_KEY", "")                    # inline alternative
# Passphrase mode (2.9+): master key wrapped by passphrase + key file; unlocked copy lives only in /run (tmpfs)
KEYRING = os.getenv("BT_KEYRING", "")                  # e.g. /etc/billtrack/keyring.json
RUNTIME_KEY = os.getenv("BT_RUNTIME_KEY", "/run/billtrack/dek")
COOKIE_SECURE = _bool("BT_COOKIE_SECURE", True)        # false only for plain-HTTP LAN testing
ICS_TOKEN = os.getenv("BT_ICS_TOKEN", "")              # blank disables calendar feed
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("BT_ALLOWED_ORIGINS", "").split(",") if o.strip()]
CHECK_INTERVAL_MIN = _int("BT_CHECK_INTERVAL_MIN", 15)
RATE_LIMIT_PER_MIN = _int("BT_RATE_LIMIT_PER_MIN", 300)
# Browser API traffic must use the encrypted channel (/api/hs + /api/x). Bearer-token scripts are exempt.
REQUIRE_CHANNEL = _bool("BT_REQUIRE_CHANNEL", True)

# First-run defaults for DB-stored settings (editable later in Admin > Settings)
DEFAULTS = {
    "base_url": os.getenv("BT_BASE_URL", ""),
    "timezone": os.getenv("BT_TZ", "America/Chicago"),
    "week_start": _int("BT_WEEK_START", 0),
    "digest_weekday": _int("BT_DIGEST_WEEKDAY", 0),
    "digest_hour": _int("BT_DIGEST_HOUR", 7),
    "smtp_host": os.getenv("BT_SMTP_HOST", ""),
    "smtp_port": _int("BT_SMTP_PORT", 587),
    "smtp_tls": os.getenv("BT_SMTP_TLS", "starttls"),
    "smtp_auth": os.getenv("BT_SMTP_AUTH", "password"),   # password | none (IP-allow-listed relay)
    "smtp_helo": os.getenv("BT_SMTP_HELO", ""),
    "smtp_user": os.getenv("BT_SMTP_USER", ""),
    "smtp_pass": os.getenv("BT_SMTP_PASS", ""),
    "smtp_from": os.getenv("BT_SMTP_FROM", os.getenv("BT_SMTP_USER", "")),
    "ntfy_server": os.getenv("BT_NTFY_SERVER", "https://ntfy.sh"),
    "ntfy_token": os.getenv("BT_NTFY_TOKEN", ""),
    "plaid_env": os.getenv("BT_PLAID_ENV", "production"),
    "plaid_client_id": os.getenv("BT_PLAID_CLIENT_ID", ""),
    "plaid_secret": os.getenv("BT_PLAID_SECRET", ""),
    "plaid_sync_hours": int(os.getenv("BT_PLAID_SYNC_HOURS", "6") or 6),
    "require_2fa": _bool("BT_REQUIRE_2FA", False),
    "session_days": _int("BT_SESSION_DAYS", 30),
    "idle_days": _int("BT_IDLE_DAYS", 7),
}
