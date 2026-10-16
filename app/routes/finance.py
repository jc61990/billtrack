"""Accounts, bills, pay, extras, expenses, paid/received marks, drag overrides, week/summary views."""
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from .. import db, ledger
from ..security import Principal, audit, iso, now_utc, require
from .common import Cents, Color, In, Name, Note, Rec, SCents, Short, crud

r = APIRouter(prefix="/api", tags=["finance"])
R, W = "finance:read", "finance:write"


class AccountIn(In):
    name: Name
    color: Color = "#3b6ea5"
    kind: Literal["checking", "savings", "cash", "credit", "loan", "other"] = "checking"
    balance_cents: SCents = 0


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
    row["balance_updated"] = iso(now_utc())


def _ot_amount(con, row, item_id):
    if row.get("amount_cents") is None:
        if row.get("kind") == "overtime" and row.get("hours") and row.get("source_id"):
            s = con.execute("SELECT hourly_rate_cents, ot_multiplier FROM income_sources WHERE id=?",
                            (row["source_id"],)).fetchone()
            if s and s["hourly_rate_cents"]:
                row["amount_cents"] = round(row["hours"] * s["hourly_rate_cents"] * s["ot_multiplier"])
        if row.get("amount_cents") is None:
            raise HTTPException(422, "Enter an amount, or hours with a job that has an hourly rate")


crud(r, "accounts", "accounts", AccountIn, R, W, before_save=_acct_stamp)
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
        b = con.execute("SELECT amount_cents FROM bills WHERE id=?", (body.bill_id,)).fetchone()
        if not b:
            raise HTTPException(404, "Bill not found")
        amt = b["amount_cents"] if body.amount_cents is None else body.amount_cents
        con.execute("""INSERT INTO payments(bill_id,due_date,paid_on,amount_cents) VALUES (?,?,?,?)
                       ON CONFLICT(bill_id,due_date) DO UPDATE SET paid_on=excluded.paid_on,
                       amount_cents=excluded.amount_cents""",
                    (body.bill_id, body.due_date.isoformat(), (body.paid_on or ledger.today()).isoformat(), amt))
        audit(con, request, "mark paid", f"bill {body.bill_id} {body.due_date}")
    return {"ok": True}


@r.delete("/payments/{pid}")
def unmark_paid(pid: int, request: Request, p: Principal = Depends(require(W))):
    with db.tx() as con:
        con.execute("DELETE FROM payments WHERE id=?", (pid,))
        audit(con, request, "mark unpaid", f"payment {pid}")
    return {"ok": True}


class ReceivedIn(In):
    source_id: int
    pay_date: date
    amount_cents: Cents


@r.post("/received", status_code=201)
def mark_received(body: ReceivedIn, request: Request, p: Principal = Depends(require(W))):
    with db.tx() as con:
        if not con.execute("SELECT 1 FROM income_sources WHERE id=?", (body.source_id,)).fetchone():
            raise HTTPException(404, "Pay not found")
        con.execute("""INSERT INTO income_received(source_id,pay_date,amount_cents) VALUES (?,?,?)
                       ON CONFLICT(source_id,pay_date) DO UPDATE SET amount_cents=excluded.amount_cents""",
                    (body.source_id, body.pay_date.isoformat(), body.amount_cents))
        audit(con, request, "pay received", f"source {body.source_id} {body.pay_date}")
    return {"ok": True}


@r.delete("/received/{rid}")
def unmark_received(rid: int, p: Principal = Depends(require(W))):
    with db.tx() as con:
        con.execute("DELETE FROM income_received WHERE id=?", (rid,))
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
        it = con.execute(f"SELECT start_date, recurrence, end_date FROM {tbl} WHERE id=?", (body.item_id,)).fetchone()
        if not it:
            raise HTTPException(404, "Not found")
        from ..recurrence import occurrences
        if body.orig_date not in occurrences(date.fromisoformat(it["start_date"]), it["recurrence"],
                                             body.orig_date, body.orig_date,
                                             date.fromisoformat(it["end_date"]) if it["end_date"] else None):
            raise HTTPException(422, "That date isn't one of this item's scheduled dates")
        new = body.new_date if body.new_date and body.new_date != body.orig_date else None
        if not new and not body.skipped:
            con.execute("DELETE FROM occurrence_overrides WHERE kind=? AND item_id=? AND orig_date=?",
                        (body.kind, body.item_id, body.orig_date.isoformat()))
        else:
            con.execute("""INSERT INTO occurrence_overrides(kind,item_id,orig_date,new_date,skipped) VALUES (?,?,?,?,?)
                           ON CONFLICT(kind,item_id,orig_date) DO UPDATE SET new_date=excluded.new_date,
                           skipped=excluded.skipped""",
                        (body.kind, body.item_id, body.orig_date.isoformat(),
                         new.isoformat() if new else None, int(body.skipped)))
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
