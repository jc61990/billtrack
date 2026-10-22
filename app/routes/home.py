"""At-a-glance home page: one request, sections included only if the user may see them."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends

from .. import ledger, settings
from ..db import rows, tx
from ..recurrence import occurrences
from ..security import Principal, require

r = APIRouter(prefix="/api", tags=["home"])


@r.get("/home")
def home(p: Principal = Depends(require("finance:read"))):
    t = ledger.today()
    out: dict = {"today": t.isoformat(), "sections": []}
    with tx() as con:
        u = con.execute("SELECT display_name, username FROM users WHERE id=?", (p.user_id,)).fetchone()
        out["name"] = u["display_name"] or u["username"]
        out["unread"] = con.execute("SELECT count(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
                                    (p.user_id,)).fetchone()[0]
        out["alerts"] = rows(con, "SELECT id, ts, level, title FROM notifications WHERE user_id=? AND read_at IS NULL "
                                  "ORDER BY id DESC LIMIT 4", (p.user_id,))

        if p.can("finance:read"):
            w = ledger.week(con, t)
            out["week"] = {k: w[k] for k in ("start", "end", "in_cents", "out_cents", "net_cents", "pay_cents",
                                             "extra_cents", "ot_hours", "bills_cents", "expenses_cents", "unpaid_cents")}
            # next 14 days of bills + paydays, plus anything overdue in the last 30 days
            bills = ledger.bill_occurrences(con, t - timedelta(days=30), t + timedelta(days=14))
            out["overdue"] = [b for b in bills if b["status"] == "overdue"]
            out["upcoming"] = [b for b in bills if b["date"] >= t.isoformat() and b["status"] != "paid"][:8]
            out["upcoming_total_cents"] = sum(b["amount_cents"] for b in bills
                                              if b["date"] >= t.isoformat() and b["status"] != "paid")
            pays = [x for x in ledger.pay_occurrences(con, t, t + timedelta(days=14)) if not x["received"]]
            out["next_pay"] = pays[0] if pays else None
            out["bill_suggestions"] = con.execute("SELECT count(*) FROM bill_suggestions WHERE owner_id=? AND status='open'",
                                                  (p.user_id,)).fetchone()[0]
            a = ledger.accounts_view(con)
            out["accounts"] = {
                "cash_cents": a["cash_cents"], "projected_cash_cents": a["projected_cash_cents"], "week_end": a["week_end"],
                "card_balance_cents": a["card_balance_cents"], "card_limit_cents": a["card_limit_cents"],
                "card_utilization": a["card_utilization"],
                "low": [{"name": x["name"], "balance_cents": x["balance_cents"], "projected_cents": x["projected_cents"],
                         "low_balance_cents": x["low_balance_cents"], "now": x["low"]}
                        for x in a["banks"] if x["low"] or x["projected_low"]],
                "cards_due": [{"name": c["name"], "next_due": c["next_due"], "days_to_due": c["days_to_due"],
                               "min_payment_cents": c["min_payment_cents"], "balance_cents": c["balance_cents"]}
                              for c in a["cards"] if c["days_to_due"] is not None and c["days_to_due"] <= 7 and c["balance_cents"] > 0],
                "cards_over": [{"name": c["name"], "utilization": c["utilization"], "util_alert_pct": c["util_alert_pct"]}
                               for c in a["cards"] if c["over_alert"]],
                "has_accounts": bool(a["banks"] or a["cards"] or a["other"]),
            }
            ledger.snapshot_networth(con)
            nw = ledger.networth(con, p.perms)
            out["networth"] = {"net_cents": nw["net_cents"], "assets_cents": nw["assets_cents"],
                               "liabilities_cents": nw["liabilities_cents"], "change_30d": nw["changes"].get("30d"),
                               "history": ledger.networth_history(con, 90, p.perms)}
        if p.can("trackers:read"):
            out["trackers"] = [{k: x[k] for k in ("id", "name", "kind", "color", "current_cents", "target_cents",
                                                  "start_cents", "progress", "weekly_needed_cents")}
                               for x in ledger.trackers(con)][:4]
        if p.can("investments:read"):
            inv = ledger.investments(con)
            out["investments"] = {k: inv[k] for k in ("value_cents", "cost_cents", "gain_cents")}
        if p.can("reminders"):
            rem = []
            for x in rows(con, "SELECT * FROM reminders WHERE user_id=? AND active=1", (p.user_id,)):
                first = datetime.fromisoformat(x["next_at"])
                for d in occurrences(first.date(), x["recurrence"], t, t + timedelta(days=7)):
                    rem.append({"id": x["id"], "title": x["title"], "date": d.isoformat(), "time": x["next_at"][11:16]})
            out["reminders"] = sorted(rem, key=lambda z: (z["date"], z["time"]))[:5]
    out["week_start"] = settings.get("week_start")
    return out
