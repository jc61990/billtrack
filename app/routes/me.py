"""The signed-in user's own profile, security, API tokens, alerts inbox and reminders."""
import json
import secrets
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, StringConstraints

from .. import __version__, config, crypto, ledger, notify, settings, totp
from ..db import rows, tx
from ..recurrence import RECURRENCES
from ..security import (COOKIE, DENYABLE, PERMS, Principal, audit, check_pw, hash_pw, iso, now_utc, principal,
                        require)
from .common import In, Name, Note

r = APIRouter(prefix="/api", tags=["me"])
Self = Depends(require("self"))
SelfSession = Depends(require("self", session_only=True))
Pw = Annotated[str, StringConstraints(max_length=256)]


def _user(con, uid):
    return con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


@r.get("/me")
def me(p: Principal = Self):
    with tx() as con:
        u = _user(con, p.user_id)
        unread = con.execute("SELECT count(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
                             (p.user_id,)).fetchone()[0]
        sess = con.execute("SELECT expires_at, created_at FROM sessions WHERE token_hash=?", (p.session_hash,)).fetchone() \
            if p.session_hash else None
        ends = None
        if sess:
            from datetime import datetime as _dt
            cap = _dt.fromisoformat(sess["created_at"]) + timedelta(minutes=settings.get("session_minutes")) \
                if sess["created_at"] else None
            ends = min(sess["expires_at"], iso(cap)) if cap else sess["expires_at"]
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"], "email": u["email"],
            "has_feed": bool(u["ics_token_hash"]),
            "session_expires_at": ends, "idle_minutes": settings.get("idle_minutes"),
            "session_minutes": settings.get("session_minutes"),
            "role": u["role"], "perms": sorted(p.perms), "totp_enabled": bool(u["totp_enabled"]),
            "mfa_setup_required": p.mfa_setup_required, "prefs": notify.prefs(u), "unread": unread,
            "version": __version__,
            "week_start": notify.prefs(u).get("week_start") if notify.prefs(u).get("week_start") is not None else settings.get("week_start"),
            "status": u["status"], "today": ledger.today().isoformat(),
            "timezone": settings.user_tz_name(notify.prefs(u)) or settings.get("timezone"),
            "system_timezone": settings.get("timezone"), "ics": bool(config.ICS_TOKEN),
            "email_ready": notify.email_ready(), "push_server": settings.get("ntfy_server"), "via": p.via}


class Prefs(In):
    email_alerts: bool = True
    push_alerts: bool = False
    ntfy_topic: Annotated[str, StringConstraints(max_length=64, pattern=r"^[A-Za-z0-9_\-]*$")] = ""
    digest: bool = True
    bill_reminders: bool = True
    due_alerts: bool = True
    overdue_alerts: bool = True
    tracker_alerts: bool = True
    balance_alerts: bool = True
    timezone: Annotated[str, StringConstraints(max_length=64)] = ""   # "" = system default
    tz_auto: bool = True                                               # follow this browser's zone
    week_start: Annotated[int, Field(ge=0, le=6)] | None = None        # this user's pay week; None = system default


class MeIn(In):
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] = ""
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)] = ""
    current_password: Pw | None = None
    prefs: Prefs


@r.put("/me")
def update_me(body: MeIn, request: Request, p: Principal = SelfSession):
    if body.prefs.timezone and not settings.valid_tz(body.prefs.timezone):
        raise HTTPException(422, "Unknown time zone")
    from ..security import clear_tz_cache
    clear_tz_cache()
    with tx() as con:
        u = _user(con, p.user_id)
        if body.email != u["email"]:
            # Changing where reset links go is security-sensitive: re-authenticate
            if not check_pw(u["pw_hash"], body.current_password or ""):
                raise HTTPException(403, "Enter your current password to change your email")
            if body.email and not notify.valid_email(body.email):
                raise HTTPException(422, "That email address doesn't look right")
            if body.email and con.execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE AND id<>?",
                                          (body.email, p.user_id)).fetchone():
                raise HTTPException(409, "That email is used by another account")
            audit(con, request, "email changed")
        con.execute("UPDATE users SET display_name=?, email=?, prefs=? WHERE id=?",
                    (body.display_name, body.email, json.dumps(body.prefs.model_dump()), p.user_id))
    return {"ok": True}


@r.post("/me/session/extend")
def extend_session(request: Request, response: Response, p: Principal = SelfSession):
    """'Stay signed in': replace this session with a brand-new one (new token, fresh time limit). The old token stops
    working immediately, so an extension never leaves a longer-lived copy of the cookie around."""
    from ..routes.auth import _set_cookie
    from ..security import client_ip, new_session
    with tx() as con:
        tok = new_session(con, p.user_id, client_ip(request), request.headers.get("user-agent", ""))
        con.execute("DELETE FROM sessions WHERE token_hash=?", (p.session_hash,))
        exp = con.execute("SELECT expires_at FROM sessions WHERE token_hash=?", (crypto.token_hash(tok),)).fetchone()[0]
        audit(con, request, "session extended")
    _set_cookie(response, tok)
    return {"session_expires_at": exp}


@r.post("/me/calendar-feed")
def new_feed(request: Request, p: Principal = Depends(require("finance:read", session_only=True))):
    """Private calendar subscription URL for YOUR bills and paydays. Shown once; making a new one revokes the old."""
    tok = crypto.new_token()
    with tx() as con:
        con.execute("UPDATE users SET ics_token_hash=? WHERE id=?", (crypto.token_hash(tok), p.user_id))
        audit(con, request, "calendar feed created")
    base = settings.get("base_url") or ""
    return {"url": f"{base}/cal/{tok}.ics", "path": f"/cal/{tok}.ics"}


@r.delete("/me/calendar-feed")
def drop_feed(request: Request, p: Principal = SelfSession):
    with tx() as con:
        con.execute("UPDATE users SET ics_token_hash=NULL WHERE id=?", (p.user_id,))
        audit(con, request, "calendar feed revoked")
    return {"ok": True}


class TzIn(In):
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)]


@r.put("/me/timezone")
def set_timezone(body: TzIn, p: Principal = Self):
    """Called by the browser when it notices its zone differs (only if the user lets it follow the device)."""
    if not settings.valid_tz(body.timezone):
        raise HTTPException(422, "Unknown time zone")
    with tx() as con:
        u = _user(con, p.user_id)
        pr = notify.prefs(u)
        if not pr.get("tz_auto", True):
            return {"ok": False, "reason": "time zone is set manually"}
        pr["timezone"] = body.timezone
        con.execute("UPDATE users SET prefs=? WHERE id=?", (json.dumps(pr), p.user_id))
    from ..security import clear_tz_cache
    clear_tz_cache()
    return {"ok": True}


class PwIn(In):
    current_password: Pw
    new_password: Pw


@r.post("/me/password")
def change_password(body: PwIn, request: Request, response: Response, p: Principal = SelfSession):
    with tx() as con:
        u = _user(con, p.user_id)
        if not check_pw(u["pw_hash"], body.current_password):
            raise HTTPException(403, "Current password is wrong")
        try:
            h = hash_pw(body.new_password)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        con.execute("UPDATE users SET pw_hash=?, pw_changed_at=? WHERE id=?", (h, iso(now_utc()), p.user_id))
        # sign out every other session
        con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (p.user_id, p.session_hash))
        audit(con, request, "password changed")
    return {"ok": True}


# ---------- 2FA ----------
@r.post("/me/2fa/setup")
def totp_setup(request: Request, p: Principal = SelfSession):
    with tx() as con:
        u = _user(con, p.user_id)
        if u["totp_enabled"]:
            raise HTTPException(409, "Two-factor is already on. Turn it off first to re-enroll.")
        secret = totp.new_secret()
        con.execute("UPDATE users SET totp_secret_enc=? WHERE id=?", (crypto.seal(secret, f"totp:{p.user_id}"), p.user_id))
    return {"secret": secret, "uri": totp.uri(secret, u["username"])}


class CodeIn(In):
    code: Annotated[str, StringConstraints(max_length=12)]


@r.post("/me/2fa/enable")
def totp_enable(body: CodeIn, request: Request, p: Principal = SelfSession):
    with tx() as con:
        u = _user(con, p.user_id)
        if u["totp_enabled"] or not u["totp_secret_enc"]:
            raise HTTPException(409, "Start setup first")
        step = totp.verify(crypto.unseal(u["totp_secret_enc"], f"totp:{p.user_id}"), body.code)
        if not step:
            raise HTTPException(422, "That code didn't match. Check your phone's time and try again.")
        codes = [secrets.token_hex(5) for _ in range(10)]
        con.execute("DELETE FROM recovery_codes WHERE user_id=?", (p.user_id,))
        con.executemany("INSERT INTO recovery_codes(user_id,code_hash) VALUES (?,?)",
                        [(p.user_id, crypto.token_hash(c)) for c in codes])
        con.execute("UPDATE users SET totp_enabled=1, totp_last_step=? WHERE id=?", (step, p.user_id))
        audit(con, request, "2fa enabled")
    return {"recovery_codes": [f"{c[:5]}-{c[5:]}" for c in codes]}


class DisableIn(In):
    password: Pw


@r.post("/me/2fa/disable")
def totp_disable(body: DisableIn, request: Request, p: Principal = SelfSession):
    with tx() as con:
        u = _user(con, p.user_id)
        if not check_pw(u["pw_hash"], body.password):
            raise HTTPException(403, "Password is wrong")
        if settings.get("require_2fa"):
            raise HTTPException(403, "Your administrator requires two-factor authentication")
        con.execute("UPDATE users SET totp_enabled=0, totp_secret_enc='', totp_last_step=0 WHERE id=?", (p.user_id,))
        con.execute("DELETE FROM recovery_codes WHERE user_id=?", (p.user_id,))
        audit(con, request, "2fa disabled")
    return {"ok": True}


# ---------- sessions ----------
@r.get("/me/sessions")
def my_sessions(p: Principal = SelfSession):
    with tx() as con:
        out = rows(con, "SELECT rowid AS id, token_hash, created_at, last_seen, ip, user_agent FROM sessions "
                        "WHERE user_id=? ORDER BY last_seen DESC", (p.user_id,))
    for s in out:
        s["current"] = s.pop("token_hash") == p.session_hash
    return out


@r.delete("/me/sessions/{sid}")
def end_session(sid: int, request: Request, p: Principal = SelfSession):
    with tx() as con:
        con.execute("DELETE FROM sessions WHERE rowid=? AND user_id=?", (sid, p.user_id))
        audit(con, request, "session revoked", sid)
    return {"ok": True}


@r.post("/me/sessions/revoke-others")
def end_other_sessions(request: Request, p: Principal = SelfSession):
    with tx() as con:
        con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (p.user_id, p.session_hash))
        audit(con, request, "other sessions revoked")
    return {"ok": True}


# ---------- API tokens ----------
class TokenIn(In):
    name: Name
    scope: Literal["read", "write"] = "read"
    expires_days: Annotated[int, Field(ge=0, le=365)] = 90   # 0 = never


@r.get("/me/tokens")
def my_tokens(p: Principal = SelfSession):
    with tx() as con:
        return rows(con, "SELECT id, name, prefix, scope, created_at, expires_at, last_used_at FROM api_tokens "
                         "WHERE user_id=? AND revoked=0 ORDER BY id DESC", (p.user_id,))


@r.post("/me/tokens", status_code=201)
def create_token(body: TokenIn, request: Request, p: Principal = SelfSession):
    if p.mfa_setup_required:
        raise HTTPException(403, "Set up two-factor authentication first")
    tok = crypto.new_token("bt")
    exp = iso(now_utc() + timedelta(days=body.expires_days)) if body.expires_days else None
    with tx() as con:
        cur = con.execute("INSERT INTO api_tokens(user_id,name,token_hash,prefix,scope,created_at,expires_at) "
                          "VALUES (?,?,?,?,?,?,?)", (p.user_id, body.name, crypto.token_hash(tok), tok[:10],
                                                     body.scope, iso(now_utc()), exp))
        audit(con, request, "api token created", body.name, f"scope={body.scope}")
    return {"id": cur.lastrowid, "token": tok, "note": "Copy this now. It won't be shown again."}


@r.delete("/me/tokens/{tid}")
def revoke_token(tid: int, request: Request, p: Principal = SelfSession):
    with tx() as con:
        con.execute("UPDATE api_tokens SET revoked=1 WHERE id=? AND user_id=?", (tid, p.user_id))
        audit(con, request, "api token revoked", tid)
    return {"ok": True}


# ---------- inbox ----------
@r.get("/me/notifications")
def notifications(p: Principal = Self):
    with tx() as con:
        return rows(con, "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 200", (p.user_id,))


class ReadIn(In):
    ids: Annotated[list[int], Field(max_length=500)] = []
    all: bool = False


@r.post("/me/notifications/read")
def mark_read(body: ReadIn, p: Principal = Self):
    with tx() as con:
        if body.all:
            con.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (iso(now_utc()), p.user_id))
        for i in body.ids:
            con.execute("UPDATE notifications SET read_at=? WHERE id=? AND user_id=?", (iso(now_utc()), i, p.user_id))
    return {"ok": True}


@r.delete("/me/notifications")
def clear_notifications(p: Principal = Self):
    with tx() as con:
        con.execute("DELETE FROM notifications WHERE user_id=? AND read_at IS NOT NULL", (p.user_id,))
    return {"ok": True}


@r.post("/me/notify-test")
def notify_test(p: Principal = SelfSession):
    with tx() as con:
        u = dict(_user(con, p.user_id))
        if "finance:read" in p.perms:
            subj, body = notify.digest(con, ledger.today())
        else:
            subj, body = "Test alert", "Alerts are working."
        notify.inbox(con, u["id"], "[Test] " + subj, body)
    # send after the transaction is committed: a slow mail server never holds a database lock
    return notify.deliver(None, u, "[Test] " + subj, body, wait=True)


# ---------- reminders ----------
class ReminderIn(In):
    title: Name
    note: Note = ""
    next_at: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")]
    recurrence: Literal[RECURRENCES] = "once"  # type: ignore[valid-type]
    email: bool = True
    push: bool = True
    active: bool = True


def _check_dt(s):
    try:
        datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, "Invalid date/time") from None


@r.get("/reminders/occurrences")
def reminder_occurrences(start: date, end: date, p: Principal = Depends(require("reminders"))):
    """Your active reminders expanded onto dates, for the calendar and week views."""
    if end < start or (end - start).days > 400:
        raise HTTPException(422, "Range must be 0-400 days")
    from ..recurrence import occurrences
    out = []
    with tx() as con:
        for r_ in rows(con, "SELECT * FROM reminders WHERE user_id=? AND active=1", (p.user_id,)):
            first = datetime.fromisoformat(r_["next_at"])
            for d in occurrences(first.date(), r_["recurrence"], start, end):
                out.append({"type": "reminder", "id": r_["id"], "title": r_["title"], "note": r_["note"],
                            "date": d.isoformat(), "time": r_["next_at"][11:16], "recurrence": r_["recurrence"],
                            "is_next": d == first.date(), "amount_cents": 0})
    return sorted(out, key=lambda x: (x["date"], x["time"]))


@r.get("/reminders")
def list_reminders(p: Principal = Depends(require("reminders"))):
    with tx() as con:
        return rows(con, "SELECT * FROM reminders WHERE user_id=? ORDER BY active DESC, next_at", (p.user_id,))


@r.post("/reminders", status_code=201)
def create_reminder(body: ReminderIn, p: Principal = Depends(require("reminders"))):
    _check_dt(body.next_at)
    with tx() as con:
        cur = con.execute("INSERT INTO reminders(user_id,title,note,next_at,recurrence,email,push,active) "
                          "VALUES (?,?,?,?,?,?,?,?)", (p.user_id, body.title, body.note, body.next_at,
                                                       body.recurrence, int(body.email), int(body.push), int(body.active)))
    return {"id": cur.lastrowid}


@r.put("/reminders/{rid}")
def update_reminder(rid: int, body: ReminderIn, p: Principal = Depends(require("reminders"))):
    _check_dt(body.next_at)
    with tx() as con:
        n = con.execute("UPDATE reminders SET title=?,note=?,next_at=?,recurrence=?,email=?,push=?,active=? "
                        "WHERE id=? AND user_id=?", (body.title, body.note, body.next_at, body.recurrence,
                                                     int(body.email), int(body.push), int(body.active), rid, p.user_id)).rowcount
    if not n:
        raise HTTPException(404, "Not found")
    return {"ok": True}


@r.delete("/reminders/{rid}")
def delete_reminder(rid: int, p: Principal = Depends(require("reminders"))):
    with tx() as con:
        con.execute("DELETE FROM reminders WHERE id=? AND user_id=?", (rid, p.user_id))
    return {"ok": True}
