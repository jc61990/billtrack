"""Accounts, bills, pay, extras, expenses, paid/received marks, drag overrides, week/summary views."""
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field, model_validator

from .. import db, ledger, notify
from ..scope import owner
from ..security import Principal, audit, iso, now_utc, require
from .common import Cents, Color, In, Name, Note, Rec, SCents, Short, crud

r = APIRouter(prefix="/api", tags=["finance"])
R, W = "finance:read", "finance:write"


class AccountIn(In):
    name: Name
    color: Color = "#3b6ea5"
    kind: Literal["checking", "savings", "cash", "credit", "loan", "other"] = "checking"
    balance_cents: SCents = 0            # cards/loans: amount owed
    institution: Short = ""
    # Last four digits only. Full card/account numbers are never accepted or stored.
    last4: Annotated[str, Field(pattern=r"^(\d{4})?$")] = ""
    credit_limit_cents: Cents = 0
    low_balance_cents: Cents = 0          # bank accounts: alert below this (0 = off)
    util_alert_pct: Annotated[int, Field(ge=0, le=100)] = 30   # cards: alert at/above this % (0 = off)
    apr_bps: Annotated[int, Field(ge=0, le=10000)] = 0          # 24.99% = 2499
    due_day: Annotated[int, Field(ge=1, le=31)] | None = None
    min_payment_cents: Cents = 0
    notes: Note = ""


class BillIn(In):
    name: Name
    amount_cents: Cents
    account_id: int | None = None
    start_date: date
    recurrence: Rec = "monthly"
    end_date: date | None = None
    autopay: bool = False
    remind_days: Annotated[int, Field(ge=0, le=30)] = 3
    category: Short = ""
    notes: Note = ""
    active: bool = True
    amount_mode: Literal["fixed", "varies"] = "fixed"   # varies: expected amount follows bank charges
    match_name: Short = ""                               # words to find in bank payees ("" = use the bill name)

    @model_validator(mode="after")
    def _ends(self):
        return _end_after_start(self)


def _end_after_start(m):
    if m.end_date and m.end_date < m.start_date:
        raise ValueError("'Stop after' date is before the first due date. Leave it blank for bills that keep going.")
    return m


class SourceIn(In):
    name: Name
    amount_cents: Cents
    account_id: int | None = None
    start_date: date
    recurrence: Rec = "weekly"
    end_date: date | None = None
    hourly_rate_cents: Cents = 0
    ot_multiplier: Annotated[float, Field(ge=1, le=4)] = 1.5
    active: bool = True

    @model_validator(mode="after")
    def _ends(self):
        return _end_after_start(self)


class ExtraIn(In):
    date: date
    kind: Literal["overtime", "bonus", "side", "refund", "other"] = "overtime"
    source_id: int | None = None
    account_id: int | None = None
    hours: Annotated[float, Field(ge=0, le=200)] | None = None
    amount_cents: Cents | None = None
    note: Note = ""


class ExpenseIn(In):
    date: date
    name: Name
    amount_cents: Cents
    account_id: int | None = None
    category: Short = ""


def _acct_stamp(con, row, item_id):
    old = con.execute("SELECT balance_cents FROM accounts WHERE id=? AND owner_id=?", (item_id, owner())).fetchone() if item_id else None
    if not old or old["balance_cents"] != row["balance_cents"]:
        row["balance_updated"] = iso(now_utc())


def _acct_after(con, row):
    ledger.record_balance(con, row["id"], row["balance_cents"])
    ledger.snapshot_networth(con)
    notify.account_alerts(con)


def _ot_amount(con, row, item_id):
    if row.get("amount_cents") is None:
        if row.get("kind") == "overtime" and row.get("hours") and row.get("source_id"):
            s = con.execute("SELECT hourly_rate_cents, ot_multiplier FROM income_sources WHERE id=? AND owner_id=?",
                            (row["source_id"], owner())).fetchone()
            if s and s["hourly_rate_cents"]:
                row["amount_cents"] = round(row["hours"] * s["hourly_rate_cents"] * s["ot_multiplier"])
        if row.get("amount_cents") is None:
            raise HTTPException(422, "Enter an amount, or hours with a job that has an hourly rate")


crud(r, "accounts", "accounts", AccountIn, R, W, before_save=_acct_stamp, after_save=_acct_after)


class BalanceIn(In):
    balance_cents: SCents


@r.post("/accounts/{aid}/balance")
def update_balance(aid: int, body: BalanceIn, request: Request, p: Principal = Depends(require(W))):
    """Quick balance update (also what a script would call with an API token)."""
    with db.tx() as con:
        n = con.execute("UPDATE accounts SET balance_cents=?, balance_updated=? WHERE id=? AND owner_id=?",
                        (body.balance_cents, iso(now_utc()), aid, owner())).rowcount
        if not n:
            raise HTTPException(404, "Not found")
        ledger.record_balance(con, aid, body.balance_cents)
        ledger.snapshot_networth(con)
        audit(con, request, "balance updated", f"account {aid}")
        notify.account_alerts(con)
    return {"ok": True}


# ---------- net worth ----------
class NetWorthItemIn(In):
    name: Name
    side: Literal["asset", "liability"]
    category: Literal["property", "vehicle", "valuables", "retirement", "receivable", "mortgage", "auto", "student",
                      "personal", "medical", "tax", "other"] = "other"
    value_cents: Cents = 0
    notes: Note = ""
    active: bool = True


def _nw_stamp(con, row, item_id):
    asset_cats = set(ledger.ASSET_CATS)
    if (row["side"] == "asset") != (row["category"] in asset_cats) and row["category"] != "other":
        raise HTTPException(422, "Category doesn't match asset/liability")
    old = con.execute("SELECT value_cents FROM networth_items WHERE id=? AND owner_id=?", (item_id, owner())).fetchone() if item_id else None
    if not old or old["value_cents"] != row["value_cents"]:
        row["value_updated"] = iso(now_utc())


crud(r, "networth/items", "networth_items", NetWorthItemIn, R, W, order="side, value_cents DESC",
     before_save=_nw_stamp, after_save=lambda con, row: ledger.snapshot_networth(con))


@r.get("/networth")
def networth(p: Principal = Depends(require(R))):
    with db.tx() as con:
        ledger.snapshot_networth(con)   # keep today's point current (and start history on first look)
        return ledger.networth(con, p.perms)


@r.get("/networth/history")
def networth_history(days: Annotated[int, Query(ge=7, le=3650)] = 365, p: Principal = Depends(require(R))):
    with db.tx() as con:
        return ledger.networth_history(con, days, p.perms)


# ---------- bill autopilot suggestions ----------
@r.get("/bills/suggestions")
def bill_suggestions(p: Principal = Depends(require(R))):
    import json as _j
    with db.tx() as con:
        out = db.rows(con, "SELECT id, kind, bill_id, payload, created_at FROM bill_suggestions WHERE owner_id=? "
                           "AND status='open' ORDER BY id DESC LIMIT 50", (owner(),))
    for x in out:
        x["payload"] = _j.loads(x["payload"])
    return out


@r.post("/bills/suggestions/{sid}/accept")
def accept_suggestion(sid: int, request: Request, p: Principal = Depends(require(W))):
    from .. import autopilot
    with db.tx() as con:
        s = con.execute("SELECT * FROM bill_suggestions WHERE id=? AND owner_id=? AND status='open'", (sid, owner())).fetchone()
        if not s:
            raise HTTPException(404, "Not found")
        autopilot.apply(con, dict(s))
        audit(con, request, f"bill suggestion accepted ({s['kind']})", sid)
    return {"ok": True}


@r.post("/bills/suggestions/{sid}/dismiss")
def dismiss_suggestion(sid: int, p: Principal = Depends(require(W))):
    with db.tx() as con:
        con.execute("UPDATE bill_suggestions SET status='dismissed' WHERE id=? AND owner_id=?", (sid, owner()))
    return {"ok": True}


@r.post("/bills/suggestions/scan")
def scan_suggestions(p: Principal = Depends(require(W))):
    from .. import autopilot
    with db.tx() as con:
        return {"new": autopilot.scan_recurring(con)}


@r.get("/accounts/overview")
def accounts_overview(p: Principal = Depends(require(R))):
    with db.tx() as con:
        return ledger.accounts_view(con)
crud(r, "bills", "bills", BillIn, R, W)
crud(r, "income-sources", "income_sources", SourceIn, R, W)
crud(r, "income-extra", "income_extra", ExtraIn, R, W, order="date DESC", date_col="date", before_save=_ot_amount)
crud(r, "expenses", "expenses", ExpenseIn, R, W, order="date DESC", date_col="date")


class PayIn(In):
    bill_id: int
    due_date: date
    amount_cents: Cents | None = None
    paid_on: date | None = None


@r.post("/payments", status_code=201)
def mark_paid(body: PayIn, request: Request, p: Principal = Depends(require(W))):
    with db.tx() as con:
        b = con.execute("SELECT amount_cents FROM bills WHERE id=? AND owner_id=?", (body.bill_id, owner())).fetchone()
        if not b:
            raise HTTPException(404, "Bill not found")
        amt = b["amount_cents"] if body.amount_cents is None else body.amount_cents
        con.execute("""INSERT INTO payments(bill_id,due_date,paid_on,amount_cents,owner_id) VALUES (?,?,?,?,?)
                       ON CONFLICT(bill_id,due_date) DO UPDATE SET paid_on=excluded.paid_on,
                       amount_cents=excluded.amount_cents""",
                    (body.bill_id, body.due_date.isoformat(), (body.paid_on or ledger.today()).isoformat(), amt, owner()))
        audit(con, request, "mark paid", f"bill {body.bill_id} {body.due_date}")
    return {"ok": True}


@r.delete("/payments/{pid}")
def unmark_paid(pid: int, request: Request, p: Principal = Depends(require(W))):
    with db.tx() as con:
        con.execute("DELETE FROM payments WHERE id=? AND owner_id=?", (pid, owner()))
        audit(con, request, "mark unpaid", f"payment {pid}")
    return {"ok": True}


class ReceivedIn(In):
    source_id: int
    pay_date: date
    amount_cents: Cents


@r.post("/received", status_code=201)
def mark_received(body: ReceivedIn, request: Request, p: Principal = Depends(require(W))):
    with db.tx() as con:
        if not con.execute("SELECT 1 FROM income_sources WHERE id=? AND owner_id=?", (body.source_id, owner())).fetchone():
            raise HTTPException(404, "Pay not found")
        con.execute("""INSERT INTO income_received(source_id,pay_date,amount_cents,owner_id) VALUES (?,?,?,?)
                       ON CONFLICT(source_id,pay_date) DO UPDATE SET amount_cents=excluded.amount_cents""",
                    (body.source_id, body.pay_date.isoformat(), body.amount_cents, owner()))
        audit(con, request, "pay received", f"source {body.source_id} {body.pay_date}")
    return {"ok": True}


@r.delete("/received/{rid}")
def unmark_received(rid: int, p: Principal = Depends(require(W))):
    with db.tx() as con:
        con.execute("DELETE FROM income_received WHERE id=? AND owner_id=?", (rid, owner()))
    return {"ok": True}


class OverrideIn(In):
    kind: Literal["bill", "pay"]
    item_id: int
    orig_date: date
    new_date: date | None = None
    skipped: bool = False


@r.put("/overrides")
def set_override(body: OverrideIn, request: Request, p: Principal = Depends(require(W))):
    """Move or skip one occurrence (drag-and-drop on the calendar)."""
    if body.new_date and abs((body.new_date - body.orig_date).days) > 60:
        raise HTTPException(422, "An occurrence can move at most 60 days")
    tbl = "bills" if body.kind == "bill" else "income_sources"
    with db.tx() as con:
        it = con.execute(f"SELECT start_date, recurrence, end_date FROM {tbl} WHERE id=? AND owner_id=?",
                         (body.item_id, owner())).fetchone()
        if not it:
            raise HTTPException(404, "Not found")
        from ..recurrence import occurrences
        if body.orig_date not in occurrences(date.fromisoformat(it["start_date"]), it["recurrence"],
                                             body.orig_date, body.orig_date,
                                             date.fromisoformat(it["end_date"]) if it["end_date"] else None):
            raise HTTPException(422, "That date isn't one of this item's scheduled dates")
        new = body.new_date if body.new_date and body.new_date != body.orig_date else None
        if not new and not body.skipped:
            con.execute("DELETE FROM occurrence_overrides WHERE kind=? AND item_id=? AND orig_date=? AND owner_id=?",
                        (body.kind, body.item_id, body.orig_date.isoformat(), owner()))
        else:
            con.execute("""INSERT INTO occurrence_overrides(kind,item_id,orig_date,new_date,skipped,owner_id) VALUES (?,?,?,?,?,?)
                           ON CONFLICT(kind,item_id,orig_date) DO UPDATE SET new_date=excluded.new_date,
                           skipped=excluded.skipped""",
                        (body.kind, body.item_id, body.orig_date.isoformat(),
                         new.isoformat() if new else None, int(body.skipped), owner()))
        audit(con, request, "reschedule", f"{body.kind} {body.item_id} {body.orig_date}",
              f"to {new} skipped={body.skipped}")
    return {"ok": True}


def _range(start: date, end: date, maxdays=400):
    if end < start or (end - start).days > maxdays:
        raise HTTPException(422, f"Range must be 0-{maxdays} days")


@r.get("/week")
def week(day: date | None = None, p: Principal = Depends(require(R))):
    with db.tx() as con:
        return ledger.week(con, day or ledger.today())


@r.get("/summary")
def summary(start: date, end: date, skipped: bool = False, p: Principal = Depends(require(R))):
    _range(start, end)
    with db.tx() as con:
        return ledger.summarize(con, start, end, include_skipped=skipped)


@r.get("/trend")
def trend(back: Annotated[int, Query(ge=0, le=52)] = 8, ahead: Annotated[int, Query(ge=0, le=26)] = 4,
          p: Principal = Depends(require(R))):
    with db.tx() as con:
        return ledger.trend(con, back, ahead)


@r.get("/reports/overview")
def overview(start: date, end: date, group: Literal["week", "month"] = "week",
             p: Principal = Depends(require(R))):
    _range(start, end, 800)
    with db.tx() as con:
        return ledger.overview(con, start, end, group, p.perms)
