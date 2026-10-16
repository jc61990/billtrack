"""Admin portal API: users, roles/permissions, invites, system settings, audit log, tokens, backup."""
import json
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import Field, StringConstraints

from .. import __version__, crypto, db, ledger, notify, settings
from ..db import rows, tx
from ..security import DENYABLE, PERMS, ROLE_PERMS, Principal, audit, hash_pw, iso, now_utc, require
from .common import In

r = APIRouter(prefix="/api/admin", tags=["admin"])
Admin = Depends(require("admin", session_only=True))
Role = Literal["admin", "member", "viewer"]
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=32,
                                            pattern=r"^[A-Za-z0-9_.\-]+$")]
Email = Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)]


def _admins(con) -> int:
    return con.execute("SELECT count(*) FROM users WHERE role='admin' AND disabled=0").fetchone()[0]


@r.get("/meta")
def meta(p: Principal = Admin):
    con = db.connect()
    try:
        cipher = {k: con.execute(f"PRAGMA {k}").fetchone()[0] for k in ("cipher_version", "cipher_provider_version")}
    finally:
        con.close()
    return {"roles": {k: sorted(v) for k, v in ROLE_PERMS.items()}, "perms": PERMS, "denyable": DENYABLE,
            "version": __version__, **cipher}


@r.get("/users")
def users(p: Principal = Admin):
    with tx() as con:
        out = rows(con, """SELECT u.id, u.username, u.display_name, u.email, u.role, u.disabled, u.totp_enabled,
                           u.created_at, u.locked_until, u.perms_deny,
                           (SELECT max(last_seen) FROM sessions s WHERE s.user_id=u.id) last_seen,
                           (SELECT count(*) FROM sessions s WHERE s.user_id=u.id) sessions
                           FROM users u ORDER BY u.username""")
    for u in out:
        u["perms_deny"] = json.loads(u["perms_deny"] or "[]")
    return out


class UserCreate(In):
    username: Username
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] = ""
    email: Email = ""
    role: Role = "member"
    password: Annotated[str, StringConstraints(max_length=256)]


@r.post("/users", status_code=201)
def create_user(body: UserCreate, request: Request, p: Principal = Admin):
    if body.email and not notify.valid_email(body.email):
        raise HTTPException(422, "Invalid email")
    try:
        h = hash_pw(body.password)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    with tx() as con:
        if con.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (body.username,)).fetchone():
            raise HTTPException(409, "Username taken")
        n = iso(now_utc())
        cur = con.execute("INSERT INTO users(username,pw_hash,role,email,display_name,created_at,pw_changed_at) "
                          "VALUES (?,?,?,?,?,?,?)", (body.username, h, body.role, body.email, body.display_name, n, n))
        audit(con, request, "user created", body.username, f"role={body.role}")
    return {"id": cur.lastrowid}


class UserUpdate(In):
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] = ""
    email: Email = ""
    role: Role
    disabled: bool = False
    perms_deny: Annotated[list[str], Field(max_length=20)] = []


@r.put("/users/{uid}")
def update_user(uid: int, body: UserUpdate, request: Request, p: Principal = Admin):
    if body.email and not notify.valid_email(body.email):
        raise HTTPException(422, "Invalid email")
    bad = set(body.perms_deny) - set(DENYABLE)
    if bad:
        raise HTTPException(422, f"Can't remove: {', '.join(sorted(bad))}")
    with tx() as con:
        u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404, "Not found")
        if u["role"] == "admin" and (body.role != "admin" or body.disabled) and _admins(con) <= 1:
            raise HTTPException(409, "Keep at least one active admin")
        if uid == p.user_id and (body.role != "admin" or body.disabled):
            raise HTTPException(409, "You can't demote or disable yourself")
        con.execute("UPDATE users SET display_name=?, email=?, role=?, disabled=?, perms_deny=? WHERE id=?",
                    (body.display_name, body.email, body.role, int(body.disabled),
                     json.dumps(sorted(set(body.perms_deny))), uid))
        if body.disabled:
            con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            con.execute("UPDATE api_tokens SET revoked=1 WHERE user_id=?", (uid,))
        audit(con, request, "user updated", u["username"],
              f"role={body.role} disabled={body.disabled} deny={','.join(body.perms_deny)}")
    return {"ok": True}


@r.delete("/users/{uid}")
def delete_user(uid: int, request: Request, p: Principal = Admin):
    with tx() as con:
        u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404, "Not found")
        if uid == p.user_id:
            raise HTTPException(409, "You can't delete yourself")
        if u["role"] == "admin" and _admins(con) <= 1:
            raise HTTPException(409, "Keep at least one active admin")
        con.execute("DELETE FROM users WHERE id=?", (uid,))
        audit(con, request, "user deleted", u["username"])
    return {"ok": True}


class SetPw(In):
    password: Annotated[str, StringConstraints(max_length=256)]


@r.post("/users/{uid}/password")
def set_password(uid: int, body: SetPw, request: Request, p: Principal = Admin):
    try:
        h = hash_pw(body.password)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    with tx() as con:
        u = con.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404, "Not found")
        con.execute("UPDATE users SET pw_hash=?, pw_changed_at=?, failed_logins=0, locked_until=NULL WHERE id=?",
                    (h, iso(now_utc()), uid))
        con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit(con, request, "password set by admin", u["username"])
    return {"ok": True}


@r.post("/users/{uid}/send-reset")
def send_reset(uid: int, request: Request, p: Principal = Admin):
    with tx() as con:
        u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u or not u["email"]:
            raise HTTPException(422, "User has no email address")
        if not (notify.email_ready() and settings.get("base_url")):
            raise HTTPException(422, "Set up email and the public URL in Settings first")
        tok = crypto.new_token()
        con.execute("INSERT INTO reset_tokens(token_hash,user_id,expires_at) VALUES (?,?,?)",
                    (crypto.token_hash(tok), uid, iso(now_utc() + timedelta(hours=24))))
        notify.send_email(u["email"], "Set your BillTrack password",
                          f"An administrator asked you to set a new password for '{u['username']}'.\n\n"
                          f"{settings.get('base_url')}/#reset={tok}\n\nThis link works once and expires in 24 hours.")
        audit(con, request, "reset email sent", u["username"])
    return {"ok": True}


@r.post("/users/{uid}/unlock")
def unlock(uid: int, request: Request, p: Principal = Admin):
    with tx() as con:
        con.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (uid,))
        audit(con, request, "user unlocked", uid)
    return {"ok": True}


@r.post("/users/{uid}/reset-2fa")
def reset_2fa(uid: int, request: Request, p: Principal = Admin):
    with tx() as con:
        con.execute("UPDATE users SET totp_enabled=0, totp_secret_enc='', totp_last_step=0 WHERE id=?", (uid,))
        con.execute("DELETE FROM recovery_codes WHERE user_id=?", (uid,))
        con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit(con, request, "2fa reset by admin", uid)
    return {"ok": True}


@r.post("/users/{uid}/revoke-sessions")
def revoke_sessions(uid: int, request: Request, p: Principal = Admin):
    with tx() as con:
        con.execute("DELETE FROM sessions WHERE user_id=?" + (" AND token_hash<>?" if uid == p.user_id else ""),
                    (uid, p.session_hash) if uid == p.user_id else (uid,))
        audit(con, request, "sessions revoked", uid)
    return {"ok": True}


# ---------- invites ----------
class InviteIn(In):
    email: Email
    role: Role = "member"
    send: bool = True


@r.get("/invites")
def invites(p: Principal = Admin):
    with tx() as con:
        return rows(con, "SELECT id, email, role, created_at, expires_at, used_at FROM invites ORDER BY id DESC LIMIT 100")


@r.post("/invites", status_code=201)
def create_invite(body: InviteIn, request: Request, p: Principal = Admin):
    if not notify.valid_email(body.email):
        raise HTTPException(422, "Invalid email")
    if not settings.get("base_url"):
        raise HTTPException(422, "Set the public URL (Admin > Settings) first so invite links work")
    tok = crypto.new_token()
    link = f"{settings.get('base_url')}/#invite={tok}"
    sent = False
    with tx() as con:
        con.execute("INSERT INTO invites(token_hash,email,role,created_by,created_at,expires_at) VALUES (?,?,?,?,?,?)",
                    (crypto.token_hash(tok), body.email, body.role, p.user_id, iso(now_utc()),
                     iso(now_utc() + timedelta(days=3))))
        if body.send and notify.email_ready():
            try:
                sent = notify.send_email(body.email, "You're invited to BillTrack",
                                         f"You've been invited to BillTrack as a {body.role}.\n\n"
                                         f"Create your login here (expires in 3 days):\n{link}")
            except Exception as e:
                raise HTTPException(502, f"Invite saved but email failed: {type(e).__name__}") from None
        audit(con, request, "invite created", body.email, f"role={body.role} emailed={sent}")
    return {"link": link, "emailed": sent}


@r.delete("/invites/{iid}")
def delete_invite(iid: int, request: Request, p: Principal = Admin):
    with tx() as con:
        con.execute("DELETE FROM invites WHERE id=?", (iid,))
        audit(con, request, "invite revoked", iid)
    return {"ok": True}


# ---------- settings ----------
@r.get("/settings")
def get_settings(p: Principal = Admin):
    return settings.all_public()


@r.put("/settings")
def put_settings(body: Annotated[dict, Field(max_length=40)], request: Request, p: Principal = Admin):
    vals = {k: v for k, v in body.items() if not (k in settings.SECRET and v in (None, "********"))}
    try:
        changed = settings.update(vals)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e)) from None
    with tx() as con:
        audit(con, request, "settings changed", ",".join(changed))
    return settings.all_public()


@r.post("/settings/test-email")
def test_email(request: Request, p: Principal = Admin):
    with tx() as con:
        u = con.execute("SELECT email FROM users WHERE id=?", (p.user_id,)).fetchone()
    if not u["email"]:
        raise HTTPException(422, "Add your own email in Settings first")
    try:
        ok = notify.send_email(u["email"], "BillTrack test email", "Email delivery is working.")
    except Exception as e:
        raise HTTPException(502, f"Send failed: {type(e).__name__}: {str(e)[:200]}") from None
    if not ok:
        raise HTTPException(422, "Email isn't configured (host and from address required)")
    return {"ok": True}


# ---------- audit / tokens / backup ----------
@r.get("/audit")
def audit_log(q: Annotated[str, Query(max_length=60)] = "", before: int | None = None,
              limit: Annotated[int, Query(ge=1, le=500)] = 100, p: Principal = Admin):
    sql, args = "SELECT * FROM audit_log WHERE 1=1", []
    if q:
        sql += " AND (action LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\' OR target LIKE ? ESCAPE '\\' OR ip LIKE ? ESCAPE '\\')"
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args += [like] * 4
    if before:
        sql += " AND id < ?"
        args.append(before)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with tx() as con:
        return rows(con, sql, args)


@r.get("/tokens")
def all_tokens(p: Principal = Admin):
    with tx() as con:
        return rows(con, """SELECT t.id, t.name, t.prefix, t.scope, t.created_at, t.expires_at, t.last_used_at,
                            u.username FROM api_tokens t JOIN users u ON u.id=t.user_id WHERE t.revoked=0
                            ORDER BY t.id DESC""")


@r.delete("/tokens/{tid}")
def admin_revoke_token(tid: int, request: Request, p: Principal = Admin):
    with tx() as con:
        con.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (tid,))
        audit(con, request, "api token revoked by admin", tid)
    return {"ok": True}


class BackupIn(In):
    passphrase: Annotated[str, StringConstraints(min_length=12, max_length=256)]


@r.post("/backup")
def backup(body: BackupIn, request: Request, p: Principal = Admin):
    data = db.backup_bytes(body.passphrase)
    with tx() as con:
        audit(con, request, "backup downloaded")
    name = f"billtrack-backup-{ledger.today().isoformat()}.db"
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
