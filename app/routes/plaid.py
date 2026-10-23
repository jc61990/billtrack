"""Each user links their own banks (Plaid). Plaid API keys are a system setting (admins); links and data are private."""
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, StringConstraints

from .. import ledger, plaid
from ..db import rows, tx
from ..scope import owner
from ..security import Principal, audit, require
from .common import In

r = APIRouter(prefix="/api/plaid", tags=["bank sync"])
Admin = Depends(require("finance:write", session_only=True))   # the signed-in user, never via API token


def _err(e: plaid.PlaidError):
    raise HTTPException(502 if e.status >= 500 or e.code == "NETWORK" else 400,
                        f"Plaid: {e.message or e.code}") from None


@r.get("/status")
def status(p: Principal = Admin):
    with tx() as con:
        items = rows(con, "SELECT id, institution_name, institution_id, status, error, last_sync, created_at, import_since, positions_status "
                          "FROM plaid_items WHERE owner_id=? ORDER BY institution_name", (owner(),))
        accts = rows(con, "SELECT id, item_id, name, official_name, mask, type, subtype, map_kind, map_id, "
                          "current_cents, available_cents, limit_cents, updated_at FROM plaid_accounts WHERE owner_id=? ORDER BY name", (owner(),))
        counts = {r_["plaid_account_id"]: r_ for r_ in rows(
            con, "SELECT plaid_account_id, count(*) n, sum(outcome='expense') expenses, sum(outcome LIKE 'bill:%') bills, "
                 "sum(outcome LIKE 'pay:%') pays, max(date) last FROM plaid_txns WHERE owner_id=? GROUP BY plaid_account_id", (owner(),))}
    for a in accts:
        a["stats"] = counts.get(a["id"], {})
    for it in items:
        it["accounts"] = [a for a in accts if a["item_id"] == it["id"]]
    from .. import settings
    return {"configured": plaid.configured(), "env": settings.get("plaid_env"), "items": items}


class LinkIn(In):
    item: int | None = None     # set = reconnect (update mode)
    add_investments: bool = False   # with item: also ask the bank to share positions


@r.post("/link-token")
def link_token(body: LinkIn, p: Principal = Admin):
    item = None
    if body.item:
        with tx() as con:
            item = con.execute("SELECT * FROM plaid_items WHERE id=? AND owner_id=?", (body.item, owner())).fetchone()
        if not item:
            raise HTTPException(404, "Not found")
    try:
        return {"link_token": plaid.link_token(p.user_id, item, body.add_investments)}
    except plaid.PlaidError as e:
        _err(e)


class ExchangeIn(In):
    public_token: Annotated[str, StringConstraints(min_length=10, max_length=200, pattern=r"^[A-Za-z0-9\-]+$")]
    institution_id: Annotated[str, StringConstraints(max_length=64)] = ""
    institution_name: Annotated[str, StringConstraints(max_length=100)] = "Bank"
    import_since: date | None = None


@r.post("/exchange")
def exchange(body: ExchangeIn, request: Request, p: Principal = Admin):
    since = body.import_since or ledger.today().replace(day=1)
    if since < ledger.today() - timedelta(days=180):
        raise HTTPException(422, "Import can go back at most 180 days")
    try:
        local = plaid.exchange(body.public_token, body.institution_id, body.institution_name, p.user_id, since.isoformat())
    except plaid.PlaidError as e:
        _err(e)
    with tx() as con:
        audit(con, request, "bank linked", body.institution_name)
    return {"item": local}


class MapIn(In):
    map_kind: Literal["none", "account", "investment"]
    map_id: int | None = None
    create: bool = False         # make a matching BillTrack account first


KIND = {"checking": "checking", "savings": "savings", "money market": "savings", "cd": "savings", "prepaid": "cash",
        "cash management": "checking", "credit card": "credit"}


@r.put("/accounts/{aid}")
def map_account(aid: int, body: MapIn, request: Request, p: Principal = Admin):
    with tx() as con:
        pa = con.execute("SELECT * FROM plaid_accounts WHERE id=? AND owner_id=?", (aid, owner())).fetchone()
        if not pa:
            raise HTTPException(404, "Not found")
        map_id = body.map_id
        label = f"{pa['name']}"[:100]
        if body.map_kind == "account":
            if body.create:
                kind = KIND.get(pa["subtype"], "credit" if pa["type"] == "credit" else "loan" if pa["type"] == "loan" else "checking")
                map_id = con.execute("INSERT INTO accounts(name,kind,last4,color,owner_id) VALUES (?,?,?,?,?)",
                                     (label, kind, pa["mask"] if pa["mask"].isdigit() and len(pa["mask"]) == 4 else "",
                                      "#a3352b" if kind == "credit" else "#3b6ea5", owner())).lastrowid
            elif not map_id or not con.execute("SELECT 1 FROM accounts WHERE id=? AND owner_id=?", (map_id, owner())).fetchone():
                raise HTTPException(422, "Pick a BillTrack account")
        elif body.map_kind == "investment":
            if body.create:
                map_id = con.execute("INSERT INTO inv_accounts(name,kind,owner_id) VALUES (?,?,?)",
                                     (label, "retirement" if pa["subtype"] in ("ira", "roth", "401k", "403b", "roth 401k") else "brokerage",
                                      owner())).lastrowid
            elif not map_id or not con.execute("SELECT 1 FROM inv_accounts WHERE id=? AND owner_id=?", (map_id, owner())).fetchone():
                raise HTTPException(422, "Pick an investment account")
        else:
            map_id = None
        if pa["map_kind"] == "investment" and (body.map_kind != "investment" or map_id != pa["map_id"]):
            con.execute("UPDATE inv_accounts SET synced_value_cents=NULL WHERE id=? AND owner_id=?", (pa["map_id"], owner()))
        if map_id and con.execute("SELECT 1 FROM plaid_accounts WHERE map_kind=? AND map_id=? AND id<>? AND owner_id=?",
                                  (body.map_kind, map_id, aid, owner())).fetchone():
            raise HTTPException(409, "That BillTrack account is already linked to another bank account")
        con.execute("UPDATE plaid_accounts SET map_kind=?, map_id=? WHERE id=? AND owner_id=?", (body.map_kind, map_id, aid, owner()))
        plaid._apply_balances(con, pa["item_id"])
        audit(con, request, "bank account mapped", f"{pa['name']} ••{pa['mask']}", f"{body.map_kind} {map_id}")
    return {"ok": True, "map_id": map_id}


class SinceIn(In):
    import_since: date


@r.put("/items/{iid}")
def set_since(iid: int, body: SinceIn, p: Principal = Admin):
    if body.import_since < ledger.today() - timedelta(days=180):
        raise HTTPException(422, "Import can go back at most 180 days")
    with tx() as con:
        con.execute("UPDATE plaid_items SET import_since=? WHERE id=? AND owner_id=?", (body.import_since.isoformat(), iid, owner()))
    return {"ok": True}


@r.post("/sync")
def sync_now(request: Request, item: int | None = None, p: Principal = Admin):
    if item:
        with tx() as con:
            if not con.execute("SELECT 1 FROM plaid_items WHERE id=? AND owner_id=?", (item, owner())).fetchone():
                raise HTTPException(404, "Not found")
        res = {item: plaid.sync_item(item)}
    else:
        res = plaid.sync_all(False, only_owner=owner())
    with tx() as con:
        audit(con, request, "bank sync", str(item or "all"), str(res)[:400])
    return res


@r.delete("/items/{iid}")
def unlink(iid: int, request: Request, p: Principal = Admin):
    with tx() as con:
        it = con.execute("SELECT institution_name FROM plaid_items WHERE id=? AND owner_id=?", (iid, owner())).fetchone()
    if not it:
        raise HTTPException(404, "Not found")
    plaid.remove(iid)
    with tx() as con:
        audit(con, request, "bank unlinked", it["institution_name"])
    return {"ok": True}
