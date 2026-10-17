"""Email (SMTP over verified TLS), ntfy push, in-app inbox, and the background scheduler."""
import asyncio
import json
import logging
import queue
import threading
import smtplib
import ssl
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from urllib.parse import quote, urlparse

from . import ledger, settings
from .db import rows, tx
from .recurrence import occurrences
from .security import effective_perms, iso, now_utc

log = logging.getLogger("billtrack.notify")


def money(c: int) -> str:
    return f"-${abs(c)/100:,.2f}" if c < 0 else f"${c/100:,.2f}"


def _clean(s: str, n=200) -> str:
    return "".join(ch for ch in str(s) if ch.isprintable())[:n]


def email_ready() -> bool:
    return bool(settings.get("smtp_host") and settings.get("smtp_from"))


def valid_email(addr: str) -> bool:
    name, a = parseaddr(addr or "")
    return bool(a) and a == addr and "@" in a and len(a) <= 254 and "\n" not in a and " " not in a


def send_email(to: str, subject: str, body: str) -> bool:
    if not (email_ready() and valid_email(to)):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = _clean(subject), settings.get("smtp_from"), to
    msg.set_content(body)
    ctx = ssl.create_default_context()  # verifies cert + hostname
    host, port = settings.get("smtp_host"), settings.get("smtp_port")
    helo = settings.get("smtp_helo") or None   # name announced in EHLO; default = this machine's FQDN
    if settings.get("smtp_tls") == "ssl":
        s = smtplib.SMTP_SSL(host, port, local_hostname=helo, context=ctx, timeout=20)
    else:
        s = smtplib.SMTP(host, port, local_hostname=helo, timeout=20)
        s.starttls(context=ctx)  # raises if unsupported - mail is never sent unencrypted
    with s:
        # "none": the relay trusts this server's IP (e.g. Google Workspace SMTP relay) - no username/password
        if settings.get("smtp_auth") == "password" and settings.get("smtp_user"):
            s.login(settings.get("smtp_user"), settings.get("smtp_pass"))
        s.send_message(msg)
    return True


def send_push(topic: str, title: str, body: str, priority="default") -> bool:
    server = settings.get("ntfy_server")
    if not (server and topic):
        return False
    if urlparse(server).scheme not in ("https", "http"):
        return False
    req = urllib.request.Request(f"{server.rstrip('/')}/{quote(topic, safe='')}", data=body.encode()[:4000],
                                 method="POST")
    req.add_header("Title", _clean(title).encode("ascii", "replace").decode())
    req.add_header("Priority", priority)
    if settings.get("ntfy_token"):
        req.add_header("Authorization", f"Bearer {settings.get('ntfy_token')}")
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 (scheme checked)
        return 200 <= r.status < 300


def prefs(user) -> dict:
    p = {"email_alerts": True, "push_alerts": False, "ntfy_topic": "", "digest": True,
         "bill_reminders": True, "due_alerts": True, "overdue_alerts": True, "tracker_alerts": True,
         "balance_alerts": True, "timezone": "", "tz_auto": True}
    try:
        p.update(json.loads(user["prefs"] or "{}"))
    except ValueError:
        pass
    return p


def inbox(con, user_id: int, title: str, body: str = "", level="info"):
    con.execute("INSERT INTO notifications(user_id,ts,level,title,body) VALUES (?,?,?,?,?)",
                (user_id, iso(now_utc()), level, _clean(title), _clean(body, 4000)))


# Email/push go through an outbox worker so no request or DB transaction ever waits on a mail or push server.
_outbox: "queue.Queue[tuple]" = queue.Queue(maxsize=2000)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def _send_worker():
    while True:
        kind, args = _outbox.get()
        try:
            (send_email if kind == "email" else send_push)(*args)
        except Exception as e:
            log.warning("%s send failed: %s", kind, type(e).__name__)
        finally:
            _outbox.task_done()


def _enqueue(kind: str, *args) -> str:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_send_worker, name="bt-outbox", daemon=True)
            _worker.start()
    try:
        _outbox.put_nowait((kind, args))
        return "queued"
    except queue.Full:
        log.warning("outbox full, dropping %s", kind)
        return "dropped (outbox full)"


def deliver(con, user, title: str, body: str, level="info", email=True, push=True, wait=False) -> dict:
    """In-app always; email/push per user prefs. Queued unless wait=True (used only by 'send test'). Never raises."""
    if con is not None:
        inbox(con, user["id"], title, body, level)
    pr, res = prefs(user), {"inbox": True}
    jobs = []
    if email and pr["email_alerts"] and user["email"]:
        jobs.append(("email", (user["email"], title, body)))
    if push and pr["push_alerts"] and pr["ntfy_topic"]:
        jobs.append(("push", (pr["ntfy_topic"], title, body, "high" if level == "warn" else "default")))
    for kind, args in jobs:
        if not wait:
            res[kind] = _enqueue(kind, *args)
            continue
        try:
            res[kind] = (send_email if kind == "email" else send_push)(*args)
        except Exception as e:
            log.warning("%s to user %s failed: %s", kind, user["id"], type(e).__name__)
            res[kind] = f"error: {type(e).__name__}"
    return res


def notify_perm(con, perm: str, title: str, body: str, level="info", pref: str | None = None):
    for u in rows(con, "SELECT * FROM users WHERE disabled=0"):
        if perm in effective_perms(u) and (pref is None or prefs(u).get(pref, True)):
            deliver(con, u, title, body, level)


def digest(con, any_day):
    w = ledger.week(con, any_day)
    out = [f"Week {w['start']} to {w['end']}",
           f"Money in:  {money(w['in_cents'])}  (pay {money(w['pay_cents'])}, OT/extra {money(w['extra_cents'])})",
           f"Money out: {money(w['out_cents'])}  (bills {money(w['bills_cents'])}, other {money(w['expenses_cents'])})",
           f"Left over: {money(w['net_cents'])}", "", "Bills this week:"]
    out += [f"  {b['date']}  {b['name'][:24]:<24} {money(b['amount_cents']):>11}  {b['account'] or '-'}  [{b['status']}]"
            for b in w["bills"] if not b["skipped"]] or ["  none"]
    return f"Weekly money: left over {money(w['net_cents'])}", "\n".join(out)


def _already(con, uid, ref, kind) -> bool:
    return bool(con.execute("SELECT 1 FROM alert_log WHERE user_id=? AND ref=? AND kind=?", (uid, ref, kind)).fetchone())


def _mark(con, uid, ref, kind):
    con.execute("INSERT OR IGNORE INTO alert_log VALUES (?,?,?,?)", (uid, ref, kind, iso(now_utc())))


def _user_clock(u):
    """(now, today) in the user's own time zone (system default if they haven't got one)."""
    z = settings.user_tz_name(prefs(u)) or settings.get("timezone")
    tok = settings.use_tz(z)
    try:
        now = datetime.now(settings.tz())
    finally:
        settings.reset_tz(tok)
    return now, now.date(), z


def check_once() -> None:
    with tx() as con:
        users = [u for u in rows(con, "SELECT * FROM users WHERE disabled=0")]
        sys_today = datetime.now(settings.system_tz()).date()
        # wide enough to cover every user's "today" (zones differ by up to ~1 day)
        occ = ledger.bill_occurrences(con, sys_today - timedelta(days=2), sys_today + timedelta(days=32))
        reminders = rows(con, "SELECT * FROM reminders WHERE active=1")

        for u in users:
            now, t, zone = _user_clock(u)
            tok = settings.use_tz(zone)          # anything computed for this user uses their zone
            try:
                # Bill alerts, batched into one message
                if "finance:read" in effective_perms(u):
                    pr, lines, refs, urgent = prefs(u), [], [], False
                    for o in occ:
                        if o["status"] in ("paid", "skipped"):
                            continue
                        days = (datetime.fromisoformat(o["date"]).date() - t).days
                        kind = None
                        if days == -1 and not o["autopay"] and pr["overdue_alerts"]:
                            kind = "overdue"
                        elif days == 0 and not o["autopay"] and pr["due_alerts"]:
                            kind = "due"
                        elif 0 < days <= o["remind_days"] and pr["bill_reminders"]:
                            kind = "reminder"
                        ref = f"bill:{o['bill_id']}:{o['orig_date']}"
                        if not kind or _already(con, u["id"], ref, kind):
                            continue
                        urgent |= kind != "reminder"
                        tag = {"overdue": "OVERDUE", "due": "Due today", "reminder": f"Due in {days}d"}[kind]
                        lines.append(f"{tag}: {o['name']} {money(o['amount_cents'])} on {o['date']}"
                                     f" from {o['account'] or 'no account'}{' (autopay)' if o['autopay'] else ''}")
                        refs.append((ref, kind))
                    if lines:
                        deliver(con, u, f"Bills: {len(lines)} need attention", "\n".join(lines), "warn" if urgent else "info")
                        for ref, kind in refs:
                            _mark(con, u["id"], ref, kind)

                    # Weekly digest at the configured weekday/hour in the user's own zone
                    ws = ledger.week_start_of(t).isoformat()
                    if (now.weekday() == settings.get("digest_weekday") and now.hour >= settings.get("digest_hour")
                            and prefs(u)["digest"] and not _already(con, u["id"], f"digest:{ws}", "digest")):
                        subj, body = digest(con, t)
                        deliver(con, u, subj, body)
                        _mark(con, u["id"], f"digest:{ws}", "digest")

                # Custom reminders: their times are the user's local wall-clock times
                stamp = now.strftime("%Y-%m-%dT%H:%M")
                for r in [x for x in reminders if x["user_id"] == u["id"] and x["next_at"] <= stamp]:
                    deliver(con, u, f"Reminder: {r['title']}", r["note"] or r["title"], "info", bool(r["email"]), bool(r["push"]))
                    nxt = next_reminder(r["next_at"], r["recurrence"], stamp)
                    if nxt:
                        con.execute("UPDATE reminders SET next_at=? WHERE id=?", (nxt, r["id"]))
                    else:
                        con.execute("UPDATE reminders SET active=0 WHERE id=?", (r["id"],))
            finally:
                settings.reset_tz(tok)

        account_alerts(con)
        ledger.snapshot_investments(con)
        ledger.snapshot_accounts(con)
        ledger.snapshot_networth(con)

        # Housekeeping
        n = now_utc()
        con.execute("DELETE FROM notifications WHERE ts < ?", (iso(n - timedelta(days=180)),))
        con.execute("DELETE FROM audit_log WHERE ts < ?", (iso(n - timedelta(days=400)),))
        con.execute("DELETE FROM alert_log WHERE sent_at < ?", (iso(n - timedelta(days=120)),))
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(n),))
        con.execute("DELETE FROM reset_tokens WHERE expires_at < ?", (iso(n),))


def account_alerts(con) -> int:
    """Low balance, projected low balance this week, card utilization over threshold, card payment due soon.
    Each fires once; a condition that clears re-arms its alert. Returns alerts delivered."""
    v = ledger.accounts_view(con)
    conds = []   # (ref, kind, active, level, line)
    for a in v["banks"]:
        if a["low_balance_cents"]:
            conds.append((f"acct:{a['id']}:low", "low", a["low"], "warn",
                          f"Low balance: {a['name']} is {money(a['balance_cents'])} "
                          f"(alert below {money(a['low_balance_cents'])})"))
            conds.append((f"acct:{a['id']}:proj:{v['week_end']}", "projected", a["projected_low"] and not a["low"], "warn",
                          f"Heads up: {a['name']} will be about {money(a['projected_cents'])} by {v['week_end']} after "
                          f"scheduled bills (alert below {money(a['low_balance_cents'])})"))
    for c in v["cards"]:
        if c["utilization"] is not None and c["util_alert_pct"]:
            conds.append((f"card:{c['id']}:util", str(c["util_alert_pct"]), c["over_alert"], "warn",
                          f"{c['name']} is {round(c['utilization'] * 100)}% utilized "
                          f"({money(c['balance_cents'])} of {money(c['credit_limit_cents'])}; alert at {c['util_alert_pct']}%)"))
        if c["next_due"] and c["balance_cents"] > 0:
            conds.append((f"card:{c['id']}:due:{c['next_due']}", "due", 0 <= c["days_to_due"] <= 3, "warn",
                          f"{c['name']} payment due {c['next_due']}"
                          + (f", minimum {money(c['min_payment_cents'])}" if c["min_payment_cents"] else "")
                          + f", balance {money(c['balance_cents'])}"))
    for ref, kind, active, _, _ in conds:
        if not active:
            con.execute("DELETE FROM alert_log WHERE ref=? AND kind=?", (ref, kind))  # re-arm
    sent = 0
    for u in rows(con, "SELECT * FROM users WHERE disabled=0"):
        if "finance:read" not in effective_perms(u) or not prefs(u)["balance_alerts"]:
            continue
        due = [(ref, kind, line) for ref, kind, active, _, line in conds if active and not _already(con, u["id"], ref, kind)]
        if due:
            deliver(con, u, "Account alert" if len(due) == 1 else f"{len(due)} account alerts",
                    "\n".join(x[2] for x in due), "warn")
            for ref, kind, _ in due:
                _mark(con, u["id"], ref, kind)
            sent += 1
    return sent


def next_reminder(cur: str, rec: str, after: str) -> str | None:
    if rec == "once":
        return None
    d = datetime.fromisoformat(cur)
    hi = d.date() + timedelta(days=800)
    for occ in occurrences(d.date(), rec, d.date() + timedelta(days=1), hi):
        cand = datetime.combine(occ, d.time()).strftime("%Y-%m-%dT%H:%M")
        if cand > after:
            return cand
    return None


async def loop(interval_min: int):
    from . import db as _db, plaid
    while True:
        if _db.locked():                 # nothing to do until an admin unlocks
            await asyncio.sleep(30)
            continue
        try:
            await asyncio.to_thread(plaid.sync_all, True)   # before alerts, so alerts see fresh balances
        except Exception:
            log.exception("bank sync failed")
        try:
            await asyncio.to_thread(check_once)
        except Exception:
            log.exception("scheduler run failed")
        await asyncio.sleep(max(1, interval_min) * 60)
