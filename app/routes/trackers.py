"""Savings / debt / custom trackers and investments."""
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from .. import db, ledger, notify
from ..scope import owner
from ..security import Principal, audit, iso, now_utc, require
from .common import Cents, Color, In, Name, Note, SCents, Short, crud

r = APIRouter(prefix="/api", tags=["trackers", "investments"])


class TrackerIn(In):
    name: Name
    kind: Literal["savings", "debt", "custom"] = "savings"
    target_cents: Cents = 0
    start_cents: Cents = 0
    target_date: date | None = None
    account_id: int | None = None
    color: Color = "#1f7a4d"
    notes: Note = ""
    archived: bool = False


class EntryIn(In):
    tracker_id: int
    date: date
    amount_cents: SCents
    note: Note = ""


def _milestone(con, row):
    """In-app/email note when a tracker crosses 25/50/75/100%."""
    t = next((x for x in ledger.trackers(con, True) if x["id"] == row["tracker_id"]), None)
    if not t:
        return
    base = t["start_cents"] if t["kind"] == "debt" else t["target_cents"]
    if not base:
        return
    prev = t["progress"] - row["amount_cents"] / base
    for m in (1.0, 0.75, 0.5, 0.25):
        if prev < m <= t["progress"]:
            u = con.execute("SELECT * FROM users WHERE id=?", (owner(),)).fetchone()
            if u and notify.prefs(u).get("tracker_alerts", True):
                notify.deliver(con, u, f"{t['name']}: {int(m * 100)}% reached",
                               f"{t['name']} is at {notify.money(t['current_cents'])} ({round(t['progress'] * 100)}%).")
            break


crud(r, "trackers/items", "trackers", TrackerIn, "trackers:read", "trackers:write")
crud(r, "trackers/entries", "tracker_entries", EntryIn, "trackers:read", "trackers:write",
     order="date DESC", date_col="date", after_save=_milestone)


@r.get("/trackers")
def trackers(archived: bool = False, p: Principal = Depends(require("trackers:read"))):
    with db.tx() as con:
        return ledger.trackers(con, archived)


@r.get("/trackers/{tid}/entries")
def tracker_entries(tid: int, p: Principal = Depends(require("trackers:read"))):
    with db.tx() as con:
        return db.rows(con, "SELECT * FROM tracker_entries WHERE tracker_id=? AND owner_id=? ORDER BY date DESC LIMIT 1000",
                       (tid, owner()))


# ---------- investments ----------
class InvAccountIn(In):
    name: Name
    kind: Literal["brokerage", "retirement", "crypto", "hsa", "other"] = "brokerage"
    institution: Short = ""
    cash_cents: Cents = 0
    notes: Note = ""


class HoldingIn(In):
    inv_account_id: int
    symbol: Annotated[str, Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-:/^ ]+$")]
    name: Short = ""
    quantity: Annotated[float, Field(ge=0, le=1e12)] = 0
    cost_basis_cents: Cents = 0
    price_cents: Cents = 0


def _price_stamp(con, row, item_id):
    row["symbol"] = row["symbol"].upper().strip()
    row["price_updated"] = iso(now_utc())


crud(r, "investments/accounts", "inv_accounts", InvAccountIn, "investments:read", "investments:write",
     after_save=lambda con, row: ledger.snapshot_networth(con))
crud(r, "investments/holdings", "holdings", HoldingIn, "investments:read", "investments:write",
     order="symbol", before_save=_price_stamp, after_save=lambda con, row: ledger.snapshot_networth(con))


@r.get("/investments")
def investments(p: Principal = Depends(require("investments:read"))):
    with db.tx() as con:
        return ledger.investments(con)


@r.get("/investments/history")
def investment_history(days: Annotated[int, Query(ge=7, le=3650)] = 365,
                       p: Principal = Depends(require("investments:read"))):
    with db.tx() as con:
        return ledger.investment_history(con, days)


class PriceIn(In):
    symbol: Annotated[str, Field(min_length=1, max_length=20)]
    price_cents: Cents


class PricesIn(In):
    prices: Annotated[list[PriceIn], Field(max_length=500)]


@r.post("/investments/prices")
def update_prices(body: PricesIn, request: Request, p: Principal = Depends(require("investments:write"))):
    """Bulk price update by symbol - handy for a cron job using an API token."""
    n = 0
    with db.tx() as con:
        for x in body.prices:
            n += con.execute("UPDATE holdings SET price_cents=?, price_updated=? WHERE upper(symbol)=? AND owner_id=?",
                             (x.price_cents, iso(now_utc()), x.symbol.upper().strip(), owner())).rowcount
        ledger.snapshot_investments(con)
        ledger.snapshot_networth(con)
        audit(con, request, "update prices", f"{len(body.prices)} symbols", f"{n} holdings")
    return {"updated_holdings": n}
