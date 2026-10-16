"""BillTrack app: security middleware, routers, calendar feed, static PWA."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import secrets

from . import __version__, channel, config, crypto, db, ledger, notify, settings
from .routes import admin, auth, finance, home, me, plaid as plaid_routes, trackers
from .security import Principal, Throttle, client_ip, require

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("billtrack")
STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_BODY = 256 * 1024
api_limit = Throttle(config.RATE_LIMIT_PER_MIN, 60)


@asynccontextmanager
async def lifespan(_app):
    try:
        db.init()
        settings.reset_cache()
        settings.tz()
    except db.KeyError_ as e:
        log.critical("%s", e)
        raise SystemExit(1) from None
    import anyio.to_thread
    from . import watchdog
    watchdog.start(asyncio.get_running_loop(), anyio.to_thread.current_default_thread_limiter())
    task = asyncio.create_task(notify.loop(config.CHECK_INTERVAL_MIN))
    yield
    task.cancel()


api = FastAPI(title="BillTrack API", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; manifest-src 'self'; worker-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def origin_allowed(scheme: str, host: str, origin: str | None, fwd_host: str | None) -> bool:
    if not origin:
        return True
    ok = {f"{scheme}://{host}", *config.ALLOWED_ORIGINS}
    if settings.get("base_url"):
        ok.add(settings.get("base_url"))
    if fwd_host:
        ok.add(f"https://{fwd_host}")
    return origin.rstrip("/") in {o.rstrip("/") for o in ok}


@api.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith(("/api/", "/cal/")):
        key = client_ip(request)
        if api_limit.hit(key):
            return JSONResponse({"detail": "Rate limit exceeded. Slow down."}, 429, headers={"Retry-After": "60"})
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if "chunked" in request.headers.get("transfer-encoding", "").lower():
            return JSONResponse({"detail": "Chunked bodies not accepted"}, 411)
        cl = request.headers.get("content-length")
        if cl is not None and (not cl.isdigit() or int(cl) > MAX_BODY):
            return JSONResponse({"detail": "Request body too large"}, 413)
        # CSRF applies to cookie-authenticated requests. Bearer-token requests carry no ambient credentials.
        if not request.headers.get("authorization"):
            if request.headers.get("x-bt") != "1":
                return JSONResponse({"detail": "Missing CSRF header"}, 403)
            if not origin_allowed(request.url.scheme, request.headers.get("host", ""), request.headers.get("origin"),
                                  request.headers.get("x-forwarded-host")):
                return JSONResponse({"detail": "Origin not allowed"}, 403)
    resp = await call_next(request)
    h = resp.headers
    if "content-security-policy" not in h:
        h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "no-referrer"
    h["X-Frame-Options"] = "DENY"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    if "cross-origin-opener-policy" not in h:
        h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    if config.COOKIE_SECURE:
        h["Strict-Transport-Security"] = "max-age=31536000"
    if path.startswith("/api/"):
        h["Cache-Control"] = "no-store"
    return resp


@api.exception_handler(db.KeyError_)
async def key_error(_req, exc):
    log.error("database key problem: %s", exc)
    return JSONResponse({"detail": "Server storage error"}, 500)


for router in (auth.r, me.r, home.r, finance.r, trackers.r, admin.r, plaid_routes.r):
    api.include_router(router)


@api.get("/api/openapi.json", include_in_schema=False)
def openapi_spec(p: Principal = Depends(require("self"))):
    """API schema for signed-in users / token holders (not public)."""
    return api.openapi()


# ---------- calendar feed (subscribe from Google/Outlook/iPhone) ----------
def _ics_text(s: str) -> str:
    s = "".join(ch for ch in s if ch.isprintable())
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


@api.get("/cal/{token}.ics", include_in_schema=False)
def ics(token: str):
    if not config.ICS_TOKEN or len(token) > 200 or not crypto.eq(token, config.ICS_TOKEN):
        raise HTTPException(404)
    t = ledger.today()
    with db.tx() as con:
        bills = ledger.bill_occurrences(con, t - timedelta(days=60), t + timedelta(days=365))
        pays = ledger.pay_occurrences(con, t - timedelta(days=60), t + timedelta(days=365))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//BillTrack//EN", "X-WR-CALNAME:Bills", "CALSCALE:GREGORIAN"]
    for b in bills:
        d = b["date"].replace("-", "")
        mark = "PAID " if b["status"] == "paid" else ""
        out += ["BEGIN:VEVENT", f"UID:bill-{b['bill_id']}-{b['orig_date']}@billtrack", f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{d}",
                "SUMMARY:" + _ics_text(mark + b["name"] + " " + notify.money(b["amount_cents"])),
                "DESCRIPTION:" + _ics_text("Account: " + (b["account"] or "none"))]
        if b["status"] != "paid":
            out += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:" + _ics_text(b["name"] + " due"),
                    f"TRIGGER:-P{b['remind_days']}D", "END:VALARM"]
        out.append("END:VEVENT")
    for p in pays:
        out += ["BEGIN:VEVENT", f"UID:pay-{p['source_id']}-{p['orig_date']}@billtrack", f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{p['date'].replace('-', '')}",
                "SUMMARY:" + _ics_text("Payday " + p["name"] + " " + notify.money(p["amount_cents"])), "END:VEVENT"]
    out.append("END:VCALENDAR")
    return PlainTextResponse("\r\n".join(out) + "\r\n", media_type="text/calendar", headers={"Cache-Control": "no-store"})


@api.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


_INDEX = (STATIC / "index.html").read_text()


@api.get("/", include_in_schema=False)
@api.get("/index.html", include_in_schema=False)
def index():
    """App shell only (no data). A fresh random nonce + random-length filler on every load, so the page source
    is never byte-identical twice; the nonce is also bound into the CSP for the module script."""
    n = secrets.token_urlsafe(18)
    filler = secrets.token_urlsafe(secrets.randbelow(48) + 16)
    html = _INDEX.replace('<script type="module" src="/js/main.js"></script>',
                          f'<script type="module" nonce="{n}" src="/js/main.js"></script>')
    html = html.replace("<head>", f'<head>\n<meta name="bt-r" content="{filler}">', 1)
    csp = CSP.replace("script-src 'self'", f"script-src 'self' 'nonce-{n}'")
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Content-Security-Policy": csp})


_CONNECT = (STATIC / "connect.html").read_text()
# Plaid Link needs its script + iframe from cdn.plaid.com and an OAuth pop-up that can talk back to this window.
# Only this page gets that; the main app keeps the strict self-only policy.
CONNECT_CSP = ("default-src 'self'; script-src 'self' 'nonce-{n}' https://cdn.plaid.com; style-src 'self'; "
               "img-src 'self' data: https://*.plaid.com; connect-src 'self' https://*.plaid.com; "
               "frame-src https://cdn.plaid.com https://*.plaid.com; object-src 'none'; base-uri 'none'; "
               "form-action 'self'; frame-ancestors 'none'")


@api.get("/connect.html", include_in_schema=False)
def connect_page():
    n = secrets.token_urlsafe(18)
    html = _CONNECT.replace('<script type="module" src="/js/connect.js"></script>',
                            f'<script type="module" nonce="{n}" src="/js/connect.js"></script>')
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Content-Security-Policy": CONNECT_CSP.format(n=n),
                                       "Cross-Origin-Opener-Policy": "same-origin-allow-popups"})


api.mount("/", StaticFiles(directory=STATIC, html=True), name="static")


def _origin_ok(scope, hdrs) -> bool:
    return origin_allowed(scope.get("scheme", "http"), hdrs.get("host", ""), hdrs.get("origin"), hdrs.get("x-forwarded-host"))


app = channel.ChannelMiddleware(api, _origin_ok)
