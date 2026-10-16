"""Login (password + optional TOTP), logout, password reset by email, invite acceptance."""
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, StringConstraints

from .. import crypto, notify, settings, totp
from ..db import tx
from ..security import (COOKIE, LOCK_AFTER, LOCK_MIN, audit, check_pw, client_ip, forgot_ip, hash_pw, iso,
                        login_ip, mfa_done, mfa_ip, mfa_start, mfa_user, new_session, now_utc)
from .. import config

r = APIRouter(prefix="/api", tags=["auth"])
Str64 = Annotated[str, StringConstraints(max_length=64)]
Str256 = Annotated[str, StringConstraints(max_length=256)]


class Login(BaseModel):
    username: Str64
    password: Str256


class MfaIn(BaseModel):
    mfa_token: Str256
    code: Annotated[str, StringConstraints(max_length=32)]


def _set_cookie(resp: Response, tok: str):
    resp.set_cookie(COOKIE, tok, max_age=settings.get("session_days") * 86400, httponly=True,
                    secure=config.COOKIE_SECURE, samesite="strict", path="/")


def _finish(con, request, resp, user):
    con.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (user["id"],))
    _set_cookie(resp, new_session(con, user["id"], client_ip(request), request.headers.get("user-agent", "")))
    audit(con, request, "login", user_id=user["id"], username=user["username"])


@r.post("/login")
def login(body: Login, request: Request, response: Response):
    ip = client_ip(request)
    if login_ip.blocked(ip):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    with tx() as con:
        u = con.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (body.username.strip(),)).fetchone()
        ok = check_pw(u["pw_hash"] if u else None, body.password)
        locked = u and u["locked_until"] and u["locked_until"] > iso(now_utc())
        if not ok or not u or u["disabled"] or locked:
            login_ip.hit(ip)
            if u and not locked:
                n = u["failed_logins"] + 1
                until = iso(now_utc() + timedelta(minutes=LOCK_MIN)) if n >= LOCK_AFTER else None
                con.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?",
                            (0 if until else n, until, u["id"]))
                if until:
                    audit(con, request, "account locked", u["username"], user_id=u["id"], username=u["username"])
            audit(con, request, "login failed", body.username[:64], user_id=u["id"] if u else None,
                  username=u["username"] if u else "")
            failed = True  # committed below before raising (raising inside tx would roll back the lockout)
        else:
            failed = False
            if ph_needs_rehash(u["pw_hash"]):
                con.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(body.password), u["id"]))
            if u["totp_enabled"]:
                return {"mfa_required": True, "mfa_token": mfa_start(u["id"])}
            _finish(con, request, response, u)
    if failed:
        # Same message for every failure type: no username enumeration
        raise HTTPException(401, "Wrong username or password, or the account is locked")
    return {"ok": True}


def ph_needs_rehash(h: str) -> bool:
    from ..security import ph
    try:
        return ph.check_needs_rehash(h)
    except Exception:
        return False


@r.post("/login/mfa")
def login_mfa(body: MfaIn, request: Request, response: Response):
    ip = client_ip(request)
    if mfa_ip.blocked(ip):
        raise HTTPException(429, "Too many attempts. Try again later.")
    uid = mfa_user(body.mfa_token)
    if not uid:
        raise HTTPException(401, "Sign-in expired. Start again.")
    with tx() as con:
        u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        code = body.code.strip().replace(" ", "")
        ok = False
        if code.isdigit() and len(code) == 6:
            step = totp.verify(crypto.unseal(u["totp_secret_enc"], f"totp:{uid}"), code, u["totp_last_step"])
            if step:
                con.execute("UPDATE users SET totp_last_step=? WHERE id=?", (step, uid))
                ok = True
        else:  # recovery code
            h = crypto.token_hash(code.lower().replace("-", ""))
            rc = con.execute("SELECT id FROM recovery_codes WHERE user_id=? AND code_hash=? AND used_at IS NULL",
                             (uid, h)).fetchone()
            if rc:
                con.execute("UPDATE recovery_codes SET used_at=? WHERE id=?", (iso(now_utc()), rc["id"]))
                audit(con, request, "recovery code used", user_id=uid, username=u["username"])
                ok = True
        if not ok:
            mfa_ip.hit(ip)
            audit(con, request, "2fa failed", user_id=uid, username=u["username"])
        else:
            mfa_done(body.mfa_token)
            _finish(con, request, response, u)
    if not ok:
        raise HTTPException(401, "That code didn't work")
    return {"ok": True}


@r.post("/logout")
def logout(request: Request, response: Response):
    tok = request.cookies.get(COOKIE)
    if tok:
        with tx() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (crypto.token_hash(tok),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


# ---------- password reset ----------
class ForgotIn(BaseModel):
    email: Str256


@r.post("/password/forgot")
def forgot(body: ForgotIn, request: Request):
    """Always returns the same response so it can't be used to discover accounts."""
    ip = client_ip(request)
    if forgot_ip.hit(ip):
        raise HTTPException(429, "Too many requests. Try again later.")
    email = body.email.strip()
    with tx() as con:
        u = con.execute("SELECT * FROM users WHERE email=? COLLATE NOCASE AND disabled=0", (email,)).fetchone()
        if u and notify.email_ready() and settings.get("base_url"):
            tok = crypto.new_token()
            con.execute("INSERT INTO reset_tokens(token_hash,user_id,expires_at) VALUES (?,?,?)",
                        (crypto.token_hash(tok), u["id"], iso(now_utc() + timedelta(minutes=30))))
            try:
                notify.send_email(u["email"], "BillTrack password reset",
                                  f"Someone asked to reset the password for '{u['username']}'.\n\n"
                                  f"Reset it here (valid 30 minutes, one use):\n{settings.get('base_url')}/#reset={tok}\n\n"
                                  "If this wasn't you, ignore this email. Your password hasn't changed.")
            except Exception:
                pass
            audit(con, request, "password reset requested", user_id=u["id"], username=u["username"])
    return {"ok": True, "message": "If that email belongs to an account, a reset link is on its way."}


class ResetIn(BaseModel):
    token: Str256
    password: Str256


@r.post("/password/reset")
def reset(body: ResetIn, request: Request):
    h = crypto.token_hash(body.token)
    with tx() as con:
        t = con.execute("SELECT * FROM reset_tokens WHERE token_hash=? AND used_at IS NULL", (h,)).fetchone()
        if not t or t["expires_at"] < iso(now_utc()):
            raise HTTPException(400, "This reset link is invalid or has expired. Request a new one.")
        try:
            pw = hash_pw(body.password)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        con.execute("UPDATE users SET pw_hash=?, pw_changed_at=?, failed_logins=0, locked_until=NULL WHERE id=?",
                    (pw, iso(now_utc()), t["user_id"]))
        con.execute("UPDATE reset_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL", (iso(now_utc()), t["user_id"]))
        con.execute("DELETE FROM sessions WHERE user_id=?", (t["user_id"],))
        audit(con, request, "password reset", user_id=t["user_id"])
    return {"ok": True}


# ---------- invites ----------
@r.get("/invite/info")
def invite_info(token: Annotated[str, StringConstraints(max_length=256)]):
    with tx() as con:
        i = con.execute("SELECT email, role, expires_at, used_at FROM invites WHERE token_hash=?",
                        (crypto.token_hash(token),)).fetchone()
    if not i or i["used_at"] or i["expires_at"] < iso(now_utc()):
        raise HTTPException(400, "This invite is invalid or has expired")
    return {"email": i["email"], "role": i["role"]}


class AcceptIn(BaseModel):
    token: Str256
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=32,
                                               pattern=r"^[A-Za-z0-9_.\-]+$")]
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] = ""
    password: Str256


@r.post("/invite/accept")
def invite_accept(body: AcceptIn, request: Request, response: Response):
    h = crypto.token_hash(body.token)
    with tx() as con:
        i = con.execute("SELECT * FROM invites WHERE token_hash=?", (h,)).fetchone()
        if not i or i["used_at"] or i["expires_at"] < iso(now_utc()):
            raise HTTPException(400, "This invite is invalid or has expired")
        if con.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (body.username,)).fetchone():
            raise HTTPException(409, "That username is taken")
        try:
            pw = hash_pw(body.password)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        n = iso(now_utc())
        cur = con.execute("INSERT INTO users(username,pw_hash,role,email,display_name,created_at,pw_changed_at) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (body.username, pw, i["role"], i["email"], "".join(c for c in body.display_name if c.isprintable()), n, n))
        con.execute("UPDATE invites SET used_at=? WHERE id=?", (n, i["id"]))
        u = con.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        audit(con, request, "invite accepted", body.username, user_id=u["id"], username=u["username"])
        _finish(con, request, response, u)
    return {"ok": True}
