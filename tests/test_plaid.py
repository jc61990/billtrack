"""Bank sync against a simulated Plaid (no network)."""
from datetime import timedelta

import pytest

from app import db, ledger, plaid, settings
from tests.test_app import H, admin, login, mkuser  # noqa: F401  (fixture)


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


def test_link_map_sync(admin, fake):
    t = ledger.today()
    assert admin.post("/api/plaid/link-token", json={}, headers=H).json()["link_token"] == "link-sandbox-abc"
    r = admin.post("/api/plaid/exchange", json={"public_token": "public-sandbox-xyz", "institution_name": "Capital One",
                                                 "import_since": (t - timedelta(days=30)).isoformat()}, headers=H)
    assert r.status_code == 200, r.text
    with db.tx() as con:
        enc = con.execute("SELECT access_token_enc FROM plaid_items").fetchone()[0]
    assert "SECRET123" not in enc                                     # access token sealed
    st = admin.get("/api/plaid/status").json()
    accts = {a["name"]: a for a in st["items"][0]["accounts"]}
    assert len(accts) == 3
    for name, kind in (("360 Checking", "account"), ("Quicksilver", "account"), ("Schwab One", "investment")):
        assert admin.put(f"/api/plaid/accounts/{accts[name]['id']}", json={"map_kind": kind, "create": True}, headers=H).status_code == 200
    bt = {a["name"]: a for a in admin.get("/api/accounts").json()}
    assert bt["360 Checking"]["balance_cents"] == 250055 and bt["360 Checking"]["kind"] == "checking"
    assert bt["Quicksilver"]["kind"] == "credit" and bt["Quicksilver"]["credit_limit_cents"] == 500000
    assert bt["Quicksilver"]["last4"] == "4417"
    assert admin.get("/api/investments").json()["value_cents"] >= 1523410

    chk = bt["360 Checking"]["id"]
    due = t - timedelta(days=2)
    admin.post("/api/bills", json={"name": "Electric", "amount_cents": 13800, "account_id": chk, "start_date": due.isoformat()}, headers=H)
    src = admin.post("/api/income-sources", json={"name": "Stafford", "amount_cents": 142000, "account_id": chk,
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
    res = admin.post("/api/plaid/sync", headers=H).json()
    s = list(res.values())[0]
    assert s["bills_paid"] == 1 and s["pay_received"] == 1 and s["expenses"] == 1, s
    w = admin.get(f"/api/summary?start={(t - timedelta(days=5)).isoformat()}&end={t.isoformat()}&skipped=true").json()
    assert [b["status"] for b in w["bills"] if b["name"] == "Electric"] == ["paid"]
    assert [b["amount_cents"] for b in w["bills"] if b["name"] == "Electric"] == [13850]
    assert [p["amount_cents"] for p in w["pay"] if p["received"]] == [161233]
    assert [(x["name"], x["category"]) for x in w["expenses"]] == [("ShopRite", "Food & Drink")]
    # removal undoes what it created
    fake.pages = [{"added": [], "modified": [], "removed": [{"transaction_id": "t3"}], "next_cursor": "c2", "has_more": False}]
    admin.post("/api/plaid/sync", headers=H)
    assert admin.get(f"/api/expenses?start={t.isoformat()}&end={t.isoformat()}").json() == []
    # re-delivered transaction is not double counted
    fake.pages = [{"added": [txn("t2", "acc-chk", 0, -1612.33, "STAFFORD PAYROLL", "INCOME", base=t - timedelta(days=1))],
                   "modified": [], "removed": [], "next_cursor": "c3", "has_more": False}]
    assert list(admin.post("/api/plaid/sync", headers=H).json().values())[0]["added"] == 0


def test_login_required_and_permissions(admin, fake):
    admin.post("/api/plaid/exchange", json={"public_token": "public-sandbox-xyz", "institution_name": "Citi"}, headers=H)
    fake.fail = "ITEM_LOGIN_REQUIRED"
    admin.post("/api/plaid/sync", headers=H)
    it = admin.get("/api/plaid/status").json()["items"][0]
    assert it["status"] == "login_required"
    assert admin.post("/api/plaid/link-token", json={"item": it["id"]}, headers=H).status_code == 200   # reconnect
    mkuser("mem5", "member")
    assert login("mem5").get("/api/plaid/status").status_code == 403
    tok = admin.post("/api/me/tokens", json={"name": "x", "scope": "write"}, headers=H).json()["token"]
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/api/plaid/status", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    assert admin.delete(f"/api/plaid/items/{it['id']}", headers=H).status_code == 200
    assert "/item/remove" in fake.calls and admin.get("/api/plaid/status").json()["items"] == []


def test_plaid_secret_sealed(admin, fake):
    with db.tx() as con:
        raw = con.execute("SELECT value FROM settings WHERE key='plaid_secret'").fetchone()[0]
    assert "sekrit" not in raw
    assert "plaid_secret" not in admin.get("/api/admin/settings").json()
