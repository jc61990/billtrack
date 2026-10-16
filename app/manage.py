"""BillTrack admin CLI

  python -m app.manage adduser <name> [role] create login (role: admin|member|viewer, default admin if first user)
  python -m app.manage passwd <name>        change password (signs out that user)
  python -m app.manage reset-2fa <name>     turn off two-factor for a locked-out user
  python -m app.manage unlock <name>        clear a login lockout
  python -m app.manage gen-db-key           print a new 256-bit database key (hex)
  python -m app.manage gen-ics-token        print a random calendar-feed token
  python -m app.manage encrypt-db           encrypt a v1.0 plaintext database in place
  python -m app.manage rekey <new-key-file> re-encrypt the database with a new key
  python -m app.manage backup <out.db>      write a passphrase-encrypted backup
  python -m app.manage restore <in.db>      restore a backup (service must be stopped)
  python -m app.manage snapshot <dir> [keep] [label]  verified copy of the DB (server key), keep newest N (default 7)
  python -m app.manage vacuum               compact the database (service must be stopped)
  python -m app.manage rollback <snapshot>  put an upgrade snapshot back as the live DB (service must be stopped)
"""
import getpass
import os
import secrets
import sys
from pathlib import Path

import json

from . import config, crypto, db, security


def _pw(prompt: str, confirm=True, min_len=12) -> str:
    pw = getpass.getpass(prompt)
    if confirm and pw != getpass.getpass("Confirm: "):
        sys.exit("Entries do not match")
    if len(pw) < min_len:
        sys.exit(f"Must be at least {min_len} characters")
    return pw


def main():
    a = sys.argv[1:]
    try:
        if a[:1] == ["gen-db-key"] and len(a) == 1:
            print(db.new_key())
        elif a[:1] == ["gen-ics-token"] and len(a) == 1:
            print(secrets.token_urlsafe(32))
        elif a[:1] == ["adduser"] and len(a) in (2, 3):
            db.init()
            with db.tx() as con:
                if con.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (a[1],)).fetchone():
                    sys.exit("User exists. Use passwd to change the password.")
                first = not con.execute("SELECT 1 FROM users").fetchone()
            role = a[2] if len(a) == 3 else ("admin" if first else "member")
            if role not in security.ROLE_PERMS:
                sys.exit("Role must be admin, member or viewer")
            h = security.hash_pw(_pw("Password (12+ chars): "))
            n = security.iso(security.now_utc())
            with db.tx() as con:
                con.execute("INSERT INTO users(username,pw_hash,role,created_at,pw_changed_at) VALUES (?,?,?,?,?)",
                            (a[1], h, role, n, n))
                security.audit(con, None, "user created (cli)", a[1], f"role={role}")
            print(f"User '{a[1]}' created as {role}.")
        elif a[:1] in (["passwd"], ["reset-2fa"], ["unlock"]) and len(a) == 2:
            db.init()
            with db.tx() as con:
                u = con.execute("SELECT id FROM users WHERE username=? COLLATE NOCASE", (a[1],)).fetchone()
            if not u:
                sys.exit("No such user")
            with db.tx() as con:
                if a[0] == "passwd":
                    h = security.hash_pw(_pw("New password (12+ chars): "))
                    con.execute("UPDATE users SET pw_hash=?, pw_changed_at=?, failed_logins=0, locked_until=NULL "
                                "WHERE id=?", (h, security.iso(security.now_utc()), u["id"]))
                    con.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
                elif a[0] == "reset-2fa":
                    con.execute("UPDATE users SET totp_enabled=0, totp_secret_enc='', totp_last_step=0 WHERE id=?",
                                (u["id"],))
                    con.execute("DELETE FROM recovery_codes WHERE user_id=?", (u["id"],))
                else:
                    con.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (u["id"],))
                security.audit(con, None, f"{a[0]} (cli)", a[1])
            print("Done.")
        elif a == ["encrypt-db"]:
            if not db.is_plaintext(config.DB_PATH):
                print("Database is already encrypted (or does not exist). Nothing to do.")
                return
            bak = db.encrypt_plaintext(config.DB_PATH, db.load_key())
            print(f"Encrypted {config.DB_PATH}.\nPlaintext copy kept at {bak} - verify the app works, then delete it:\n"
                  f"  shred -u '{bak}'   (on SSDs shred is best-effort; full-disk encryption covers the rest)")
        elif a[:1] == ["rekey"] and len(a) == 2:
            new = Path(a[1]).read_text().strip()
            if not db._HEX64.match(new):
                sys.exit("New key must be 64 hex characters")
            old = db.load_key()
            con = db.connect()
            # Re-seal field-encrypted secrets (derived from the DB key) before switching keys
            for r in con.execute("SELECT id, totp_secret_enc FROM users WHERE totp_secret_enc<>''").fetchall():
                v = crypto.unseal(r["totp_secret_enc"], f"totp:{r['id']}", old)
                con.execute("UPDATE users SET totp_secret_enc=? WHERE id=?", (crypto.seal(v, f"totp:{r['id']}", new), r["id"]))
            from .settings import SECRET
            for r in con.execute("SELECT key, value FROM settings").fetchall():
                if r["key"] in SECRET:
                    v = crypto.unseal(json.loads(r["value"]), f"setting:{r['key']}", old)
                    con.execute("UPDATE settings SET value=? WHERE key=?",
                                (json.dumps(crypto.seal(v, f"setting:{r['key']}", new)), r["key"]))
            con.commit()
            con.execute(f"PRAGMA rekey = \"x'{new}'\"")
            con.close()
            db.connect(key=new).close()
            print("Re-encrypted. Point BT_DB_KEY_FILE at the new key file and restart the service.")
        elif a[:1] == ["backup"] and len(a) == 2:
            data = db.backup_bytes(_pw("Backup passphrase (12+ chars): "))
            fd = os.open(a[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            print(f"Encrypted backup written to {a[1]}")
        elif a[:1] == ["snapshot"] and 2 <= len(a) <= 4:
            path, removed = db.snapshot(a[1], int(a[2]) if len(a) > 2 else 7, a[3] if len(a) > 3 else "")
            print(f"    Backup: {path} ({os.path.getsize(path) / 1024:.0f} KB, verified)")
            for f in removed:
                print(f"    Rotated out old backup: {f}")
        elif a == ["vacuum"]:
            before, after = db.vacuum()
            print(f"    Compacted database: {before / 1024:.0f} KB -> {after / 1024:.0f} KB")
        elif a[:1] == ["rollback"] and len(a) == 2:
            import shutil
            src = a[1]
            if not db.SNAP_RE.match(os.path.basename(src)):
                sys.exit("Not a BillTrack upgrade snapshot (billtrack-YYYYMMDD-HHMMSS*.db)")
            db.connect(src).close()   # proves it opens with the current key
            for suffix in ("-wal", "-shm"):
                if os.path.exists(config.DB_PATH + suffix):
                    os.remove(config.DB_PATH + suffix)
            if os.path.exists(config.DB_PATH):
                os.replace(config.DB_PATH, config.DB_PATH + ".before-rollback")
            shutil.copy2(src, config.DB_PATH)
            os.chmod(config.DB_PATH, 0o600)
            print(f"Restored {src}. Previous database kept as {config.DB_PATH}.before-rollback. Start the service.")
        elif a[:1] == ["restore"] and len(a) == 2:
            if os.path.exists(config.DB_PATH):
                sys.exit(f"{config.DB_PATH} exists. Stop the service and move it aside first.")
            db.restore_backup(a[1], _pw("Backup passphrase: ", confirm=False, min_len=1),
                              config.DB_PATH, db.load_key())
            print(f"Restored into {config.DB_PATH}. Start the service.")
        else:
            sys.exit(__doc__)
    except db.KeyError_ as e:
        sys.exit(str(e))
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
