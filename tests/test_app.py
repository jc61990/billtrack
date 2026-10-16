import os
import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import crypto, db, ledger, notify, security, settings, totp
from app.main import app
from app.recurrence import occurrences

H = {"X-BT": "1"}
PW = "correct horse battery"


def mkuser(name, role="admin", email=""):
    db.init()
    with db.tx() as con:
        con.execute("DELETE FROM users WHERE username=?", (name,))
        con.execute("INSERT INTO users(username,pw_hash,role,email) VALUES (?,?,?,?)",
                    (name, security.hash_pw(PW), role, email))


def login(name):
    c = TestClient(app)
    c.__enter__()
    r = c.post("/api/login", json={"username": name, "password": PW}, headers=H)
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def admin():
    mkuser("jc", "admin", "jc@example.com")
    return login("jc")


def test_recurrence_month_end_clamp():
    got = list(occurrences(date(2026, 1, 31), "monthly", date(2026, 1, 1), date(2026, 4, 30)))
    assert got == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]


def test_security_basics():
    db.init()
    c = TestClient(app)
    assert c.get("/api/week").status_code == 401
    assert c.post("/api/login", json={"username": "x", "password": "y"}).status_code == 403
    assert c.post("/api/login", json={"username": "x", "password": "y"},
                  headers={**H, "Origin": "https://evil.example"}).status_code == 403
    r = c.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert c.get("/cal/wrong.ics").status_code == 404
    assert c.get("/docs").status_code == 404 and c.get("/api/openapi.json").status_code == 401


def test_flow_and_overrides(admin):
    c, t = admin, ledger.today()
    acct = c.post("/api/accounts", json={"name": "Checking"}, headers=H).json()
    bill = c.post("/api/bills", json={"name": "Electric", "amount_cents": 12000, "account_id": acct["id"],
                                      "start_date": t.isoformat(), "recurrence": "weekly"}, headers=H).json()
    src = c.post("/api/income-sources", json={"name": "Job", "amount_cents": 100000, "start_date": t.isoformat(),
                                              "hourly_rate_cents": 3000}, headers=H).json()
    ot = c.post("/api/income-extra", json={"date": t.isoformat(), "hours": 4, "source_id": src["id"]}, headers=H).json()
    assert ot["amount_cents"] == 18000
    assert c.post("/api/bills", json={"name": "x", "amount_cents": 1, "start_date": t.isoformat(),
                                      "evil": 1}, headers=H).status_code == 422  # extra fields rejected
    # drag: move this week's occurrence out of the week, then skip, then reset
    new = t + timedelta(days=20)
    assert c.put("/api/overrides", json={"kind": "bill", "item_id": bill["id"], "orig_date": t.isoformat(),
                                         "new_date": new.isoformat()}, headers=H).status_code == 200
    s = c.get(f"/api/summary?start={new}&end={new}").json()
    assert any(b["orig_date"] == t.isoformat() and b["moved"] for b in s["bills"])
    c.put("/api/overrides", json={"kind": "bill", "item_id": bill["id"], "orig_date": t.isoformat(), "skipped": True},
          headers=H)
    w = c.get("/api/week").json()
    assert [b for b in w["bills"] if b["orig_date"] == t.isoformat()][0]["status"] == "skipped"
    assert c.put("/api/overrides", json={"kind": "bill", "item_id": bill["id"], "orig_date": (t + timedelta(days=1)).isoformat(),
                                         "new_date": new.isoformat()}, headers=H).status_code == 422
    c.put("/api/overrides", json={"kind": "bill", "item_id": bill["id"], "orig_date": t.isoformat()}, headers=H)
    assert c.post("/api/payments", json={"bill_id": bill["id"], "due_date": t.isoformat()}, headers=H).status_code == 201
    ov = c.get(f"/api/reports/overview?start={t - timedelta(days=30)}&end={t + timedelta(days=30)}&group=month").json()
    assert ov["in_cents"] > 0 and ov["series"] and "net_worth_cents" in ov
    ics = c.get("/cal/testtoken123.ics").text
    assert "Electric" in ics and "Payday" in ics


def test_roles_and_permissions(admin):
    mkuser("viewer1", "viewer")
    v = login("viewer1")
    assert v.get("/api/week").status_code == 200
    assert v.post("/api/accounts", json={"name": "x"}, headers=H).status_code == 403
    assert v.get("/api/admin/users").status_code == 403
    assert "admin" not in v.get("/api/me").json()["perms"]
    mkuser("member1", "member")
    m = login("member1")
    assert m.post("/api/accounts", json={"name": "m"}, headers=H).status_code == 201
    uid = [u for u in admin.get("/api/admin/users").json() if u["username"] == "member1"][0]["id"]
    r = admin.put(f"/api/admin/users/{uid}", json={"role": "member", "perms_deny": ["investments:read"]}, headers=H)
    assert r.status_code == 200
    assert m.get("/api/investments").status_code == 403
    assert admin.put(f"/api/admin/users/{uid}", json={"role": "member", "perms_deny": ["admin"]},
                     headers=H).status_code == 422
    # disabling kills sessions
    admin.put(f"/api/admin/users/{uid}", json={"role": "member", "disabled": True}, headers=H)
    assert m.get("/api/me").status_code == 401


def test_last_admin_protected(admin):
    me_id = admin.get("/api/me").json()["id"]
    assert admin.put(f"/api/admin/users/{me_id}", json={"role": "member"}, headers=H).status_code == 409


def test_totp_login(admin):
    s = admin.post("/api/me/2fa/setup", headers=H).json()
    step = int(__import__("time").time() // 30)
    code = totp._code(s["secret"], step)
    rc = admin.post("/api/me/2fa/enable", json={"code": code}, headers=H).json()["recovery_codes"]
    assert len(rc) == 10
    with db.tx() as con:  # secret is sealed, not stored raw
        enc = con.execute("SELECT totp_secret_enc FROM users WHERE username='jc'").fetchone()[0]
    assert s["secret"] not in enc and crypto.unseal(enc, f"totp:{admin.get('/api/me').json()['id']}") == s["secret"]
    c = TestClient(app)
    r = c.post("/api/login", json={"username": "jc", "password": PW}, headers=H).json()
    assert r["mfa_required"]
    assert c.post("/api/login/mfa", json={"mfa_token": r["mfa_token"], "code": code}, headers=H).status_code == 401  # replay
    assert c.post("/api/login/mfa", json={"mfa_token": r["mfa_token"], "code": rc[0]}, headers=H).status_code == 200
    assert c.get("/api/me").status_code == 200
    c2 = TestClient(app)
    r = c2.post("/api/login", json={"username": "jc", "password": PW}, headers=H).json()
    assert c2.post("/api/login/mfa", json={"mfa_token": r["mfa_token"], "code": rc[0]}, headers=H).status_code == 401  # used


def test_require_2fa_blocks_until_enrolled(admin):
    settings.update({"require_2fa": True})
    try:
        mkuser("m2", "member")
        m = login("m2")
        assert m.get("/api/week").status_code == 403
        assert m.get("/api/me").json()["mfa_setup_required"] is True
    finally:
        settings.update({"require_2fa": False})


def test_lockout():
    mkuser("lock1", "member")
    c = TestClient(app)
    for _ in range(security.LOCK_AFTER):
        c.post("/api/login", json={"username": "lock1", "password": "wrong password!"}, headers=H)
    security.login_ip.hits.clear()
    assert c.post("/api/login", json={"username": "lock1", "password": PW}, headers=H).status_code == 401


def test_api_tokens(admin):
    tok = admin.post("/api/me/tokens", json={"name": "cron", "scope": "read"}, headers=H).json()["token"]
    with db.tx() as con:
        assert not con.execute("SELECT 1 FROM api_tokens WHERE token_hash=?", (tok,)).fetchone()  # only hash stored
    c = TestClient(app)
    auth = {"Authorization": f"Bearer {tok}"}
    assert c.get("/api/week", headers=auth).status_code == 200
    assert c.post("/api/accounts", json={"name": "x"}, headers=auth).status_code == 403     # read scope
    assert c.get("/api/admin/users", headers=auth).status_code == 403                       # never admin
    assert c.post("/api/me/tokens", json={"name": "x"}, headers=auth).status_code == 403     # can't mint tokens
    w = admin.post("/api/me/tokens", json={"name": "w", "scope": "write"}, headers=H).json()
    acct = c.post("/api/investments/accounts", json={"name": "Brokerage"}, headers={"Authorization": f"Bearer {w['token']}"})
    assert acct.status_code == 201  # no CSRF header needed for bearer
    admin.delete(f"/api/me/tokens/{w['id']}", headers=H)
    assert c.get("/api/week", headers={"Authorization": f"Bearer {w['token']}"}).status_code == 401


def test_trackers_investments(admin):
    t = admin.post("/api/trackers/items", json={"name": "Emergency", "target_cents": 100000}, headers=H).json()
    admin.post("/api/trackers/entries", json={"tracker_id": t["id"], "date": ledger.today().isoformat(),
                                              "amount_cents": 50000}, headers=H)
    tr = [x for x in admin.get("/api/trackers").json() if x["id"] == t["id"]][0]
    assert tr["progress"] == 0.5
    with db.tx() as con:
        assert con.execute("SELECT count(*) FROM notifications WHERE title LIKE 'Emergency: 50%'").fetchone()[0] >= 1
    a = admin.post("/api/investments/accounts", json={"name": "IRA", "kind": "retirement"}, headers=H).json()
    admin.post("/api/investments/holdings", json={"inv_account_id": a["id"], "symbol": "vti", "quantity": 10,
                                                  "cost_basis_cents": 200000, "price_cents": 25000}, headers=H)
    assert admin.post("/api/investments/prices", json={"prices": [{"symbol": "VTI", "price_cents": 30000}]},
                      headers=H).json()["updated_holdings"] == 1
    inv = admin.get("/api/investments").json()
    acc = [x for x in inv["accounts"] if x["id"] == a["id"]][0]
    assert acc["value_cents"] == 300000 and acc["gain_cents"] == 100000


def test_settings_secrets_sealed(admin):
    r = admin.put("/api/admin/settings", json={"smtp_pass": "s3cret-pass", "timezone": "America/New_York"}, headers=H)
    assert r.status_code == 200 and "smtp_pass" not in r.json() and r.json()["smtp_pass_set"]
    with db.tx() as con:
        raw = con.execute("SELECT value FROM settings WHERE key='smtp_pass'").fetchone()[0]
    assert "s3cret" not in raw
    assert admin.put("/api/admin/settings", json={"timezone": "Mars/Base"}, headers=H).status_code == 422
    settings.update({"timezone": "America/Chicago", "smtp_pass": ""})


def test_password_reset_and_invite(admin, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "email_ready", lambda: True)
    monkeypatch.setattr(notify, "send_email", lambda to, s, b: sent.append((to, b)) or True)
    r = TestClient(app).post("/api/password/forgot", json={"email": "jc@example.com"}, headers=H)
    assert r.status_code == 200 and sent
    tok = sent[-1][1].split("#reset=")[1].split()[0]
    c = TestClient(app)
    assert c.post("/api/password/reset", json={"token": tok, "password": "short"}, headers=H).status_code == 422
    assert c.post("/api/password/reset", json={"token": tok, "password": PW + "!"}, headers=H).status_code == 200
    assert c.post("/api/password/reset", json={"token": tok, "password": PW + "!"}, headers=H).status_code == 400
    assert admin.get("/api/me").status_code == 401  # sessions ended on reset
    # unknown email gets identical response
    assert TestClient(app).post("/api/password/forgot", json={"email": "nobody@x.com"}, headers=H).json()["ok"]
    mkuser("jc", "admin", "jc@example.com")
    a = login("jc")
    inv = a.post("/api/admin/invites", json={"email": "new@example.com", "role": "viewer"}, headers=H).json()
    itok = inv["link"].split("#invite=")[1]
    c = TestClient(app)
    assert c.get(f"/api/invite/info?token={itok}").json()["role"] == "viewer"
    assert c.post("/api/invite/accept", json={"token": itok, "username": "newbie", "password": PW}, headers=H).status_code == 200
    assert c.get("/api/me").json()["role"] == "viewer"
    assert TestClient(app).post("/api/invite/accept", json={"token": itok, "username": "again", "password": PW},
                                headers=H).status_code == 400


def test_reminders_and_inbox(admin):
    r = admin.post("/api/reminders", json={"title": "Call bank", "next_at": "2020-01-01T09:00"}, headers=H)
    assert r.status_code == 201
    notify.check_once()
    n = admin.get("/api/me/notifications").json()
    assert any("Call bank" in x["title"] for x in n)
    assert not [x for x in admin.get("/api/reminders").json() if x["id"] == r.json()["id"]][0]["active"]
    assert notify.next_reminder("2026-01-31T09:00", "monthly", "2026-01-31T09:00") == "2026-02-28T09:00"


def test_audit_log(admin):
    admin.post("/api/accounts", json={"name": "Audited"}, headers=H)
    log = admin.get("/api/admin/audit?q=accounts").json()
    assert any(x["action"] == "create accounts" for x in log)


def test_db_file_is_encrypted(admin):
    admin.post("/api/bills", json={"name": "ZZSECRETBILL", "amount_cents": 1, "start_date": "2026-01-01"}, headers=H)
    con = db.connect(); con.execute("PRAGMA wal_checkpoint(TRUNCATE)"); con.close()
    raw = open(os.environ["BT_DB_PATH"], "rb").read()
    assert not raw.startswith(db.PLAIN_HEADER) and b"ZZSECRETBILL" not in raw
    with pytest.raises(db.KeyError_):
        db.connect(key="cd" * 32)


def test_backup_restore_roundtrip(admin, tmp_path):
    admin.post("/api/accounts", json={"name": "RestoreMe"}, headers=H)
    r = admin.post("/api/admin/backup", json={"passphrase": "backup passphrase 1"}, headers=H)
    assert r.status_code == 200 and b"RestoreMe" not in r.content
    bk = tmp_path / "bk.db"
    bk.write_bytes(r.content)
    db.restore_backup(str(bk), "backup passphrase 1", str(tmp_path / "r.db"), "ef" * 32)
    con = db.connect(str(tmp_path / "r.db"), "ef" * 32)
    assert any(a["name"] == "RestoreMe" for a in db.rows(con, "SELECT name FROM accounts"))
    con.close()


def test_migrate_v1_plaintext(tmp_path):
    p = str(tmp_path / "old.db")
    s = sqlite3.connect(p)
    s.executescript("""CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL);
                       CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT);
                       CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL DEFAULT '#3b6ea5');
                       CREATE TABLE notices (bill_id INTEGER, due_date TEXT, kind TEXT, sent_at TEXT);""")
    s.execute("INSERT INTO users(username,pw_hash) VALUES ('old','x')")
    s.execute("INSERT INTO accounts(name) VALUES ('OldPlain')")
    s.commit(); s.close()
    db.encrypt_plaintext(p, "12" * 32)
    con = db.connect(p, "12" * 32)
    db.migrate(con); con.commit()
    assert db.rows(con, "SELECT role FROM users")[0]["role"] == "admin"
    assert db.rows(con, "SELECT kind FROM accounts")[0]["kind"] == "checking"
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='notices'").fetchone()
    con.close()


def test_bank_accounts_and_cards(admin):
    with db.tx() as con:
        con.execute("DELETE FROM alert_log"); con.execute("DELETE FROM notifications")
    t = ledger.today()
    chk = admin.post("/api/accounts", json={"name": "Chk", "kind": "checking", "balance_cents": 50000,
                                            "low_balance_cents": 20000}, headers=H).json()
    admin.post("/api/bills", json={"name": "BigBill", "amount_cents": 40000, "account_id": chk["id"],
                                   "start_date": t.isoformat(), "recurrence": "once"}, headers=H)
    card = admin.post("/api/accounts", json={"name": "Visa", "kind": "credit", "balance_cents": 1000,
                                             "credit_limit_cents": 100000, "util_alert_pct": 30, "last4": "4417",
                                             "due_day": t.day, "min_payment_cents": 2500}, headers=H).json()
    # full card numbers are refused
    assert admin.post("/api/accounts", json={"name": "x", "kind": "credit", "last4": "4111111111111111"},
                      headers=H).status_code == 422
    v = admin.get("/api/accounts/overview").json()
    b = [a for a in v["banks"] if a["id"] == chk["id"]][0]
    assert b["projected_cents"] == 10000 and b["projected_low"] and not b["low"]
    c = [a for a in v["cards"] if a["id"] == card["id"]][0]
    assert c["utilization"] == 0.01 and c["available_cents"] == 99000 and c["days_to_due"] == 0
    titles = lambda: [n["body"] for n in admin.get("/api/me/notifications").json()]
    assert any("Heads up: Chk" in x for x in titles()) and any("Visa payment due" in x for x in titles())
    # cross the utilization threshold -> one alert, not repeated
    admin.post(f"/api/accounts/{card['id']}/balance", json={"balance_cents": 45000}, headers=H)
    admin.post(f"/api/accounts/{card['id']}/balance", json={"balance_cents": 46000}, headers=H)
    assert sum("45% utilized" in x for x in titles()) == 1 and not any("46% utilized" in x for x in titles())
    # drop below, then back over -> re-armed
    admin.post(f"/api/accounts/{card['id']}/balance", json={"balance_cents": 5000}, headers=H)
    admin.post(f"/api/accounts/{card['id']}/balance", json={"balance_cents": 50000}, headers=H)
    assert any("50% utilized" in x for x in titles())
    # low balance now
    admin.post(f"/api/accounts/{chk['id']}/balance", json={"balance_cents": 15000}, headers=H)
    assert any("Low balance: Chk" in x for x in titles())
    hist = [a for a in admin.get("/api/accounts/overview").json()["banks"] if a["id"] == chk["id"]][0]["history"]
    assert hist[-1]["balance_cents"] == 15000
    # editing other fields keeps the card's limit etc. (full model round trip)
    r = admin.put(f"/api/accounts/{card['id']}", json={"name": "Visa 2", "kind": "credit", "balance_cents": 50000,
                                                       "credit_limit_cents": 100000, "last4": "4417"}, headers=H)
    assert r.status_code == 200
    mkuser("v2", "viewer")
    v2 = login("v2")
    assert v2.post(f"/api/accounts/{card['id']}/balance", json={"balance_cents": 1}, headers=H).status_code == 403
    assert v2.get("/api/accounts/overview").status_code == 200


def test_net_worth(admin):
    with db.tx() as con:
        for t in ("accounts", "networth_items", "networth_snapshots", "inv_accounts"):
            con.execute(f"DELETE FROM {t}")
    admin.post("/api/accounts", json={"name": "Chk", "kind": "checking", "balance_cents": 300000}, headers=H)
    admin.post("/api/accounts", json={"name": "Visa", "kind": "credit", "balance_cents": 50000, "credit_limit_cents": 100000}, headers=H)
    admin.post("/api/accounts", json={"name": "Car loan", "kind": "loan", "balance_cents": 1000000}, headers=H)
    h = admin.post("/api/networth/items", json={"name": "House", "side": "asset", "category": "property",
                                                "value_cents": 30000000}, headers=H).json()
    admin.post("/api/networth/items", json={"name": "Mortgage", "side": "liability", "category": "mortgage",
                                            "value_cents": 20000000}, headers=H)
    assert admin.post("/api/networth/items", json={"name": "x", "side": "asset", "category": "mortgage"},
                      headers=H).status_code == 422
    ia = admin.post("/api/investments/accounts", json={"name": "IRA", "cash_cents": 500000}, headers=H).json()
    nw = admin.get("/api/networth").json()
    assert nw["assets_cents"] == 300000 + 500000 + 30000000
    assert nw["liabilities_cents"] == 50000 + 1000000 + 20000000
    assert nw["net_cents"] == nw["assets_cents"] - nw["liabilities_cents"]
    hist = admin.get("/api/networth/history").json()
    assert hist[-1]["net_cents"] == nw["net_cents"]
    # old snapshot -> change figures
    with db.tx() as con:
        con.execute("INSERT INTO networth_snapshots VALUES (?,?,?,?,?,?,?)",
                    ((ledger.today() - timedelta(days=40)).isoformat(), 100000, 0, 30000000, 0, 0, 20000000))
    nw = admin.get("/api/networth").json()
    assert nw["changes"]["30d"] == nw["net_cents"] - (100000 + 30000000 - 20000000)
    # overview uses the same number
    t = ledger.today()
    assert admin.get(f"/api/reports/overview?start={t}&end={t}").json()["net_worth_cents"] == nw["net_cents"]
    # someone who can't see investments gets a net worth without them
    mkuser("m3", "member")
    uid = [u for u in admin.get("/api/admin/users").json() if u["username"] == "m3"][0]["id"]
    admin.put(f"/api/admin/users/{uid}", json={"role": "member", "perms_deny": ["investments:read"]}, headers=H)
    m = login("m3")
    assert m.get("/api/networth").json()["net_cents"] == nw["net_cents"] - 500000


def test_reminders_on_calendar(admin):
    t = ledger.today()
    d23 = t.replace(day=23)
    admin.post("/api/reminders", json={"title": "Dentist", "next_at": f"{d23}T15:30"}, headers=H)
    admin.post("/api/reminders", json={"title": "Weekly check", "next_at": f"{t}T08:00", "recurrence": "weekly"}, headers=H)
    occ = admin.get(f"/api/reminders/occurrences?start={t.replace(day=1)}&end={t.replace(day=28)}").json()
    assert any(o["title"] == "Dentist" and o["date"] == d23.isoformat() and o["time"] == "15:30" for o in occ)
    assert sum(o["title"] == "Weekly check" for o in occ) >= 2
    mkuser("v9", "viewer")
    assert all(o["title"] not in ("Dentist",) for o in login("v9").get(
        f"/api/reminders/occurrences?start={t.replace(day=1)}&end={t.replace(day=28)}").json())  # own reminders only
