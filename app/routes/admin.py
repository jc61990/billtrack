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
Role = Literal["admin", "user"]
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=32,
                                            pattern=r"^[A-Za-z0-9_.\-]+$")]
Email = Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)]


def _admins(con) -> int:
    return con.execute("SELECT count(*) FROM users WHERE role='admin' AND disabled=0").fetchone()[0]


# ---------- system & maintenance ----------
import os
import shutil
import tempfile
import time as _time
from datetime import datetime as _dt


def _meta_get(con, key):
    r_ = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r_[0]) if r_ else None


def _meta_set(con, key, val):
    con.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(val)))


def _fsize(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


@r.get("/system")
def system_info(p: Principal = Admin):
    from .. import config
    dbp = config.DB_PATH
    bdir = db.backup_dir()
    du = shutil.disk_usage(os.path.dirname(os.path.abspath(dbp)))
    with tx() as con:
        pc = con.execute("PRAGMA page_count").fetchone()[0]
        ps = con.execute("PRAGMA page_size").fetchone()[0]
        free = con.execute("PRAGMA freelist_count").fetchone()[0]
        tables = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in (
            "bills", "payments", "expenses", "income_extra", "income_received", "accounts", "account_history",
            "networth_snapshots", "notifications", "audit_log", "plaid_txns", "users", "sessions")}
        last = {k: _meta_get(con, f"maint:{k}") for k in ("compact", "integrity", "optimize", "checkpoint", "prune")}
        cipher = {k: con.execute(f"PRAGMA {k}").fetchone()[0] for k in ("cipher_version", "cipher_provider_version")}
    backups = list_backups()
    from .. import keys as _keys
    keymode = "passphrase" if _keys.enabled() else "keyfile"
    fp = _keys.fingerprint(bytes.fromhex(db.load_key())) if keymode == "passphrase" else None
    return {"version": __version__, **cipher, "key_mode": keymode, "key_fingerprint": fp, "db_path": dbp, "backup_dir": bdir,
            "db_bytes": _fsize(dbp), "wal_bytes": _fsize(dbp + "-wal"), "page_size": ps, "pages": pc,
            "free_bytes": free * ps, "backups_bytes": sum(b["bytes"] for b in backups), "backups": len(backups),
            "disk_total": du.total, "disk_used": du.used, "disk_free": du.free, "rows": tables, "last": last}


Op = Literal["compact", "integrity", "optimize", "checkpoint", "prune"]


@r.post("/maintenance/{op}")
def maintenance(op: Op, request: Request, p: Principal = Admin):
    t0 = _time.monotonic()
    res: dict = {}
    try:
        if op == "compact":                         # rebuild file: removes free pages ("defrag")
            before, after = db.vacuum()
            res = {"before": before, "after": after, "saved": max(0, before - after)}
        else:
            con = db.connect()
            try:
                if op == "integrity":
                    out = [r_[0] for r_ in con.execute("PRAGMA integrity_check").fetchall()]
                    fk = con.execute("PRAGMA foreign_key_check").fetchall()
                    cv = [r_[0] for r_ in con.execute("PRAGMA cipher_integrity_check").fetchall()]
                    res = {"ok": out == ["ok"] and not fk and not cv, "integrity": out[:10],
                           "foreign_key_problems": len(fk), "cipher_hmac_problems": len(cv)}
                elif op == "optimize":                # refresh query-planner statistics
                    con.execute("ANALYZE"); con.execute("PRAGMA optimize")
                    res = {"ok": True}
                elif op == "checkpoint":              # fold the write-ahead log into the main file
                    busy, logp, ckp = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    res = {"ok": not busy, "frames": logp}
                elif op == "prune":                   # trim history beyond what any screen uses
                    cut = lambda d: (now_utc() - timedelta(days=d)).isoformat(timespec="seconds")
                    res = {
                        "read_notifications": con.execute("DELETE FROM notifications WHERE read_at IS NOT NULL AND ts < ?", (cut(30),)).rowcount,
                        "expired_sessions": con.execute("DELETE FROM sessions WHERE expires_at < ?", (cut(0),)).rowcount,
                        "used_tokens": con.execute("DELETE FROM reset_tokens WHERE used_at IS NOT NULL OR expires_at < ?", (cut(0),)).rowcount,
                        "old_invites": con.execute("DELETE FROM invites WHERE expires_at < ?", (cut(30),)).rowcount,
                    }
                con.commit()
            finally:
                con.close()
    except Exception as e:
        msg = str(e)
        if "locked" in msg or "busy" in msg:
            raise HTTPException(409, "The database is busy right now. Try again in a moment.") from None
        raise HTTPException(500, f"{op} failed: {type(e).__name__}") from None
    res["seconds"] = round(_time.monotonic() - t0, 2)
    res["at"] = iso(now_utc())
    with tx() as con:
        _meta_set(con, f"maint:{op}", res)
        audit(con, request, f"maintenance {op}", detail=json.dumps(res)[:400])
    return res


# ---------- on-server backups ----------
def _kind(name):
    g = db.snap_group(name)
    return {"pre": "Upgrade", "manual": "Manual", "safety": "Before restore"}.get(g, g or "Other")


def list_backups():
    d = db.backup_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d), reverse=True):
        if db.SNAP_RE.match(f):
            st = os.stat(os.path.join(d, f))
            out.append({"name": f, "bytes": st.st_size, "kind": _kind(f),
                        "created": _dt.strptime(f[10:25], "%Y%m%d-%H%M%S").isoformat(timespec="seconds")})
    return out


def _backup_path(name: str) -> str:
    if not db.SNAP_RE.match(name) or os.path.basename(name) != name:   # no paths, no traversal
        raise HTTPException(404, "No such backup")
    path = os.path.join(db.backup_dir(), name)
    if not os.path.isfile(path):
        raise HTTPException(404, "No such backup")
    return path


@r.get("/backups")
def backups(p: Principal = Admin):
    return list_backups()


class BackupNowIn(In):
    keep: Annotated[int, Field(ge=1, le=60)] = 7


@r.post("/backups", status_code=201)
def backup_now(body: BackupNowIn, request: Request, p: Principal = Admin):
    du = shutil.disk_usage(db.backup_dir() if os.path.isdir(db.backup_dir()) else os.path.dirname(db.backup_dir()))
    from .. import config
    if du.free < _fsize(config.DB_PATH) * 2 + 10 * 1024 * 1024:
        raise HTTPException(507, "Not enough free disk space for a backup")
    try:
        path, removed = db.snapshot(db.backup_dir(), body.keep, "manual")
    except Exception as e:
        raise HTTPException(500, f"Backup failed: {type(e).__name__}") from None
    with tx() as con:
        audit(con, request, "backup created (server)", os.path.basename(path), f"rotated {len(removed)}")
    return {"name": os.path.basename(path), "rotated": removed}


@r.delete("/backups/{name}")
def delete_backup(name: str, request: Request, p: Principal = Admin):
    os.remove(_backup_path(name))
    with tx() as con:
        audit(con, request, "backup deleted", name)
    return {"ok": True}


class ConfirmIn(In):
    confirm: Literal["RESTORE"]


@r.post("/backups/{name}/restore")
def restore_backup(name: str, body: ConfirmIn, request: Request, p: Principal = Admin):
    path = _backup_path(name)
    try:
        db.connect(path).close()                      # opens with this server's key?
    except db.KeyError_:
        raise HTTPException(422, "This backup can't be opened with this server's key") from None
    safety, _ = db.snapshot(db.backup_dir(), 7, "safety")   # undo point, in case
    with tx() as con:
        audit(con, request, "backup restore started", name, f"safety copy {os.path.basename(safety)}")
    db.restore_live(path)
    db.init()                                         # bring an older backup's schema up to date
    settings.reset_cache()
    with tx() as con:
        audit(con, request, "backup restored", name, f"safety copy {os.path.basename(safety)}")
    return {"ok": True, "safety": os.path.basename(safety)}


class DlIn(In):
    password: Annotated[str, StringConstraints(min_length=12, max_length=1024)]


def _dr_response(data: bytes, name: str):
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def _dr(password: str, snapshot: str | None = None) -> bytes:
    from .. import dr, keys as _keys
    if not _keys.enabled():
        raise HTTPException(409, "Set a database passphrase first: on the server run sudo billtrack-manage set-passphrase")
    try:
        return dr.export_bundle(password, dr.default_extra_files(), snapshot)
    except dr.BundleError as e:
        raise HTTPException(409, str(e)) from None


@r.post("/backups/{name}/download")
def download_backup(name: str, body: DlIn, request: Request, p: Principal = Admin):
    """A stored backup as a full disaster-recovery file (backup password + database passphrase to restore)."""
    data = _dr(body.password, _backup_path(name))
    with tx() as con:
        audit(con, request, "DR backup downloaded", name)
    return _dr_response(data, name.replace(".db", ".btdr"))


@r.post("/dr-backup")
def dr_backup(body: DlIn, request: Request, p: Principal = Admin):
    """Full disaster-recovery file of the current state."""
    data = _dr(body.password)
    with tx() as con:
        audit(con, request, "DR backup downloaded", "current")
    return _dr_response(data, f"billtrack-{ledger.today().isoformat()}.btdr")


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
                           u.created_at, u.locked_until, u.perms_deny, u.status, u.registered_at,
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
    role: Role = "user"
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
    username: Username | None = None          # omitted = unchanged
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
        renamed = body.username is not None and body.username != u["username"]
        if renamed:
            if con.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE AND id<>?", (body.username, uid)).fetchone():
                raise HTTPException(409, "That username is taken")
            con.execute("UPDATE users SET username=? WHERE id=?", (body.username, uid))
            audit(con, request, "username changed", body.username, f"was {u['username']}")
        con.execute("UPDATE users SET display_name=?, email=?, role=?, disabled=?, perms_deny=? WHERE id=?",
                    (body.display_name, body.email, body.role, int(body.disabled),
                     json.dumps(sorted(set(body.perms_deny))), uid))
        if body.disabled:
            con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            con.execute("UPDATE api_tokens SET revoked=1 WHERE user_id=?", (uid,))
        audit(con, request, "user updated", body.username or u["username"],
              f"role={body.role} disabled={body.disabled} deny={','.join(body.perms_deny)}")
    # tell the person (it's their sign-in name); sessions stay valid because they're tied to the account, not the name
    to = body.email or u["email"]
    if renamed and to and notify.email_ready():
        notify._enqueue("email", to, "Your BillTrack username changed",
                        f"An administrator changed your sign-in username from '{u['username']}' to '{body.username}'.\n"
                        "Your password is unchanged. If you didn't expect this, contact your administrator.")
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


# ---------- registrations (approval) ----------
@r.get("/registrations")
def registrations(p: Principal = Admin):
    with tx() as con:
        return rows(con, "SELECT id, username, display_name, email, registered_at, status FROM users "
                         "WHERE status IN ('pending','rejected') ORDER BY status, registered_at DESC LIMIT 200")


def _decide(uid, approve, request, p):
    with tx() as con:
        u = con.execute("SELECT * FROM users WHERE id=? AND status IN ('pending','rejected')", (uid,)).fetchone()
        if not u:
            raise HTTPException(404, "No such sign-up")
        con.execute("UPDATE users SET status=?, approved_by=? WHERE id=?", ("active" if approve else "rejected", p.user_id, uid))
        audit(con, request, "sign-up approved" if approve else "sign-up rejected", u["username"], u["email"])
    if approve and u["email"] and notify.email_ready():
        base = settings.get("base_url")
        try:
            notify._enqueue("email", u["email"], "Your BillTrack account is ready",
                            f"Hi {u['display_name'] or u['username']},\n\nAn admin approved your account. Sign in as "
                            f"'{u['username']}'" + (f" at {base}" if base else "") + ".")
        except Exception:
            pass
    return {"ok": True}


@r.post("/registrations/{uid}/approve")
def approve(uid: int, request: Request, p: Principal = Admin):
    return _decide(uid, True, request, p)


@r.post("/registrations/{uid}/reject")
def reject(uid: int, request: Request, p: Principal = Admin):
    return _decide(uid, False, request, p)


# ---------- invite codes ----------
def _new_code() -> str:
    import base64
    import secrets as _s
    raw = base64.b32encode(_s.token_bytes(10)).decode()          # 80 bits, A-Z2-7
    return f"BT-{raw[0:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"


@r.get("/codes")
def codes(p: Principal = Admin):
    with tx() as con:
        cs = rows(con, "SELECT c.*, a.username AS created_by_name FROM invite_codes c LEFT JOIN users a ON a.id=c.created_by "
                       "ORDER BY c.id DESC LIMIT 300")
        uses = rows(con, "SELECT code_id, username, email, used_at FROM invite_code_uses ORDER BY used_at")
    n = iso(now_utc())
    for c in cs:
        c["code"] = crypto.unseal(c.pop("code_enc"), f"invite:{c['id']}")
        c.pop("code_hash")
        c["used_by"] = [x for x in uses if x["code_id"] == c["id"]]
        c["state"] = ("revoked" if c["revoked"] else "used" if c["uses"] >= c["max_uses"]
                      else "expired" if c["expires_at"] and c["expires_at"] < n else "unused" if not c["uses"] else "active")
    return cs


class CodeIn(In):
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""
    max_uses: Annotated[int, Field(ge=1, le=100)] = 1
    expires_days: Annotated[int, Field(ge=0, le=365)] = 14
    email: Email = ""


@r.post("/codes", status_code=201)
def create_code(body: CodeIn, request: Request, p: Principal = Admin):
    if body.email and not notify.valid_email(body.email):
        raise HTTPException(422, "Invalid email")
    code = _new_code()
    from .auth import normalize_code
    with tx() as con:
        cid = con.execute("INSERT INTO invite_codes(code_hash,code_enc,note,created_by,created_at,expires_at,max_uses,emailed_to) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (crypto.token_hash(normalize_code(code)), "", body.note, p.user_id, iso(now_utc()),
                           iso(now_utc() + timedelta(days=body.expires_days)) if body.expires_days else None,
                           body.max_uses, body.email)).lastrowid
        con.execute("UPDATE invite_codes SET code_enc=? WHERE id=?", (crypto.seal(code, f"invite:{cid}"), cid))
        audit(con, request, "invite code created", f"#{cid}", f"uses={body.max_uses} note={body.note[:40]}")
    base = settings.get("base_url")
    link = f"{base}/#register={code}" if base else ""
    sent = False
    if body.email and notify.email_ready():
        sent = notify._enqueue("email", body.email, "Your BillTrack invite",
                               "You've been invited to BillTrack.\n\n" + (f"Sign up here: {link}\n\n" if link else "")
                               + f"Invite code: {code}\n(Tick \"I have an invite code\" on the sign-up page.)") == "queued"
    return {"id": cid, "code": code, "link": link, "emailed": sent}


@r.delete("/codes/{cid}")
def revoke_code(cid: int, request: Request, p: Principal = Admin):
    with tx() as con:
        con.execute("UPDATE invite_codes SET revoked=1 WHERE id=?", (cid,))
        audit(con, request, "invite code revoked", f"#{cid}")
    return {"ok": True}


# ---------- HTTPS certificate (applied by the root-side agent, app/tlsagent.py) ----------
def _spool():
    from .. import config as _c
    return os.getenv("BT_TLS_SPOOL") or os.path.join(os.path.dirname(os.path.abspath(_c.DB_PATH)), "tls")


def _queue(req: dict, request: Request, what: str):
    import secrets as _s
    rq = os.path.join(_spool(), "requests")
    os.makedirs(rq, mode=0o700, exist_ok=True)
    if any(f.endswith(".json") for f in os.listdir(rq)):
        raise HTTPException(409, "A certificate job is already waiting. Give it a minute.")
    req["id"] = _s.token_hex(6)
    path = os.path.join(rq, f"{int(_time.time())}-{req['id']}.json")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(req, f)
    with tx() as con:
        audit(con, request, "certificate change requested", what)
    return {"queued": True, "id": req["id"]}


@r.get("/tls")
def tls_status(p: Principal = Admin):
    sp = _spool()
    try:
        st = json.load(open(os.path.join(sp, "status.json")))
    except (OSError, ValueError):
        st = {"mode": "self", "state": "unknown",
              "message": "No certificate status yet. If this stays empty, re-run sudo ./setup.sh (it installs the certificate helper)."}
    rq = os.path.join(sp, "requests")
    st["pending"] = os.path.isdir(rq) and any(f.endswith(".json") for f in os.listdir(rq))
    return st


DomainT = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=253,
                                           pattern=r"^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")]


class LEIn(In):
    domains: Annotated[list[DomainT], Field(min_length=1, max_length=10)]
    email: Email
    challenge: Literal["http", "cloudflare"] = "http"
    cf_token: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100, pattern=r"^[A-Za-z0-9_\-]*$")] = ""
    staging: bool = False


@r.post("/tls/letsencrypt")
def tls_letsencrypt(body: LEIn, request: Request, p: Principal = Admin):
    if not notify.valid_email(body.email):
        raise HTTPException(422, "Enter a valid email")
    if body.challenge == "cloudflare" and len(body.cf_token) < 20:
        raise HTTPException(422, "Paste a Cloudflare API token with Zone → DNS → Edit permission")
    return _queue({"action": "letsencrypt", "domains": body.domains, "email": body.email, "challenge": body.challenge,
                   "cf_token": body.cf_token, "staging": body.staging}, request, f"letsencrypt {','.join(body.domains)}")


class PfxIn(In):
    pfx_b64: Annotated[str, StringConstraints(max_length=200_000)]
    password: Annotated[str, StringConstraints(max_length=512)] = ""


@r.post("/tls/pfx")
def tls_pfx(body: PfxIn, request: Request, p: Principal = Admin):
    """Unpack + check a .pfx/.p12 here (wrong password, missing key, expired, key/cert mismatch are caught now), then
    hand PEMs to the agent. The password is never stored."""
    import base64 as _b
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.serialization import pkcs12
    try:
        raw = _b.b64decode(body.pfx_b64, validate=True)
        key, cert, extra = pkcs12.load_key_and_certificates(raw, body.password.encode() or None)
    except Exception:
        raise HTTPException(422, "Couldn't open that file. Check it's a .pfx/.p12 and the password is right.") from None
    if not key or not cert:
        raise HTTPException(422, "The file needs both the certificate and its private key")
    pub = lambda k: k.public_bytes(ser.Encoding.DER, ser.PublicFormat.SubjectPublicKeyInfo)
    if pub(key.public_key()) != pub(cert.public_key()):
        raise HTTPException(422, "The private key in the file doesn't match its certificate")
    from datetime import datetime as _d, timezone as _tz
    if cert.not_valid_after_utc <= _d.now(_tz.utc):
        raise HTTPException(422, "That certificate has expired")
    chain = cert.public_bytes(ser.Encoding.PEM) + b"".join(c.public_bytes(ser.Encoding.PEM) for c in (extra or []))
    key_pem = key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())
    try:
        san = [str(n.value) for n in cert.extensions.get_extension_for_class(__import__("cryptography").x509.SubjectAlternativeName).value]
    except Exception:
        san = []
    out = _queue({"action": "custom", "fullchain_pem": chain.decode(), "key_pem": key_pem.decode()}, request,
                 f"imported certificate {cert.subject.rfc4514_string()}")
    out.update(names=san, not_after=cert.not_valid_after_utc.isoformat(), subject=cert.subject.rfc4514_string())
    return out


@r.post("/tls/selfsigned")
def tls_selfsigned(request: Request, p: Principal = Admin):
    return _queue({"action": "selfsigned"}, request, "back to self-signed")


# ---------- admin dashboard / guided setup ----------
@r.get("/overview")
def admin_overview(p: Principal = Admin):
    from .. import keys as _keys
    with tx() as con:
        cnt = lambda q, *a: con.execute(q, a).fetchone()[0]
        me = con.execute("SELECT totp_enabled FROM users WHERE id=?", (p.user_id,)).fetchone()
        out = {
            "users": cnt("SELECT count(*) FROM users WHERE role='user' AND status='active'"),
            "admins": cnt("SELECT count(*) FROM users WHERE role='admin'"),
            "pending": cnt("SELECT count(*) FROM users WHERE status='pending'"),
            "codes_open": cnt("SELECT count(*) FROM invite_codes WHERE revoked=0 AND uses<max_uses AND (expires_at IS NULL OR expires_at>?)",
                              iso(now_utc())),
            "setup_done": bool(con.execute("SELECT 1 FROM meta WHERE key='setup_done'").fetchone()),
        }
    out["checklist"] = {
        "base_url": bool(settings.get("base_url")),
        "timezone": True,
        "email": notify.email_ready(),
        "my_2fa": bool(me["totp_enabled"]),
        "registration": settings.get("registration_mode"),
        "passphrase": _keys.enabled(),
        "backups": len(list_backups()),
    }
    return out


@r.post("/setup-done")
def setup_done(done: bool = True, p: Principal = Admin):
    with tx() as con:
        if done:
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('setup_done','true')")
        else:
            con.execute("DELETE FROM meta WHERE key='setup_done'")
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
