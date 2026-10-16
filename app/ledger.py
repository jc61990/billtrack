"""Money in / money out, trackers, investments, reports. Amounts are integer cents."""
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import settings
from .db import rows
from .recurrence import occurrences, week_bounds

PAD = timedelta(days=62)  # occurrences may be dragged up to 60 days


def today() -> date:
    return datetime.now(settings.tz()).date()


def week_start_of(d: date) -> date:
    return week_bounds(d, settings.get("week_start"))[0]


def _d(s):
    return date.fromisoformat(s) if s else None


def _overrides(con, kind):
    return {(o["item_id"], o["orig_date"]): o for o in rows(
        con, "SELECT * FROM occurrence_overrides WHERE kind=?", (kind,))}


def _expand(item, ov, kind, lo, hi):
    """Yield (effective_date, orig_date, override) after applying moves/skips."""
    for d in occurrences(_d(item["start_date"]), item["recurrence"], lo - PAD, hi + PAD, _d(item["end_date"])):
        o = ov.get((item["id"], d.isoformat()))
        eff = _d(o["new_date"]) if o and o["new_date"] else d
        if lo <= eff <= hi:
            yield eff, d, o


def bill_occurrences(con, lo: date, hi: date, include_skipped=False):
    bills = rows(con, """SELECT b.*, a.name AS account, a.color AS account_color
                         FROM bills b LEFT JOIN accounts a ON a.id=b.account_id WHERE b.active=1""")
    ov = _overrides(con, "bill")
    paid = {(p["bill_id"], p["due_date"]): p for p in rows(
        con, "SELECT * FROM payments WHERE due_date BETWEEN ? AND ?", ((lo - PAD).isoformat(), (hi + PAD).isoformat()))}
    t, out = today(), []
    for b in bills:
        for d, orig, o in _expand(b, ov, "bill", lo, hi):
            skipped = bool(o and o["skipped"])
            if skipped and not include_skipped:
                continue
            p = paid.get((b["id"], orig.isoformat()))
            if skipped:
                status = "skipped"
            elif p:
                status = "paid"
            elif b["autopay"]:
                status = "autopay"
            elif d < t:
                status = "overdue"
            elif (d - t).days <= b["remind_days"]:
                status = "due-soon"
            else:
                status = "upcoming"
            out.append({
                "type": "bill", "bill_id": b["id"], "name": b["name"], "date": d.isoformat(),
                "orig_date": orig.isoformat(), "moved": d != orig, "skipped": skipped,
                "amount_cents": 0 if skipped else (p["amount_cents"] if p else b["amount_cents"]),
                "account_id": b["account_id"], "account": b["account"], "account_color": b["account_color"],
                "category": b["category"] or "Uncategorized", "autopay": bool(b["autopay"]), "status": status,
                "payment_id": p["id"] if p else None, "paid_on": p["paid_on"] if p else None,
                "remind_days": b["remind_days"]})
    return sorted(out, key=lambda x: (x["date"], x["name"].lower()))


def pay_occurrences(con, lo: date, hi: date, include_skipped=False):
    srcs = rows(con, """SELECT s.*, a.name AS account FROM income_sources s
                        LEFT JOIN accounts a ON a.id=s.account_id WHERE s.active=1""")
    ov = _overrides(con, "pay")
    rec = {(r["source_id"], r["pay_date"]): r for r in rows(
        con, "SELECT * FROM income_received WHERE pay_date BETWEEN ? AND ?",
        ((lo - PAD).isoformat(), (hi + PAD).isoformat()))}
    out = []
    for s in srcs:
        for d, orig, o in _expand(s, ov, "pay", lo, hi):
            skipped = bool(o and o["skipped"])
            if skipped and not include_skipped:
                continue
            r = rec.get((s["id"], orig.isoformat()))
            out.append({
                "type": "pay", "source_id": s["id"], "name": s["name"], "date": d.isoformat(),
                "orig_date": orig.isoformat(), "moved": d != orig, "skipped": skipped,
                "expected_cents": s["amount_cents"],
                "amount_cents": 0 if skipped else (r["amount_cents"] if r else s["amount_cents"]),
                "received": bool(r), "received_id": r["id"] if r else None,
                "account_id": s["account_id"], "account": s["account"]})
    return sorted(out, key=lambda x: x["date"])


def extras(con, lo: date, hi: date):
    return rows(con, """SELECT e.*, a.name AS account, s.name AS source FROM income_extra e
                        LEFT JOIN accounts a ON a.id=e.account_id LEFT JOIN income_sources s ON s.id=e.source_id
                        WHERE e.date BETWEEN ? AND ? ORDER BY e.date""", (lo.isoformat(), hi.isoformat()))


def expenses(con, lo: date, hi: date):
    return rows(con, """SELECT x.*, a.name AS account FROM expenses x
                        LEFT JOIN accounts a ON a.id=x.account_id
                        WHERE x.date BETWEEN ? AND ? ORDER BY x.date""", (lo.isoformat(), hi.isoformat()))


def summarize(con, lo: date, hi: date, detail=True, include_skipped=False):
    bills = bill_occurrences(con, lo, hi, include_skipped)
    pays = pay_occurrences(con, lo, hi, include_skipped)
    ext, exp = extras(con, lo, hi), expenses(con, lo, hi)
    pay_total = sum(p["amount_cents"] for p in pays)
    extra_total = sum(e["amount_cents"] for e in ext)
    ot = [e for e in ext if e["kind"] == "overtime"]
    bill_total = sum(b["amount_cents"] for b in bills)
    exp_total = sum(x["amount_cents"] for x in exp)
    unpaid = sum(b["amount_cents"] for b in bills if b["status"] in ("overdue", "due-soon", "upcoming"))

    by_acct: dict = {}
    def acct(name):
        return by_acct.setdefault(name or "Unassigned", {"in_cents": 0, "out_cents": 0})
    for x in pays + ext:
        acct(x["account"])["in_cents"] += x["amount_cents"]
    for x in bills + exp:
        acct(x["account"])["out_cents"] += x["amount_cents"]

    money_in, money_out = pay_total + extra_total, bill_total + exp_total
    res = {"start": lo.isoformat(), "end": hi.isoformat(),
           "in_cents": money_in, "out_cents": money_out, "net_cents": money_in - money_out,
           "pay_cents": pay_total, "extra_cents": extra_total,
           "ot_cents": sum(e["amount_cents"] for e in ot), "ot_hours": sum(e["hours"] or 0 for e in ot),
           "bills_cents": bill_total, "expenses_cents": exp_total, "unpaid_cents": unpaid,
           "by_account": [{"account": k, **v} for k, v in sorted(by_acct.items())]}
    if detail:
        res.update(bills=bills, pay=pays, extras=ext, expenses=exp)
    return res


def week(con, any_day: date):
    lo = week_start_of(any_day)
    return summarize(con, lo, lo + timedelta(days=6), include_skipped=True)


def trend(con, weeks_back: int, weeks_ahead: int):
    lo = week_start_of(today())
    out = []
    for i in range(-weeks_back, weeks_ahead + 1):
        s = lo + timedelta(weeks=i)
        w = summarize(con, s, s + timedelta(days=6), detail=False)
        out.append({k: w[k] for k in ("start", "in_cents", "out_cents", "net_cents", "ot_cents", "ot_hours")})
    return out


# ---------- trackers ----------
def trackers(con, include_archived=False):
    ts = rows(con, "SELECT * FROM trackers" + ("" if include_archived else " WHERE archived=0") + " ORDER BY name")
    sums = {r["tracker_id"]: r for r in rows(
        con, "SELECT tracker_id, sum(amount_cents) total, count(*) n, max(date) last FROM tracker_entries GROUP BY tracker_id")}
    t = today()
    for tr in ts:
        s = sums.get(tr["id"], {"total": 0, "n": 0, "last": None})
        total = s["total"] or 0
        if tr["kind"] == "debt":  # start = balance owed; entries = payments
            remaining = max(0, tr["start_cents"] - total)
            progress = (total / tr["start_cents"]) if tr["start_cents"] else 0
            current = remaining
        else:
            current = tr["start_cents"] + total
            remaining = max(0, tr["target_cents"] - current)
            progress = (current / tr["target_cents"]) if tr["target_cents"] else 0
        weekly_needed = None
        if tr["target_date"] and remaining:
            weeks = max(1, (date.fromisoformat(tr["target_date"]) - t).days / 7)
            weekly_needed = round(remaining / weeks)
        tr.update(current_cents=current, remaining_cents=remaining, progress=round(min(progress, 1), 4),
                  entries=s["n"], last_entry=s["last"], weekly_needed_cents=weekly_needed)
    return ts


# ---------- investments ----------
def investments(con):
    accts = rows(con, "SELECT * FROM inv_accounts ORDER BY name")
    hold = rows(con, "SELECT * FROM holdings ORDER BY symbol")
    by = defaultdict(list)
    for h in hold:
        h["value_cents"] = round(h["quantity"] * h["price_cents"])
        h["gain_cents"] = h["value_cents"] - h["cost_basis_cents"]
        by[h["inv_account_id"]].append(h)
    tot_v = tot_c = 0
    for a in accts:
        a["holdings"] = by.get(a["id"], [])
        a["value_cents"] = sum(h["value_cents"] for h in a["holdings"]) + a["cash_cents"]
        a["cost_cents"] = sum(h["cost_basis_cents"] for h in a["holdings"]) + a["cash_cents"]
        a["gain_cents"] = a["value_cents"] - a["cost_cents"]
        tot_v += a["value_cents"]
        tot_c += a["cost_cents"]
    return {"accounts": accts, "value_cents": tot_v, "cost_cents": tot_c, "gain_cents": tot_v - tot_c}


def investment_history(con, days: int):
    since = (today() - timedelta(days=days)).isoformat()
    r = rows(con, "SELECT date, sum(value_cents) value_cents FROM inv_snapshots WHERE date >= ? GROUP BY date ORDER BY date",
             (since,))
    return r


def snapshot_investments(con):
    d = today().isoformat()
    for a in investments(con)["accounts"]:
        con.execute("INSERT INTO inv_snapshots(inv_account_id,date,value_cents) VALUES (?,?,?) "
                    "ON CONFLICT(inv_account_id,date) DO UPDATE SET value_cents=excluded.value_cents",
                    (a["id"], d, a["value_cents"]))


# ---------- overview ----------
LIABILITY = {"credit", "loan"}


def overview(con, lo: date, hi: date, group: str, perms: set):
    s = summarize(con, lo, hi)
    cats: dict = defaultdict(int)
    for b in s["bills"]:
        cats[b["category"]] += b["amount_cents"]
    for x in s["expenses"]:
        cats[x["category"] or "Uncategorized"] += x["amount_cents"]
    kinds: dict = defaultdict(int)
    kinds["pay"] = s["pay_cents"]
    for e in s["extras"]:
        kinds[e["kind"]] += e["amount_cents"]
    # time series
    series = []
    if group == "month":
        cur = date(lo.year, lo.month, 1)
        while cur <= hi:
            nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
            a, b = max(cur, lo), min(nxt - timedelta(days=1), hi)
            w = summarize(con, a, b, detail=False)
            series.append({"start": a.isoformat(), "label": cur.strftime("%b %Y"),
                           **{k: w[k] for k in ("in_cents", "out_cents", "net_cents")}})
            cur = nxt
    else:
        cur = week_start_of(lo)
        while cur <= hi:
            a, b = max(cur, lo), min(cur + timedelta(days=6), hi)
            w = summarize(con, a, b, detail=False)
            series.append({"start": a.isoformat(), "label": cur.isoformat(),
                           **{k: w[k] for k in ("in_cents", "out_cents", "net_cents")}})
            cur += timedelta(days=7)
    accts = rows(con, "SELECT id, name, kind, balance_cents, balance_updated FROM accounts ORDER BY name")
    assets = sum(a["balance_cents"] for a in accts if a["kind"] not in LIABILITY)
    debts = sum(a["balance_cents"] for a in accts if a["kind"] in LIABILITY)
    res = {k: s[k] for k in ("start", "end", "in_cents", "out_cents", "net_cents", "pay_cents", "extra_cents",
                             "ot_cents", "ot_hours", "bills_cents", "expenses_cents", "unpaid_cents", "by_account")}
    res.update(
        spending_by_category=sorted([{"category": k, "cents": v} for k, v in cats.items()], key=lambda x: -x["cents"]),
        income_by_kind=[{"kind": k, "cents": v} for k, v in kinds.items() if v],
        series=series, accounts=accts, assets_cents=assets, debts_cents=debts)
    nw = assets - debts
    if "investments:read" in perms:
        inv = investments(con)
        res["investments"] = {k: inv[k] for k in ("value_cents", "cost_cents", "gain_cents")}
        nw += inv["value_cents"]
    if "trackers:read" in perms:
        res["trackers"] = [{k: t[k] for k in ("id", "name", "kind", "current_cents", "target_cents", "progress",
                                              "remaining_cents", "weekly_needed_cents")} for t in trackers(con)]
    res["net_worth_cents"] = nw
    return res
