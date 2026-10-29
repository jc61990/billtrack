"""Bill autopilot: learn from bank charges (runs inside the owner's scope after each bank sync).

- Name matching: a bill matches charges whose payee contains its key words ("PSEG" matches "PSEG LONG ISLAND").
- 'varies' bills: expected amount follows the latest bank-paid amount (automatic, with an inbox note).
- 'fixed' bills charged a different amount: suggestion to update the amount.
- Due-day drift: the last 3 bank payments landed on the same other day: suggestion to move the due day.
- Recurring charges not set up as bills: suggestion to add them (name, amount, account, schedule pre-filled).
Suggestions are stored per user; dismissed ones are never suggested again."""
import calendar
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from . import ledger
from .db import rows
from .scope import owner

NOISE = {"payment", "pmt", "autopay", "auto", "ach", "debit", "credit", "pos", "purchase", "www", "com", "net", "org",
         "inc", "llc", "co", "corp", "bill", "online", "recurring", "card", "the", "of", "and", "web", "id", "ppd",
         "sq", "tst", "paypal", "des", "indn", "conf", "ref"}


def tokens(s: str) -> list[str]:
    words = re.sub(r"[^a-z ]+", " ", (s or "").lower().replace("*", " ")).split()
    return [w for w in words if len(w) > 1 and w not in NOISE]


def payee_key(name: str) -> str:
    t = tokens(name)
    return " ".join(t[:2]) if t else ""


def name_matches(bill: dict, payee: str) -> bool:
    want = tokens(bill.get("match_name") or bill.get("name") or "")
    have = set(tokens(payee))
    return bool(want) and all(w in have for w in want)


def _suggest(con, kind, key, payload, bill_id=None):
    """Insert once per (owner, key); dismissed/accepted keys are never re-suggested."""
    con.execute("INSERT OR IGNORE INTO bill_suggestions(owner_id,kind,bill_id,key,payload,created_at) VALUES (?,?,?,?,?,?)",
                (owner(), kind, bill_id, key, json.dumps(payload), ledger.today().isoformat()))


def after_bank_payment(con, bill_id: int):
    """Called when a bank charge marked a bill paid."""
    b = con.execute("SELECT * FROM bills WHERE id=? AND owner_id=?", (bill_id, owner())).fetchone()
    if not b:
        return
    pays = rows(con, "SELECT due_date, paid_on, amount_cents FROM payments WHERE bill_id=? AND owner_id=? AND source='bank' "
                     "ORDER BY due_date DESC LIMIT 3", (bill_id, owner()))
    if not pays:
        return
    last = pays[0]["amount_cents"]
    tol = max(100, round(b["amount_cents"] * 0.02))
    if b["amount_mode"] == "varies":
        if abs(last - b["amount_cents"]) > 50:
            con.execute("UPDATE bills SET amount_cents=? WHERE id=?", (last, bill_id))
            _note(con, f"{b['name']} now expected at ${last / 100:,.2f}",
                  f"Your bank charged ${last / 100:,.2f} (was expecting ${b['amount_cents'] / 100:,.2f}). "
                  "Future weeks use the new amount. Change this in Bills if it's wrong.")
    elif abs(last - b["amount_cents"]) > tol:
        _suggest(con, "amount", f"amount:{bill_id}:{last}", {"name": b["name"], "from": b["amount_cents"], "to": last}, bill_id)
    # due-day drift (monthly-ish bills): last 3 bank payments all landed the same number of days off
    if b["recurrence"] in ("monthly", "quarterly", "yearly") and len(pays) == 3:
        offs = [(date.fromisoformat(p["paid_on"]) - date.fromisoformat(p["due_date"])).days for p in pays]
        if all(o == offs[0] for o in offs) and offs[0] != 0 and abs(offs[0]) <= 10:
            new_day = (date.fromisoformat(pays[0]["due_date"]) + timedelta(days=offs[0])).day
            if new_day != date.fromisoformat(b["start_date"]).day:
                _suggest(con, "due_day", f"due:{bill_id}:{new_day}",
                         {"name": b["name"], "from": date.fromisoformat(b["start_date"]).day, "to": new_day}, bill_id)


def _note(con, title, body):
    from .security import iso, now_utc
    con.execute("INSERT INTO notifications(user_id,ts,level,title,body) VALUES (?,?,'info',?,?)",
                (owner(), iso(now_utc()), title, body))


CADENCE = [  # (recurrence, typical days, tolerance, minimum occurrences)
    ("weekly", 7, 1, 4), ("biweekly", 14, 2, 3), ("monthly", 30.4, 4, 3), ("quarterly", 91, 7, 2)]


def scan_recurring(con, days: int = 200) -> int:
    """Look for repeating bank charges that aren't bills yet. Returns how many new suggestions were made."""
    since = (ledger.today() - timedelta(days=days)).isoformat()
    exps = rows(con, "SELECT date, name, amount_cents, account_id FROM expenses WHERE owner_id=? AND source='bank' "
                     "AND date>=? ORDER BY date", (owner(), since))
    bills = rows(con, "SELECT name, match_name FROM bills WHERE owner_id=?", (owner(),))
    groups = defaultdict(list)
    for e in exps:
        k = payee_key(e["name"])
        if k:
            groups[k].append(e)
    made = 0
    for k, g in groups.items():
        if any(name_matches(b, g[-1]["name"]) for b in bills):
            continue
        ds = [date.fromisoformat(e["date"]) for e in g]
        gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
        if not gaps:
            continue
        gap = median(gaps)
        for rec, typical, tol, need in CADENCE:
            if len(g) >= need and abs(gap - typical) <= tol and all(abs(x - typical) <= tol * 2 for x in gaps):
                amts = [e["amount_cents"] for e in g]
                varies = (max(amts) - min(amts)) > max(100, median(amts) * 0.05)
                last = ds[-1]
                if rec == "monthly":
                    nm = last.month % 12 + 1
                    ny = last.year + (last.month == 12)
                    nxt = date(ny, nm, min(last.day, calendar.monthrange(ny, nm)[1]))
                else:
                    nxt = last + timedelta(days=round(typical))
                pretty = " ".join(w.capitalize() for w in k.split())
                before = con.total_changes
                _suggest(con, "new_bill", f"new:{k}:{rec}",
                         {"name": pretty, "match_name": k, "amount_cents": amts[-1], "recurrence": rec,
                          "start_date": nxt.isoformat(), "account_id": g[-1]["account_id"], "amount_mode": "varies" if varies else "fixed",
                          "seen": len(g), "examples": [g[-1]["name"]]})
                made += con.total_changes > before
                break
    return made


def apply(con, s: dict):
    """Accept a suggestion."""
    p = json.loads(s["payload"])
    if s["kind"] == "amount":
        con.execute("UPDATE bills SET amount_cents=? WHERE id=? AND owner_id=?", (p["to"], s["bill_id"], owner()))
    elif s["kind"] == "due_day":
        b = con.execute("SELECT start_date FROM bills WHERE id=? AND owner_id=?", (s["bill_id"], owner())).fetchone()
        if b:
            d = date.fromisoformat(b["start_date"])
            nd = date(d.year, d.month, min(p["to"], calendar.monthrange(d.year, d.month)[1]))
            con.execute("UPDATE bills SET start_date=? WHERE id=? AND owner_id=?", (nd.isoformat(), s["bill_id"], owner()))
    elif s["kind"] == "new_bill":
        acct = p.get("account_id")
        if acct and not con.execute("SELECT 1 FROM accounts WHERE id=? AND owner_id=?", (acct, owner())).fetchone():
            acct = None
        con.execute("INSERT INTO bills(name,amount_cents,account_id,start_date,recurrence,autopay,amount_mode,match_name,"
                    "category,owner_id) VALUES (?,?,?,?,?,1,?,?,'',?)",
                    (p["name"][:100], p["amount_cents"], acct, p["start_date"], p["recurrence"], p["amount_mode"],
                     p["match_name"][:60], owner()))
    elif s["kind"] == "pay":
        con.execute("UPDATE income_sources SET amount_cents=?, account_id=?, splits=? WHERE id=? AND owner_id=?",
                    (p["to"], p["account_id"], json.dumps(p["splits"]), s["bill_id"], owner()))
    con.execute("UPDATE bill_suggestions SET status='accepted' WHERE id=?", (s["id"],))



def learn_income(con):
    """After deposits arrive: for 'varies' paychecks, update the expected amount and the per-account split to match the
    latest bank-recorded payday (with a note). For 'fixed' ones, suggest it when the total or the split changed."""
    for src in rows(con, "SELECT * FROM income_sources WHERE owner_id=? AND active=1", (owner(),)):
        last = con.execute("SELECT pay_date FROM income_deposits WHERE owner_id=? AND source_id=? ORDER BY pay_date DESC LIMIT 1",
                           (owner(), src["id"])).fetchone()
        if not last:
            continue
        parts = rows(con, "SELECT account_id, sum(amount_cents) amount_cents FROM income_deposits WHERE owner_id=? AND source_id=? "
                          "AND pay_date=? GROUP BY account_id ORDER BY amount_cents", (owner(), src["id"], last["pay_date"]))
        total = sum(x["amount_cents"] for x in parts)
        main = max(parts, key=lambda x: x["amount_cents"])["account_id"]       # biggest share = main account (the rest)
        splits = [{"account_id": x["account_id"], "amount_cents": x["amount_cents"]} for x in parts if x["account_id"] != main]
        try:
            cur = json.loads(src["splits"] or "[]")
        except ValueError:
            cur = []
        same_split = sorted((x["account_id"], x["amount_cents"]) for x in splits) == \
            sorted((x.get("account_id"), x.get("amount_cents")) for x in cur) and main == src["account_id"]
        if abs(total - src["amount_cents"]) <= 50 and same_split:
            continue
        names = {a["id"]: a["name"] for a in rows(con, "SELECT id, name FROM accounts WHERE owner_id=?", (owner(),))}
        desc = ", ".join(f"${x['amount_cents'] / 100:,.2f} to {names.get(x['account_id'], 'an account')}" for x in splits)
        desc = (desc + f", the rest to {names.get(main, 'the main account')}") if desc else f"all to {names.get(main, 'one account')}"
        if src["amount_mode"] == "varies":
            con.execute("UPDATE income_sources SET amount_cents=?, account_id=?, splits=? WHERE id=?",
                        (total, main, json.dumps(splits), src["id"]))
            _note(con, f"{src['name']} updated from your bank: ${total / 100:,.2f}",
                  f"Latest paycheck on {last['pay_date']}: {desc}. Future weeks use this.")
        else:
            _suggest(con, "pay", f"pay:{src['id']}:{last['pay_date']}",
                     {"name": src["name"], "from": src["amount_cents"], "to": total, "account_id": main, "splits": splits,
                      "desc": desc}, None)
            con.execute("UPDATE bill_suggestions SET bill_id=? WHERE owner_id=? AND key=?",
                        (src["id"], owner(), f"pay:{src['id']}:{last['pay_date']}"))
