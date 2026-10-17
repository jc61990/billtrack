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


def test_end_date_mistake_fixed(admin):
    t = ledger.today()
    # API now refuses an end date before the first due date
    r = admin.post("/api/bills", json={"name": "Bad", "amount_cents": 1, "start_date": "2026-10-23",
                                       "end_date": "2026-09-23"}, headers=H)
    assert r.status_code == 422 and "Stop after" in r.text
    # rows saved by older versions get repaired by the migration
    with db.tx() as con:
        con.execute("INSERT INTO bills(name,amount_cents,start_date,recurrence,end_date) VALUES "
                    "('OldA',100,'2026-10-23','monthly','2026-09-23'),('OldB',100,'2026-10-01','monthly','2026-10-01'),"
                    "('Once',100,'2026-10-01','once','2026-10-01'),('Loan',100,'2026-10-01','monthly','2027-03-01')")
        db.migrate(con)
        got = {r["name"]: r["end_date"] for r in con.execute("SELECT name,end_date FROM bills WHERE name IN ('OldA','OldB','Once','Loan')")}
    assert got == {"OldA": None, "OldB": None, "Once": "2026-10-01", "Loan": "2027-03-01"}
    s = admin.get("/api/summary?start=2026-11-01&end=2026-11-30").json()
    assert {"OldA", "OldB"} <= {b["name"] for b in s["bills"]}


def test_home_page(admin):
    t = ledger.today()
    admin.post("/api/bills", json={"name": "HomeBill", "amount_cents": 5000, "start_date": (t + timedelta(days=2)).isoformat()}, headers=H)
    admin.post("/api/bills", json={"name": "LateBill", "amount_cents": 7000, "start_date": (t - timedelta(days=3)).isoformat(),
                                   "recurrence": "once"}, headers=H)
    h = admin.get("/api/home").json()
    assert {"week", "upcoming", "overdue", "accounts", "networth", "trackers", "investments", "reminders"} <= set(h)
    assert any(b["name"] == "HomeBill" for b in h["upcoming"]) and any(b["name"] == "LateBill" for b in h["overdue"])
    mkuser("v7", "viewer")
    v = login("v7")
    uid = [u for u in admin.get("/api/admin/users").json() if u["username"] == "v7"][0]["id"]
    admin.put(f"/api/admin/users/{uid}", json={"role": "viewer", "perms_deny": ["investments:read", "trackers:read"]}, headers=H)
    hv = v.get("/api/home").json()
    assert "investments" not in hv and "trackers" not in hv and "week" in hv


def test_upgrade_snapshots_rotate_and_restore(admin, tmp_path):
    admin.post("/api/accounts", json={"name": "SnapMe"}, headers=H)
    import time as _t
    made = []
    for i in range(9):
        p, removed = db.snapshot(str(tmp_path), keep=7, label=f"pre-2.6.{i}")
        made.append(os.path.basename(p))
        _t.sleep(1.01)   # names are second-resolution timestamps
    left = sorted(f for f in os.listdir(tmp_path) if db.SNAP_RE.match(f))
    assert len(left) == 7 and left == sorted(made)[-7:]          # oldest two rotated out
    raw = open(tmp_path / left[-1], "rb").read()
    assert not raw.startswith(db.PLAIN_HEADER) and b"SnapMe" not in raw
    con = db.connect(str(tmp_path / left[-1]))                   # opens with the server key
    assert any(r["name"] == "SnapMe" for r in db.rows(con, "SELECT name FROM accounts"))
    con.close()
    before, after = db.vacuum()
    assert after <= before or after > 0


def test_maintenance_and_server_backups(admin):
    import shutil as _sh
    _sh.rmtree(db.backup_dir(), ignore_errors=True)
    s = admin.get("/api/admin/system").json()
    assert s["db_bytes"] > 0 and s["disk_free"] > 0 and "rows" in s
    for op in ("checkpoint", "optimize", "prune", "integrity", "compact"):
        r = admin.post(f"/api/admin/maintenance/{op}", headers=H)
        assert r.status_code == 200, (op, r.text)
    assert admin.post("/api/admin/maintenance/integrity", headers=H).json()["ok"] is True
    assert admin.get("/api/admin/system").json()["last"]["compact"]["at"]
    assert admin.post("/api/admin/maintenance/rm-rf", headers=H).status_code == 422
    # manual backup, then change data, then restore it
    admin.post("/api/accounts", json={"name": "BeforeBackup"}, headers=H)
    name = admin.post("/api/admin/backups", json={}, headers=H).json()["name"]
    admin.post("/api/accounts", json={"name": "AfterBackup"}, headers=H)
    lst = admin.get("/api/admin/backups").json()
    assert lst[0]["name"] == name and lst[0]["kind"] == "Manual"
    assert admin.post(f"/api/admin/backups/{name}/restore", json={"confirm": "nope"}, headers=H).status_code == 422
    r = admin.post(f"/api/admin/backups/{name}/restore", json={"confirm": "RESTORE"}, headers=H)
    assert r.status_code == 200 and r.json()["safety"].endswith("-safety.db")
    login_again = login("jc")
    names = {a["name"] for a in login_again.get("/api/accounts").json()}
    assert "BeforeBackup" in names and "AfterBackup" not in names
    kinds = {b["kind"] for b in login_again.get("/api/admin/backups").json()}
    assert kinds == {"Manual", "Before restore"}
    # download re-locked with a passphrase
    d = login_again.post(f"/api/admin/backups/{name}/download", json={"password": "download pass 123"}, headers=H)
    assert d.status_code == 409 and "passphrase" in d.text      # DR downloads need passphrase mode (tested separately)
    # path traversal / bogus names
    for bad in ("..%2F..%2Fetc%2Fpasswd", "billtrack.db", "x.db"):
        assert login_again.delete(f"/api/admin/backups/{bad}", headers=H).status_code in (404, 405)  # never served
    assert login_again.delete(f"/api/admin/backups/{name}", headers=H).status_code == 200
    # rotation is per kind: 9 manual backups leave 7 manual and keep the safety copy
    import time as _t
    for _ in range(9):
        login_again.post("/api/admin/backups", json={}, headers=H); _t.sleep(1.01)
    lst = login_again.get("/api/admin/backups").json()
    assert sum(b["kind"] == "Manual" for b in lst) == 7 and sum(b["kind"] == "Before restore" for b in lst) == 1
    mkuser("m8", "member")
    assert login("m8").get("/api/admin/backups").status_code == 403


def test_smtp_no_auth_relay(admin, monkeypatch):
    import smtplib
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, local_hostname=None, timeout=None):
            calls.append(("connect", host, port, local_hostname))
        def starttls(self, context=None):
            calls.append(("starttls",))
        def login(self, u, p):
            calls.append(("login", u))
        def send_message(self, m):
            calls.append(("send", m["From"], m["To"]))
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    settings.update({"smtp_host": "smtp-relay.gmail.com", "smtp_port": 587, "smtp_tls": "starttls", "smtp_from": "bills@example.com",
                     "smtp_auth": "none", "smtp_user": "leftover@example.com", "smtp_helo": "bills.example.com"})
    try:
        assert notify.send_email("jc@example.com", "Hi", "Body") is True
        assert ("connect", "smtp-relay.gmail.com", 587, "bills.example.com") in calls and ("starttls",) in calls
        assert not any(c[0] == "login" for c in calls)                 # relay mode never signs in
        calls.clear(); settings.update({"smtp_auth": "password"})
        notify.send_email("jc@example.com", "Hi", "Body")
        assert ("login", "leftover@example.com") in calls               # password mode still does
        assert admin.put("/api/admin/settings", json={"smtp_helo": "not a host!"}, headers=H).status_code == 422
        assert admin.put("/api/admin/settings", json={"smtp_auth": "plain"}, headers=H).status_code == 422
    finally:
        settings.update({"smtp_host": "", "smtp_from": "", "smtp_user": "", "smtp_helo": "", "smtp_auth": "password"})


def test_connect_page_csp_scoped():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    connect = c.get("/connect.html").headers["content-security-policy"]
    main = c.get("/").headers["content-security-policy"]
    assert "cdn.plaid.com" in connect and "'unsafe-inline'" in connect.split("style-src")[1].split(";")[0]
    assert "plaid" not in main and "unsafe-inline" not in main            # main app unchanged
    assert "unsafe" not in connect.split("script-src")[1].split(";")[0]    # scripts still locked down
