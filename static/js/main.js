import { $, api, can, el, refresh, S, setRefresh, setUnauthorizedHandler, showDialog, toast, closeMenu , fill } from "./core.js";
import { showAuth } from "./auth.js";
import * as home from "./views/home.js";
import * as week from "./views/week.js";
import * as cal from "./views/calendar.js";
import * as overview from "./views/overview.js";
import * as accounts from "./views/accounts.js";
import * as networth from "./views/networth.js";
import * as bills from "./views/bills.js";
import * as income from "./views/income.js";
import * as trackers from "./views/trackers.js";
import * as investments from "./views/investments.js";
import * as alerts from "./views/alerts.js";
import * as settings from "./views/settings.js";
import * as admin from "./views/admin.js";
import * as banks from "./views/banks.js";

// [key, module, nav label, required permission, on mobile bottom bar]
const VIEWS = [
  ["home", home, "Home", "finance:read", true],
  ["week", week, "Week", "finance:read", true], ["cal", cal, "Calendar", "finance:read", true],
  ["accounts", accounts, "Accounts", "finance:read", true], ["networth", networth, "Net worth", "finance:read"],
  ["overview", overview, "Overview", "finance:read"],
  ["bills", bills, "Bills", "finance:read"],
  ["income", income, "Income", "finance:read"], ["trackers", trackers, "Trackers", "trackers:read"],
  ["investments", investments, "Investments", "investments:read"], ["banks", banks, "Bank links", "finance:write"],
  ["admin", admin, "Admin", "admin", true],
  ["alerts", alerts, "Alerts", "self", true], ["settings", settings, "Settings", "self", true],
];
const allowed = () => VIEWS.filter((v) => can(v[3]) && (!S.me.mfa_setup_required || v[0] === "settings"));

function nav() {
  const vs = allowed();
  const btn = (v) => el("button", { type: "button", "aria-current": S.view === v[0] ? "page" : null, onclick: () => go(v[0]) },
    v[2], v[0] === "alerts" ? el("span", { class: "badge", hidden: !S.me.unread }, S.me.unread) : null);
  $("#sidenav").replaceChildren(...vs.map(btn),
    el("button", { type: "button", class: "signout", onclick: signOut }, "⎋ Sign out"));
  const bottom = vs.filter((v) => v[4]).slice(0, 4);
  const rest = vs.filter((v) => !bottom.includes(v));
  fill($("#tabs"), ...bottom.map(btn), rest.length ? el("button", { type: "button",
    "aria-current": rest.some((v) => v[0] === S.view) ? "page" : null, onclick: () => {
      showDialog("More", el("div", { class: "menu" }, rest.map((v) => el("button", { type: "button", onclick: () => { $("#dlg").close(); go(v[0]); } }, v[2])),
        el("button", { type: "button", class: "danger", onclick: signOut }, "⎋ Sign out")));
    } }, "More") : null);
}

async function signOut() {
  try { await api("POST", "/api/logout"); } catch { /* signed out either way */ }
  location.replace("/");
}

function go(view) {
  if (!allowed().some((v) => v[0] === view)) view = allowed()[0][0];
  S.view = view;
  if (location.hash.slice(1) !== view) history.replaceState(null, "", "#" + view);
  refresh();
}


let rendering = false;
setRefresh(async () => {
  if (!S.me || rendering) return;
  rendering = true;
  closeMenu();
  try {
    const me = await api("GET", "/api/me");
    S.me = me; S.perms = new Set(me.perms);
    if (can("finance:read")) [S.accounts, S.sources] = await Promise.all([api("GET", "/api/accounts"), api("GET", "/api/income-sources")]);
    if (!allowed().some((v) => v[0] === S.view)) S.view = allowed()[0][0];
    const v = VIEWS.find((x) => x[0] === S.view);
    $("#viewTitle").textContent = v[1].title;
    $("#who").textContent = me.display_name || me.username;
    $("#bellCount").textContent = me.unread > 99 ? "99+" : me.unread; $("#bellCount").hidden = !me.unread;
    nav();
    const root = $("#view");
    await v[1].render(root);
  } catch (e) { if (e.message !== "Sign in required") toast(e.message, true); }
  finally { rendering = false; }
});

async function boot() {
  try {
    S.me = await api("GET", "/api/me");
    // Follow this device's time zone unless the user picked one by hand
    const device = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (device && S.me.prefs.tz_auto !== false && S.me.prefs.timezone !== device) {
      const r = await api("PUT", "/api/me/timezone", { timezone: device }).catch(() => null);
      if (r?.ok) S.me = await api("GET", "/api/me");
    }
  } catch { return; }
  S.perms = new Set(S.me.perms);
  S.day ||= S.me.today; S.month ||= S.me.today.slice(0, 7);
  $("#auth").hidden = true; $("#app").hidden = false;
  const h = location.hash.slice(1);
  const isAdmin = S.me.perms.includes("admin");
  S.view = VIEWS.some((v) => v[0] === h) ? h : (S.me.mfa_setup_required ? "settings" : isAdmin ? "admin" : "home");
  go(S.view);
  if (isAdmin && !S.me.mfa_setup_required) admin.maybeAutoTour();
}

setUnauthorizedHandler(() => { S.me = null; showAuth(boot); });
$("#bell").addEventListener("click", () => go("alerts"));
addEventListener("hashchange", () => { const h = location.hash.slice(1); if (S.me && VIEWS.some((v) => v[0] === h) && h !== S.view) go(h); });
// Server is the single source of truth: re-pull on focus and every minute (skipped while a dialog is open or dragging).
const idle = () => !document.hidden && !$("#dlg").open && !document.body.classList.contains("is-dragging") && !document.querySelector(".ctx");
document.addEventListener("visibilitychange", () => { if (S.me && idle()) refresh(); });
setInterval(() => { if (S.me && idle()) refresh(); }, 60000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

const hash = new URLSearchParams(location.hash.slice(1));
if (hash.get("reset") || hash.has("register")) showAuth(boot);
else api("GET", "/api/me").then(() => boot()).catch(() => showAuth(boot));
