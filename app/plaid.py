"""Plaid bank sync: balances, transactions, automatic bill-paid / paycheck-received matching.

Talks to Plaid's REST API directly with the standard library (no extra dependency). Only the two official Plaid
hosts are ever contacted, over verified TLS. Access tokens are AES-GCM sealed in the (already encrypted) database
and never leave the server. Read-only: BillTrack only requests the Transactions product."""
import json
import logging
import ssl
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import crypto, ledger, settings
from .db import rows, tx
from .scope import owner

log = logging.getLogger("billtrack.plaid")
HOSTS = {"production": "https://production.plaid.com", "sandbox": "https://sandbox.plaid.com"}
SKIP_OUT = {"TRANSFER_OUT", "TRANSFER_IN", "INCOME"}            # money moving between your own accounts
SKIP_DETAILED = {"LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"}            # card payoff: the purchases already counted
BILL_DAYS, PAY_DAYS = 5, 3                                       # match windows around due date / payday


class PlaidError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status


def configured() -> bool:
    return bool(settings.get("plaid_client_id") and settings.get("plaid_secret"))


def _post(path: str, body: dict) -> dict:
    """The single network call. Swappable in tests."""
    if not configured():
        raise PlaidError("NOT_CONFIGURED", "Add your Plaid client ID and secret in Admin > System settings first")
    url = HOSTS[settings.get("plaid_env")] + path
    data = json.dumps({"client_id": settings.get("plaid_client_id"), "secret": settings.get("plaid_secret"), **body}).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json",
                                                                         "Plaid-Version": "2020-09-14"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:  # noqa: S310
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
        except Exception:
            err = {}
        raise PlaidError(err.get("error_code", f"HTTP_{e.code}"), err.get("display_message") or err.get("error_message", ""), e.code) from None
    except urllib.error.URLError as e:
        raise PlaidError("NETWORK", f"Couldn't reach Plaid ({type(e.reason).__name__})", 503) from None


call = _post   # tests replace this


def _cents(x) -> int | None:
    return None if x is None else round(float(x) * 100)


def _token(item) -> str:
    return crypto.unseal(item["access_token_enc"], f"plaid:{item['item_id']}")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- link ----------
def link_token(user_id: int, item=None) -> str:
    body = {"client_name": "BillTrack", "user": {"client_user_id": f"bt-{user_id}"}, "country_codes": ["US"],
            "language": "en"}
    if item:   # update mode: re-authenticate an existing connection
        body["access_token"] = _token(item)
    else:
        body.update(products=["transactions"], transactions={"days_requested": 180})
    return call("/link/token/create", body)["link_token"]


def exchange(public_token: str, institution_id: str, institution_name: str, user_id: int, import_since: str) -> int:
    r = call("/item/public_token/exchange", {"public_token": public_token})
    item_id, access = r["item_id"], r["access_token"]
    with tx() as con:
        cur = con.execute("""INSERT INTO plaid_items(item_id,access_token_enc,institution_id,institution_name,import_since,
                             created_at,created_by,owner_id) VALUES (?,?,?,?,?,?,?,?)
                             ON CONFLICT(item_id) DO UPDATE SET access_token_enc=excluded.access_token_enc,
                             status='ok', error=''""",
                          (item_id, crypto.seal(access, f"plaid:{item_id}"), institution_id[:64], institution_name[:100],
                           import_since, now(), user_id, owner()))
        local = cur.lastrowid or con.execute("SELECT id FROM plaid_items WHERE item_id=? AND owner_id=?",
                                             (item_id, owner())).fetchone()["id"]
    refresh_accounts(local)
    return local


def remove(local_id: int):
    with tx() as con:
        it = con.execute("SELECT * FROM plaid_items WHERE id=? AND owner_id=?", (local_id, owner())).fetchone()
    if not it:
        return
    try:
        call("/item/remove", {"access_token": _token(it)})
    except PlaidError as e:
        log.warning("Plaid item/remove failed (%s); deleting locally anyway", e.code)
    with tx() as con:
        con.execute("UPDATE inv_accounts SET synced_value_cents=NULL WHERE owner_id=? AND id IN "
                    "(SELECT map_id FROM plaid_accounts WHERE item_id=? AND map_kind='investment')", (owner(), local_id))
        con.execute("DELETE FROM plaid_items WHERE id=? AND owner_id=?", (local_id, owner()))


# ---------- sync ----------
def _set_status(local_id, status, error=""):
    with tx() as con:
        con.execute("UPDATE plaid_items SET status=?, error=? WHERE id=? AND owner_id=?", (status, error[:300], local_id, owner()))


def refresh_accounts(local_id: int) -> list:
    with tx() as con:
        it = con.execute("SELECT * FROM plaid_items WHERE id=? AND owner_id=?", (local_id, owner())).fetchone()
    if not it:
        raise PlaidError("NOT_FOUND", "No such bank link")
    r = call("/accounts/get", {"access_token": _token(it)})
    with tx() as con:
        for a in r["accounts"]:
            b = a.get("balances") or {}
            con.execute("""INSERT INTO plaid_accounts(item_id,account_id,name,official_name,mask,type,subtype,
                           current_cents,available_cents,limit_cents,updated_at,owner_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(account_id) DO UPDATE SET name=excluded.name, official_name=excluded.official_name,
                           mask=excluded.mask, current_cents=excluded.current_cents, available_cents=excluded.available_cents,
                           limit_cents=excluded.limit_cents, updated_at=excluded.updated_at""",
                        (local_id, a["account_id"], (a.get("name") or "")[:100], (a.get("official_name") or "")[:100],
                         (a.get("mask") or "")[-4:], a.get("type") or "", a.get("subtype") or "",
                         _cents(b.get("current")), _cents(b.get("available")), _cents(b.get("limit")), now(), owner()))
        _apply_balances(con, local_id)
        return rows(con, "SELECT * FROM plaid_accounts WHERE item_id=? AND owner_id=?", (local_id, owner()))


def _apply_balances(con, local_id):
    """Push Plaid balances into the mapped BillTrack accounts (cards/loans: amount owed; banks: current)."""
    for pa in rows(con, "SELECT * FROM plaid_accounts WHERE item_id=? AND owner_id=? AND map_kind IN ('account','investment')",
                   (local_id, owner())):
        if pa["current_cents"] is None:
            continue
        if pa["map_kind"] == "investment":
            con.execute("UPDATE inv_accounts SET synced_value_cents=? WHERE id=? AND owner_id=?",
                        (pa["current_cents"], pa["map_id"], owner()))
            continue
        acct = con.execute("SELECT id, kind FROM accounts WHERE id=? AND owner_id=?", (pa["map_id"], owner())).fetchone()
        if not acct:
            continue
        upd = {"balance_cents": pa["current_cents"], "balance_updated": now()}
        if acct["kind"] == "credit" and pa["limit_cents"]:
            upd["credit_limit_cents"] = pa["limit_cents"]
        con.execute(f"UPDATE accounts SET {','.join(f'{k}=?' for k in upd)} WHERE id=? AND owner_id=?",
                    (*upd.values(), acct["id"], owner()))
        ledger.record_balance(con, acct["id"], pa["current_cents"])


def _category(t) -> tuple[str, str]:
    pfc = t.get("personal_finance_category") or {}
    return (pfc.get("primary") or "").upper(), (pfc.get("detailed") or "").upper()


def _pretty(primary: str) -> str:
    return primary.replace("_", " ").title().replace(" And ", " & ")[:60] if primary else "Uncategorized"


def _match_bill(con, account_id, d: date, amount: int):
    """An unpaid bill occurrence on this account (or no account), due within ±BILL_DAYS, amount within 2% / $1."""
    best = None
    for b in ledger.bill_occurrences(con, d - timedelta(days=BILL_DAYS), d + timedelta(days=BILL_DAYS)):
        if b["status"] in ("paid", "skipped") or (b["account_id"] not in (None, account_id)):
            continue
        tol = max(100, round(b["amount_cents"] * 0.02))
        if abs(b["amount_cents"] - amount) <= tol:
            dist = abs((date.fromisoformat(b["date"]) - d).days) + (0 if b["account_id"] == account_id else 2)
            if best is None or dist < best[0]:
                best = (dist, b)
    return best[1] if best else None


def _match_pay(con, account_id, d: date, amount: int):
    """An un-received payday into this account within ±PAY_DAYS; take-home may vary (OT), so allow -40%/+75%."""
    best = None
    for p in ledger.pay_occurrences(con, d - timedelta(days=PAY_DAYS), d + timedelta(days=PAY_DAYS)):
        if p["received"] or p["skipped"] or p["account_id"] not in (None, account_id):
            continue
        if p["expected_cents"] * 0.6 <= amount <= p["expected_cents"] * 1.75:
            dist = abs((date.fromisoformat(p["date"]) - d).days)
            if best is None or dist < best[0]:
                best = (dist, p)
    return best[1] if best else None


def _apply_txn(con, t, pa, since: str) -> str:
    """Record one posted transaction. Returns what happened."""
    tid = t["transaction_id"]
    if con.execute("SELECT 1 FROM plaid_txns WHERE transaction_id=?", (tid,)).fetchone():
        return "dup"
    d = date.fromisoformat(t.get("authorized_date") or t["date"]) if t.get("authorized_date") else date.fromisoformat(t["date"])
    amt = _cents(t["amount"])                      # Plaid: positive = money out of the account
    name = (t.get("merchant_name") or t.get("name") or "Transaction")[:100]
    primary, detailed = _category(t)
    out, exp_id, pay_id, rec_id = "ignored", None, None, None
    acct_id = pa["map_id"]
    if d.isoformat() >= since and pa["map_kind"] == "account":
        if amt > 0 and detailed not in SKIP_DETAILED and primary not in SKIP_OUT:
            bill = _match_bill(con, acct_id, d, amt)
            if bill:
                cur = con.execute("""INSERT INTO payments(bill_id,due_date,paid_on,amount_cents,source,owner_id) VALUES (?,?,?,?,'bank',?)
                                     ON CONFLICT(bill_id,due_date) DO NOTHING""",
                                  (bill["bill_id"], bill["orig_date"], d.isoformat(), amt, owner()))
                pay_id, out = cur.lastrowid if cur.rowcount else None, f"bill:{bill['name']}"
            else:
                cur = con.execute("INSERT INTO expenses(date,name,amount_cents,account_id,category,source,owner_id) "
                                  "VALUES (?,?,?,?,?,'bank',?)", (d.isoformat(), name, amt, acct_id, _pretty(primary), owner()))
                exp_id, out = cur.lastrowid, "expense"
        elif amt < 0 and primary == "INCOME":
            p = _match_pay(con, acct_id, d, -amt)
            if p:
                cur = con.execute("""INSERT INTO income_received(source_id,pay_date,amount_cents,source,owner_id) VALUES (?,?,?,'bank',?)
                                     ON CONFLICT(source_id,pay_date) DO NOTHING""", (p["source_id"], p["orig_date"], -amt, owner()))
                rec_id, out = cur.lastrowid if cur.rowcount else None, f"pay:{p['name']}"
        elif primary in SKIP_OUT or detailed in SKIP_DETAILED:
            out = "transfer"
    con.execute("""INSERT INTO plaid_txns(transaction_id,plaid_account_id,date,name,amount_cents,category,outcome,
                   expense_id,payment_id,received_id,seen_at,owner_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, pa["id"], d.isoformat(), name, amt, primary, out, exp_id, pay_id, rec_id, now(), owner()))
    return out


def _undo_txn(con, tid):
    r = con.execute("SELECT * FROM plaid_txns WHERE transaction_id=? AND owner_id=?", (tid, owner())).fetchone()
    if not r:
        return
    if r["expense_id"]:
        con.execute("DELETE FROM expenses WHERE id=? AND source='bank' AND owner_id=?", (r["expense_id"], owner()))
    if r["payment_id"]:
        con.execute("DELETE FROM payments WHERE id=? AND source='bank' AND owner_id=?", (r["payment_id"], owner()))
    if r["received_id"]:
        con.execute("DELETE FROM income_received WHERE id=? AND source='bank' AND owner_id=?", (r["received_id"], owner()))
    con.execute("DELETE FROM plaid_txns WHERE transaction_id=? AND owner_id=?", (tid, owner()))


def sync_item(local_id: int) -> dict:
    stats = {"added": 0, "modified": 0, "removed": 0, "bills_paid": 0, "pay_received": 0, "expenses": 0}
    try:
        refresh_accounts(local_id)
        with tx() as con:
            it = con.execute("SELECT * FROM plaid_items WHERE id=? AND owner_id=?", (local_id, owner())).fetchone()
        cursor, token = it["cursor"], _token(it)
        added, modified, removed = [], [], []
        for _attempt in range(3):            # Plaid asks to restart pagination if data changes mid-way
            try:
                c, added, modified, removed = cursor, [], [], []
                while True:
                    r = call("/transactions/sync", {"access_token": token, "cursor": c, "count": 500})
                    added += r["added"]; modified += r["modified"]; removed += r["removed"]
                    c = r["next_cursor"]
                    if not r["has_more"]:
                        break
                break
            except PlaidError as e:
                if e.code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                    raise
        with tx() as con:
            accts = {a["account_id"]: a for a in rows(con, "SELECT * FROM plaid_accounts WHERE item_id=? AND owner_id=?",
                                                      (local_id, owner()))}
            for t in removed:
                _undo_txn(con, t["transaction_id"]); stats["removed"] += 1
            for t in modified:
                if t.get("pending"):
                    continue
                _undo_txn(con, t["transaction_id"])
                if t["account_id"] in accts:
                    _apply_txn(con, t, accts[t["account_id"]], it["import_since"]); stats["modified"] += 1
            for t in added:
                if t.get("pending") or t["account_id"] not in accts:
                    continue                  # pending items arrive again, final, once they post
                o = _apply_txn(con, t, accts[t["account_id"]], it["import_since"])
                stats["added"] += o != "dup"
                stats["bills_paid"] += o.startswith("bill:")
                stats["pay_received"] += o.startswith("pay:")
                stats["expenses"] += o == "expense"
            con.execute("UPDATE plaid_items SET cursor=?, last_sync=?, status='ok', error='' WHERE id=? AND owner_id=?",
                        (c, now(), local_id, owner()))
            ledger.snapshot_networth(con)
            from . import notify
            notify.account_alerts(con)
    except PlaidError as e:
        status = "login_required" if e.code in ("ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT") else "error"
        _set_status(local_id, status, f"{e.code}: {e.message}")
        stats["error"] = e.code
    return stats


def sync_all(only_due: bool = False, only_owner: int | None = None) -> dict:
    """Scheduler / manual: every user's links, each synced inside that user's own scope."""
    out = {}
    if not configured():
        return out
    from . import scope
    with tx() as con:
        items = rows(con, "SELECT p.id, p.last_sync, p.status, p.institution_name, p.owner_id, u.prefs FROM plaid_items p "
                          "JOIN users u ON u.id=p.owner_id WHERE u.disabled=0 AND u.role='user'")
    for it in items:
        if only_owner and it["owner_id"] != only_owner:
            continue
        if only_due and it["last_sync"]:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(it["last_sync"])
            if age < timedelta(hours=settings.get("plaid_sync_hours")) or it["status"] == "login_required":
                continue
        try:
            ws = json.loads(it["prefs"] or "{}").get("week_start")
        except ValueError:
            ws = None
        with scope.as_user(it["owner_id"], ws if isinstance(ws, int) else None):
            out[it["id"]] = sync_item(it["id"])
            if out[it["id"]].get("error") and it["status"] != "login_required":
                with tx() as con:
                    from . import notify
                    u = con.execute("SELECT * FROM users WHERE id=?", (it["owner_id"],)).fetchone()
                    notify.deliver(con, u, f"Bank sync problem: {it['institution_name']}",
                                   "Open Bank links. If it says to reconnect, sign in to your bank again there.", "warn")
    return out
