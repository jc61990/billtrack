"""Bank sync against a simulated Plaid (no network)."""
from datetime import timedelta

import pytest

from datetime import date  # noqa: E402
from app import db, ledger, plaid, settings
from tests.test_app import H, admin, login, mkuser, user  # noqa: F401  (fixtures)


class FakePlaid:
    def __init__(self):
        self.t = ledger.today()
        self.accounts = [
            {"account_id": "acc-chk", "name": "360 Checking", "mask": "1111", "type": "depository", "subtype": "checking",
             "balances": {"current": 2500.55, "available": 2400.00, "limit": None}},
            {"account_id": "acc-cc", "name": "Quicksilver", "mask": "4417", "type": "credit", "subtype": "credit card",
             "balances": {"current": 812.40, "available": 4187.60, "limit": 5000}},
            {"account_id": "acc-brk", "name": "Schwab One", "mask": "9999", "type": "investment", "subtype": "brokerage",
             "balances": {"current": 15234.10}},
        ]
        self.pages, self.calls, self.fail = [], [], None
        self.inv_error, self.holdings, self.securities = None, [], []

    def __call__(self, path, body):
        self.calls.append(path)
        if self.fail and path in ("/accounts/get", "/transactions/sync"):
            raise plaid.PlaidError(self.fail, "the login details of this item have changed")
        if path == "/link/token/create":
            return {"link_token": "link-sandbox-abc"}
        if path == "/item/public_token/exchange":
            return {"item_id": "item-1", "access_token": "access-production-SECRET123"}
        if path == "/accounts/get":
            return {"accounts": self.accounts}
        if path == "/transactions/sync":
            if self.pages:
                return self.pages.pop(0)
            return {"added": [], "modified": [], "removed": [], "next_cursor": body["cursor"], "has_more": False}
        if path == "/item/remove":
            return {}
        if path == "/investments/holdings/get":
            if self.inv_error:
                raise plaid.PlaidError(self.inv_error, "")
            return {"holdings": self.holdings, "securities": self.securities}
        raise AssertionError(path)


def txn(tid, acct, days, amount, name, primary, detailed="", pending=False, base=None):
    d = (base + timedelta(days=days)).isoformat()
    return {"transaction_id": tid, "account_id": acct, "date": d, "authorized_date": d, "amount": amount, "name": name,
            "merchant_name": name, "pending": pending, "personal_finance_category": {"primary": primary, "detailed": detailed}}


@pytest.fixture
def fake(monkeypatch):
    f = FakePlaid()
    monkeypatch.setattr(plaid, "call", f)
    settings.update({"plaid_client_id": "abc123", "plaid_secret": "sekrit-value", "plaid_env": "sandbox"})
    with db.tx() as con:
        for t_ in ("plaid_items", "plaid_txns", "payments", "income_received", "expenses", "bills", "income_sources", "accounts"):
            con.execute(f"DELETE FROM {t_}")
    yield f
    settings.update({"plaid_client_id": "", "plaid_secret": ""})


def test_link_map_sync(user, fake):
    t = ledger.today()
    assert user.post("/api/plaid/link-token", json={}, headers=H).json()["link_token"] == "link-sandbox-abc"
    r = user.post("/api/plaid/exchange", json={"public_token": "public-sandbox-xyz", "institution_name": "Capital One",
                                                 "import_since": (t - timedelta(days=30)).isoformat()}, headers=H)
    assert r.status_code == 200, r.text
    with db.tx() as con:
        enc = con.execute("SELECT access_token_enc FROM plaid_items").fetchone()[0]
    assert "SECRET123" not in enc                                     # access token sealed
    st = user.get("/api/plaid/status").json()
    accts = {a["name"]: a for a in st["items"][0]["accounts"]}
    assert len(accts) == 3
    for name, kind in (("360 Checking", "account"), ("Quicksilver", "account"), ("Schwab One", "investment")):
        assert user.put(f"/api/plaid/accounts/{accts[name]['id']}", json={"map_kind": kind, "create": True}, headers=H).status_code == 200
    bt = {a["name"]: a for a in user.get("/api/accounts").json()}
    assert bt["360 Checking"]["balance_cents"] == 250055 and bt["360 Checking"]["kind"] == "checking"
    assert bt["Quicksilver"]["kind"] == "credit" and bt["Quicksilver"]["credit_limit_cents"] == 500000
    assert bt["Quicksilver"]["last4"] == "4417"
    assert user.get("/api/investments").json()["value_cents"] >= 1523410

    chk = bt["360 Checking"]["id"]
    due = t - timedelta(days=2)
    user.post("/api/bills", json={"name": "Electric", "amount_cents": 13800, "account_id": chk, "start_date": due.isoformat()}, headers=H)
    src = user.post("/api/income-sources", json={"name": "Stafford", "amount_cents": 142000, "account_id": chk,
                                                  "start_date": (t - timedelta(days=1)).isoformat()}, headers=H).json()
    fake.pages = [{
        "added": [
            txn("t1", "acc-chk", 0, 138.50, "PSEG Electric", "RENT_AND_UTILITIES", base=due),            # pays the bill (within 2%/$1)
            txn("t2", "acc-chk", 0, -1612.33, "STAFFORD PAYROLL", "INCOME", base=t - timedelta(days=1)),   # paycheck w/ OT
            txn("t3", "acc-cc", 0, 54.12, "ShopRite", "FOOD_AND_DRINK", base=t),                          # spending
            txn("t4", "acc-chk", 0, 500.00, "Transfer to savings", "TRANSFER_OUT", base=t),               # skipped
            txn("t5", "acc-chk", 0, 300.00, "Capital One payment", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", base=t),
            txn("t6", "acc-cc", 0, 9.99, "Pending thing", "GENERAL_MERCHANDISE", pending=True, base=t),  # waits
            txn("t7", "acc-cc", 0, 20.00, "Old purchase", "GENERAL_MERCHANDISE", base=t - timedelta(days=90)),  # before import_since
        ], "modified": [], "removed": [], "next_cursor": "c1", "has_more": False}]
    res = user.post("/api/plaid/sync", headers=H).json()
    s = list(res.values())[0]
    assert s["bills_paid"] == 1 and s["pay_received"] == 1 and s["expenses"] == 1, s
    w = user.get(f"/api/summary?start={(t - timedelta(days=5)).isoformat()}&end={t.isoformat()}&skipped=true").json()
    assert [b["status"] for b in w["bills"] if b["name"] == "Electric"] == ["paid"]
    assert [b["amount_cents"] for b in w["bills"] if b["name"] == "Electric"] == [13850]
    assert [p["amount_cents"] for p in w["pay"] if p["received"]] == [161233]
    assert [(x["name"], x["category"]) for x in w["expenses"]] == [("ShopRite", "Food & Drink")]
    # removal undoes what it created
    fake.pages = [{"added": [], "modified": [], "removed": [{"transaction_id": "t3"}], "next_cursor": "c2", "has_more": False}]
    user.post("/api/plaid/sync", headers=H)
    assert user.get(f"/api/expenses?start={t.isoformat()}&end={t.isoformat()}").json() == []
    # re-delivered transaction is not double counted
    fake.pages = [{"added": [txn("t2", "acc-chk", 0, -1612.33, "STAFFORD PAYROLL", "INCOME", base=t - timedelta(days=1))],
                   "modified": [], "removed": [], "next_cursor": "c3", "has_more": False}]
    assert list(user.post("/api/plaid/sync", headers=H).json().values())[0]["added"] == 0


def test_login_required_and_permissions(user, fake):
    user.post("/api/plaid/exchange", json={"public_token": "public-sandbox-xyz", "institution_name": "Citi"}, headers=H)
    fake.fail = "ITEM_LOGIN_REQUIRED"
    user.post("/api/plaid/sync", headers=H)
    it = user.get("/api/plaid/status").json()["items"][0]
    assert it["status"] == "login_required"
    assert user.post("/api/plaid/link-token", json={"item": it["id"]}, headers=H).status_code == 200   # reconnect
    mkuser("mem5", "user")
    assert login("mem5").get("/api/plaid/status").json()["items"] == []          # other users see nothing
    mkuser("ops", "admin")
    assert login("ops").get("/api/plaid/status").status_code == 403             # admins don't do finances
    tok = user.post("/api/me/tokens", json={"name": "x", "scope": "write"}, headers=H).json()["token"]
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/api/plaid/status", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    assert user.delete(f"/api/plaid/items/{it['id']}", headers=H).status_code == 200
    assert "/item/remove" in fake.calls and user.get("/api/plaid/status").json()["items"] == []


def test_plaid_secret_sealed(admin, fake):  # Plaid keys are a system setting: admin
    with db.tx() as con:
        raw = con.execute("SELECT value FROM settings WHERE key='plaid_secret'").fetchone()[0]
    assert "sekrit" not in raw
    assert "plaid_secret" not in admin.get("/api/admin/settings").json()


def test_bill_autopilot(user, fake):
    """Name matching, 'varies' amounts, price-change and due-day suggestions, and finding new recurring bills."""
    t = ledger.today()
    with db.tx() as con:
        con.execute("DELETE FROM bill_suggestions"); con.execute("DELETE FROM notifications")
    user.post("/api/plaid/exchange", json={"public_token": "public-sandbox-xyz", "institution_name": "Capital One",
                                           "import_since": (t - timedelta(days=175)).isoformat()}, headers=H)
    st = user.get("/api/plaid/status").json()
    chk = [a for a in st["items"][0]["accounts"] if a["name"] == "360 Checking"][0]
    user.put(f"/api/plaid/accounts/{chk['id']}", json={"map_kind": "account", "create": True}, headers=H)
    acct = [a for a in user.get("/api/accounts").json() if a["name"] == "360 Checking"][0]["id"]

    def month(n, day):   # the given day, n months ago
        y, m = t.year, t.month - n
        while m < 1:
            m += 12; y -= 1
        import calendar as _c
        return date(y, m, min(day, _c.monthrange(y, m)[1]))
    from datetime import date
    # a 'varies' phone bill (payee differs from the bill name) and a fixed electric bill due on the 10th
    user.post("/api/bills", json={"name": "Cellphone", "amount_cents": 4000, "account_id": acct, "recurrence": "monthly",
                                  "start_date": month(4, 10).isoformat(), "amount_mode": "varies", "match_name": "t mobile"}, headers=H)
    user.post("/api/bills", json={"name": "PSEG", "amount_cents": 13800, "account_id": acct, "recurrence": "monthly",
                                  "start_date": month(4, 10).isoformat()}, headers=H)
    added = []
    for n in (3, 2, 1):
        added += [txn(f"tm{n}", "acc-chk", 0, 40 + n * 3.5, "T-MOBILE *AUTOPAY", "RENT_AND_UTILITIES", base=month(n, 10)),
                  # PSEG is always charged 2 days late, and the last one costs more than expected
                  txn(f"ps{n}", "acc-chk", 0, 138.0 if n > 1 else 162.0, "PSEG LONG ISLAND", "RENT_AND_UTILITIES", base=month(n, 12)),
                  # Netflix isn't a bill yet
                  txn(f"nf{n}", "acc-chk", 0, 15.49, "NETFLIX.COM", "ENTERTAINMENT", base=month(n, 5))]
    added.append(txn("nf0", "acc-chk", 0, 15.49, "NETFLIX.COM", "ENTERTAINMENT", base=month(0, 5) if month(0, 5) <= t else month(4, 5)))
    fake.pages = [{"added": added, "modified": [], "removed": [], "next_cursor": "a1", "has_more": False}]
    user.post("/api/plaid/sync", headers=H)

    bills = {b["name"]: b for b in user.get("/api/bills").json()}
    assert bills["Cellphone"]["amount_cents"] == 4350                       # varies: follows the latest charge
    assert any("Cellphone now expected" in n["title"] for n in user.get("/api/me/notifications").json())
    sugg = user.get("/api/bills/suggestions").json()
    kinds = {s["kind"]: s for s in sugg}
    assert kinds["amount"]["payload"]["to"] == 16200                        # PSEG price change -> suggestion only
    assert bills["PSEG"]["amount_cents"] == 13800                            # fixed bills never change by themselves
    assert kinds["due_day"]["payload"] == {"name": "PSEG", "from": 10, "to": 12}
    nb = kinds["new_bill"]["payload"]
    assert nb["name"] == "Netflix" and nb["recurrence"] == "monthly" and nb["amount_cents"] == 1549
    # accept all three
    for s in sugg:
        assert user.post(f"/api/bills/suggestions/{s['id']}/accept", headers=H).status_code == 200
    bills = {b["name"]: b for b in user.get("/api/bills").json()}
    assert bills["PSEG"]["amount_cents"] == 16200 and bills["PSEG"]["start_date"].endswith("-12")
    assert bills["Netflix"]["recurrence"] == "monthly" and bills["Netflix"]["account_id"] == acct
    assert user.get("/api/bills/suggestions").json() == []
    # dismissed / accepted suggestions don't come back; another user never sees them
    assert user.post("/api/bills/suggestions/scan", headers=H).json()["new"] == 0
    mkuser("other5")
    assert login("other5").get("/api/bills/suggestions").json() == []



def test_positions_from_bank(user, fake):
    user.post("/api/plaid/exchange", json={"public_token": "public-sandbox-xyz", "institution_name": "Schwab"}, headers=H)
    st = user.get("/api/plaid/status").json()
    brk = [a for a in st["items"][0]["accounts"] if a["name"] == "Schwab One"][0]
    user.put(f"/api/plaid/accounts/{brk['id']}", json={"map_kind": "investment", "create": True}, headers=H)
    inv_id = [a for a in user.get("/api/investments/accounts").json() if a["name"] == "Schwab One"][0]["id"]
    # a manual holding in the same account stays untouched
    user.post("/api/investments/holdings", json={"inv_account_id": inv_id, "symbol": "MYOWN", "quantity": 1, "price_cents": 100}, headers=H)
    fake.securities = [{"security_id": "s-vti", "ticker_symbol": "VTI", "name": "Vanguard Total Stock Market ETF", "type": "etf"},
                       {"security_id": "s-aapl", "ticker_symbol": "AAPL", "name": "Apple Inc.", "type": "equity"},
                       {"security_id": "s-cash", "ticker_symbol": "CUR:USD", "name": "U.S. Dollar", "type": "cash", "is_cash_equivalent": True}]
    fake.holdings = [{"account_id": "acc-brk", "security_id": "s-vti", "quantity": 42.5, "institution_price": 301.25, "cost_basis": 9500.00},
                     {"account_id": "acc-brk", "security_id": "s-aapl", "quantity": 10, "institution_price": 230.10, "cost_basis": 1800.00},
                     {"account_id": "acc-brk", "security_id": "s-cash", "quantity": 812.4, "institution_value": 812.40}]
    r = list(user.post("/api/plaid/sync", headers=H).json().values())[0]
    assert r["positions"] == 2
    inv = [a for a in user.get("/api/investments").json()["accounts"] if a["id"] == inv_id][0]
    h = {x["symbol"]: x for x in inv["holdings"]}
    assert h["VTI"]["quantity"] == 42.5 and h["VTI"]["price_cents"] == 30125 and h["VTI"]["cost_basis_cents"] == 950000
    assert h["VTI"]["source"] == "bank" and h["MYOWN"]["source"] == "manual" and inv["cash_cents"] == 81240
    # sold AAPL: removed on next sync; VTI updated
    fake.holdings = fake.holdings[:1]; fake.holdings[0]["quantity"] = 50
    user.post("/api/plaid/sync", headers=H)
    h = {x["symbol"]: x for x in [a for a in user.get("/api/investments").json()["accounts"] if a["id"] == inv_id][0]["holdings"]}
    assert set(h) == {"VTI", "MYOWN"} and h["VTI"]["quantity"] == 50
    # bank didn't grant investments: clear message, balances still sync, nothing deleted
    fake.inv_error = "ADDITIONAL_CONSENT_REQUIRED"
    r = list(user.post("/api/plaid/sync", headers=H).json().values())[0]
    assert r["positions_error"] == "ADDITIONAL_CONSENT_REQUIRED" and "error" not in r
    assert "Reconnect" in user.get("/api/plaid/status").json()["items"][0]["positions_status"]
    assert user.post("/api/plaid/link-token", json={"item": st["items"][0]["id"], "add_investments": True}, headers=H).status_code == 200


def test_split_paychecks_follow_the_bank(user, fake):
    t = ledger.today()
    fake.accounts.append({"account_id": "acc-sav", "name": "360 Savings", "mask": "2222", "type": "depository",
                          "subtype": "savings", "balances": {"current": 9000.00, "available": 9000.00, "limit": None}})
    with db.tx() as con:
        con.execute("DELETE FROM bill_suggestions"); con.execute("DELETE FROM notifications")
    user.post("/api/plaid/exchange", json={"public_token": "public-sandbox-xyz", "institution_name": "Capital One",
                                           "import_since": (t - timedelta(days=60)).isoformat()}, headers=H)
    st = user.get("/api/plaid/status").json()
    for a in st["items"][0]["accounts"]:
        if a["name"] in ("360 Checking", "360 Savings"):
            user.put(f"/api/plaid/accounts/{a['id']}", json={"map_kind": "account", "create": True}, headers=H)
    acc = {a["name"]: a["id"] for a in user.get("/api/accounts").json()}
    chk, sav = acc["360 Checking"], acc["360 Savings"]
    first = t - timedelta(days=14)
    # $1500 a week: $300 to savings, the rest to checking; follows the bank
    src = user.post("/api/income-sources", json={"name": "Acme", "amount_cents": 150000, "account_id": chk, "start_date": first.isoformat(),
                                                 "recurrence": "weekly", "splits": [{"account_id": sav, "amount_cents": 30000}],
                                                 "amount_mode": "varies", "match_name": "acme payroll"}, headers=H).json()
    assert user.post("/api/income-sources", json={"name": "Bad", "amount_cents": 1, "account_id": chk, "start_date": first.isoformat(),
                                                  "splits": [{"account_id": 999999, "amount_cents": 5}]}, headers=H).status_code == 422
    # expected split shows up per account before anything arrives
    wk = user.get(f"/api/week?day={t.isoformat()}").json()
    by = {x["account"]: x["in_cents"] for x in wk["by_account"]}
    assert by.get("360 Savings") == 30000 and by.get("360 Checking") == 120000
    # payday 1: deposits as planned; payday 2: I changed my direct deposit ($450 to savings, total $1550)
    d1, d2 = first, first + timedelta(days=7)
    fake.pages = [{"added": [txn("p1c", "acc-chk", 0, -1200.00, "ACME PAYROLL", "INCOME", base=d1),
                             txn("p1s", "acc-sav", 0, -300.00, "ACME PAYROLL", "INCOME", base=d1),
                             txn("p2c", "acc-chk", 0, -1100.00, "ACME PAYROLL DIR DEP", "INCOME", base=d2),
                             txn("p2s", "acc-sav", 0, -450.00, "ACME PAYROLL DIR DEP", "INCOME", base=d2)],
                   "modified": [], "removed": [], "next_cursor": "c1", "has_more": False}]
    user.post("/api/plaid/sync", headers=H)
    pays = {p["orig_date"]: p for p in user.get(f"/api/week?day={d2.isoformat()}").json()["pay"]}
    assert pays[d2.isoformat()]["received"] and pays[d2.isoformat()]["amount_cents"] == 155000      # both parts add up
    assert sorted(x["amount_cents"] for x in pays[d2.isoformat()]["parts"]) == [45000, 110000]
    s2 = [s for s in user.get("/api/income-sources").json() if s["id"] == src["id"]][0]
    import json as _j
    assert s2["amount_cents"] == 155000 and _j.loads(s2["splits"]) == [{"account_id": sav, "amount_cents": 45000}]
    assert s2["account_id"] == chk
    assert any("Acme updated from your bank" in n["title"] for n in user.get("/api/me/notifications").json())
    # bank reverses the savings part of payday 2: the payday keeps the checking part
    fake.pages = [{"added": [], "modified": [], "removed": [{"transaction_id": "p2s"}], "next_cursor": "c2", "has_more": False}]
    user.post("/api/plaid/sync", headers=H)
    pays = {p["orig_date"]: p for p in user.get(f"/api/week?day={d2.isoformat()}").json()["pay"]}
    assert pays[d2.isoformat()]["amount_cents"] == 110000
    # a 'fixed' paycheck only gets a suggestion
    user.put(f"/api/income-sources/{src['id']}", json={**{k: s2[k] for k in ("name", "account_id", "start_date", "recurrence")},
                                                       "amount_cents": 150000, "splits": [], "amount_mode": "fixed",
                                                       "match_name": "acme payroll"}, headers=H)
    fake.pages = [{"added": [txn("p2s2", "acc-sav", 0, -450.00, "ACME PAYROLL DIR DEP", "INCOME", base=d2)],
                   "modified": [], "removed": [], "next_cursor": "c3", "has_more": False}]
    user.post("/api/plaid/sync", headers=H)
    sg = [x for x in user.get("/api/bills/suggestions").json() if x["kind"] == "pay"]
    assert sg and sg[0]["payload"]["to"] == 155000
    assert [s for s in user.get("/api/income-sources").json() if s["id"] == src["id"]][0]["amount_cents"] == 150000
    user.post(f"/api/bills/suggestions/{sg[0]['id']}/accept", headers=H)
    assert [s for s in user.get("/api/income-sources").json() if s["id"] == src["id"]][0]["amount_cents"] == 155000


def test_refresh_when_opening_app(user, fake):
    user.post("/api/plaid/exchange", json={"public_token": "public-sandbox-xyz", "institution_name": "Capital One"}, headers=H)
    with db.tx() as con:
        con.execute("UPDATE plaid_items SET last_sync='2020-01-01T00:00:00+00:00'")
    assert user.post("/api/plaid/refresh-if-stale", headers=H).json()["started"] is True
    import time as _t
    for _ in range(50):
        _t.sleep(0.1)
        if user.get("/api/plaid/status").json()["items"][0]["last_sync"] > "2021":
            break
    assert user.get("/api/plaid/status").json()["items"][0]["last_sync"] > "2021"     # synced in the background
    assert user.post("/api/plaid/refresh-if-stale", headers=H).json()["started"] is False   # fresh now
    linked = {a["name"]: a["linked"] for a in user.get("/api/accounts/overview").json()["banks"]} \
        if "banks" in user.get("/api/accounts/overview").json() else {}
    assert linked == {} or all(v in (0, 1, True, False) for v in linked.values())
