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
  ...admin.SECTIONS.map(([k, label], i) => [`admin-${k}`, admin.view(k, label), label, "admin", i < 3]),
  ["alerts", alerts, "Alerts", "self", true],
  ["settings", settings, () => (can("admin") ? "My account" : "Settings"), "self", true],
];
const allowed = () => VIEWS.filter((v) => can(v[3]) && (!S.me.mfa_setup_required || v[0] === "settings"));

function nav() {
  const vs = allowed();
  const label = (v) => (typeof v[2] === "function" ? v[2]() : v[2]);
  const btn = (v) => el("button", { type: "button", "aria-current": S.view === v[0] ? "page" : null, onclick: () => go(v[0]) },
    label(v), v[0] === "alerts" ? el("span", { class: "badge", hidden: !S.me.unread }, S.me.unread) : null);
  $("#sidenav").replaceChildren(...vs.map(btn),
    el("button", { type: "button", class: "signout", onclick: () => signOut() }, "⎋ Sign out"));
  const bottom = vs.filter((v) => v[4]).slice(0, 4);
  const rest = vs.filter((v) => !bottom.includes(v));
  fill($("#tabs"), ...bottom.map(btn), rest.length ? el("button", { type: "button",
    "aria-current": rest.some((v) => v[0] === S.view) ? "page" : null, onclick: () => {
      showDialog("More", el("div", { class: "menu" }, rest.map((v) => el("button", { type: "button", onclick: () => { $("#dlg").close(); go(v[0]); } }, label(v))),
        el("button", { type: "button", class: "danger", onclick: () => signOut() }, "⎋ Sign out")));
    } }, "More") : null);
}

async function signOut(message) {
  try { await api("POST", "/api/logout"); } catch { /* signed out either way */ }
  try { if (typeof message === "string") sessionStorage.setItem("bt-msg", message); } catch { /* ignore */ }
  location.replace("/");
}

// Session limits: hard limit from the server, and real inactivity (the background refresh doesn't count).
// Two minutes before either one, a bar offers to keep going.
let lastActivity = Date.now();
["pointerdown", "keydown", "wheel", "touchstart"].forEach((ev) => addEventListener(ev, () => { lastActivity = Date.now(); }, { passive: true }));
const bar = el("div", { class: "session-bar", role: "alert", hidden: true });
document.body.append(bar);
const mmss = (ms) => { const t = Math.max(0, Math.ceil(ms / 1000)); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`; };
async function staySignedIn() {
  try {
    const r = await api("POST", "/api/me/session/extend");
    S.me.session_expires_at = r.session_expires_at;
    lastActivity = Date.now();
    bar.hidden = true;
    toast("You're signed in for another " + (S.me.session_minutes || 15) + " minutes");
  } catch (e) { toast(e.message, true); }
}
let barMode = "";
setInterval(() => {
  if (!S.me || !S.me.session_expires_at) { bar.hidden = true; return; }
  const left = new Date(S.me.session_expires_at).getTime() - Date.now();
  const idleMs = (S.me.idle_minutes || 15) * 60000 - (Date.now() - lastActivity);
  if (idleMs <= 0) return signOut("You were signed out after being inactive.");
  if (left <= 0) return signOut("Your session ended. Please sign in again.");
  const soon = Math.min(left, idleMs);
  if (soon > 120000) { bar.hidden = true; barMode = ""; return; }
  const mode = left <= idleMs ? "limit" : "idle";
  if (mode !== barMode || bar.hidden) {
    barMode = mode;
    bar.replaceChildren(
      el("span", { class: "msg" }),
      el("button", { type: "button", class: "primary", onclick: mode === "limit" ? staySignedIn : () => { lastActivity = Date.now(); bar.hidden = true; } },
        mode === "limit" ? "Stay signed in" : "I'm still here"),
      el("button", { type: "button", onclick: () => signOut() }, "Sign out"));
    bar.hidden = false;
  }
  bar.querySelector(".msg").textContent = mode === "limit" ? `Your session ends in ${mmss(left)}.`
    : `Still there? Signing out in ${mmss(idleMs)} because nothing has happened for a while.`;
}, 1000);

let navTimer = null;
function go(view) {
  if (!allowed().some((v) => v[0] === view)) view = allowed()[0][0];
  S.view = view;
  if (location.hash.slice(1) !== view) history.replaceState(null, "", "#" + view);
  // show the choice instantly; load once the clicking settles (a burst of clicks = one load)
  const v = VIEWS.find((x) => x[0] === view);
  $("#viewTitle").textContent = typeof v[2] === "function" ? v[2]() : v[1].title;
  nav();
  clearTimeout(navTimer);
  navTimer = setTimeout(() => runRefresh(true), 120);
}

// Rendering: only the newest request ever draws. Each render gets its own container; an older render that finishes
// late writes into a container that's no longer on the page, so it can't overwrite what you clicked last.
let gen = 0;
const shared = { at: 0 };
async function runRefresh(light = false) {
  if (!S.me) return;
  const my = ++gen;
  closeMenu();
  try {
    // Who you are, your accounts and pay are reused for 20 s while you're just moving between pages.
    if (!light || Date.now() - shared.at > 20000) {
      const me = await api("GET", "/api/me");
      if (my !== gen) return;
      S.me = me; S.perms = new Set(me.perms);
      if (can("finance:read")) [S.accounts, S.sources] = await Promise.all([api("GET", "/api/accounts"), api("GET", "/api/income-sources")]);
      if (my !== gen) return;
      shared.at = Date.now();
    }
    if (!allowed().some((v) => v[0] === S.view)) S.view = allowed()[0][0];
    const v = VIEWS.find((x) => x[0] === S.view);
    $("#viewTitle").textContent = typeof v[2] === "function" ? v[2]() : v[1].title;
    $("#who").textContent = S.me.display_name || S.me.username;
    $("#bellCount").textContent = S.me.unread > 99 ? "99+" : S.me.unread; $("#bellCount").hidden = !S.me.unread;
    nav();
    const host = el("div", { class: "view-host" });
    $("#view").replaceChildren(host);
    await v[1].render(host);
  } catch (e) {
    if (my === gen && e.message !== "Sign in required") toast(e.message, true);
  }
}
setRefresh(() => runRefresh(false));

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
  const legacy = h === "admin" ? "admin-overview" : h;   // old #admin bookmarks
  S.view = VIEWS.some((v) => v[0] === legacy) ? legacy : (S.me.mfa_setup_required ? "settings" : isAdmin ? "admin-overview" : "home");
  go(S.view);
  if (isAdmin && !S.me.mfa_setup_required) admin.maybeAutoTour();
}

setUnauthorizedHandler(() => { S.me = null; showAuth(boot); });
$("#bell").addEventListener("click", () => go("alerts"));
addEventListener("hashchange", () => {
  let h = location.hash.slice(1);
  if (h === "admin") h = "admin-overview";              // old bookmarks
  if (S.me && VIEWS.some((v) => v[0] === h) && h !== S.view) go(h);
});
// Server is the single source of truth: re-pull on focus and every minute (skipped while a dialog is open or dragging).
const idle = () => !document.hidden && !$("#dlg").open && !document.body.classList.contains("is-dragging") && !document.querySelector(".ctx");
document.addEventListener("visibilitychange", () => { if (S.me && idle()) refresh(); });
setInterval(() => { if (S.me && idle()) refresh(); }, 60000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

const hash = new URLSearchParams(location.hash.slice(1));
if (hash.get("reset") || hash.has("register")) showAuth(boot);
else api("GET", "/api/me").then(() => boot()).catch(() => showAuth(boot));
