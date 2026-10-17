import { api, can, el, fmtTs, refresh, S, showDialog, toast, closeDialog, confirmBox, fill } from "../core.js";

export const title = "Settings";

const sec = (title, ...kids) => el("section", { class: "card" }, el("h2", {}, title), ...kids);
const check = (name, label, val) => el("label", { class: "check" }, el("input", { type: "checkbox", name, checked: !!val }), label);

export async function render(root) {
  const me = S.me;
  const [sessions, tokens] = await Promise.all([api("GET", "/api/me/sessions"), api("GET", "/api/me/tokens")]);
  fill(root, 
    me.mfa_setup_required ? el("div", { class: "banner warn", role: "alert" },
      "Your administrator requires two-factor authentication. Turn it on below to unlock the app.") : null,
    profile(me), alertsPrefs(me), security(me, sessions),
    can("finance:read") ? apiAccess(tokens) : null, can("finance:read") ? calendarFeed(me) : null,
    el("p", { class: "muted" }, `BillTrack ${me.version} · signed in as ${me.username} (${me.role}) · time zone ${me.timezone}`));
}

function profile(me) {
  const err = el("p", { class: "err", role: "alert" });
  const pw = el("input", { type: "password", name: "current_password", autocomplete: "current-password" });
  const pwRow = el("label", { hidden: true }, "Current password (needed to change email)", pw);
  const email = el("input", { type: "email", name: "email", value: me.email, maxlength: 254, autocomplete: "email",
    oninput: () => { pwRow.hidden = email.value === me.email; } });
  const name = el("input", { name: "display_name", value: me.display_name, maxlength: 60 });
  const weekSel = el("select", {}, ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    .map((d, i) => el("option", { value: i, selected: i === me.week_start }, d)));
  const device = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const auto = el("input", { type: "checkbox", checked: me.prefs.tz_auto !== false });
  const zones = (Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : [device]);
  const zone = el("select", { "aria-label": "Time zone" }, zones.map((z) => el("option", { value: z, selected: z === (me.prefs.timezone || me.timezone) }, z.replace(/_/g, " "))));
  const zoneRow = el("label", {}, "Time zone", zone, el("span", { class: "hint" }, `System default: ${me.system_timezone}`));
  const syncTz = () => { zoneRow.hidden = auto.checked; };
  auto.addEventListener("change", syncTz); syncTz();
  const f = el("form", { class: "stack" }, el("label", {}, "Display name", name), el("label", {}, "Email (alerts and password resets)", email),
    pwRow,
    el("label", { class: "check" }, auto, `Use this device's time zone (${device.replace(/_/g, " ")})`),
    zoneRow,
    can("finance:read") ? el("label", {}, "My pay week starts on", weekSel) : null,
    el("p", { class: "hint" }, "Your time zone decides what \"today\" is: overdue bills, this week, paydays, reminder times and when your alerts go out."),
    err, el("button", { type: "submit", class: "primary" }, "Save profile"));
  f.onsubmit = async (e) => {
    e.preventDefault(); err.textContent = "";
    try {
      await api("PUT", "/api/me", { display_name: name.value, email: email.value.trim(),
        current_password: email.value.trim() !== me.email ? pw.value : null,
        prefs: { ...me.prefs, tz_auto: auto.checked, timezone: auto.checked ? device : zone.value,
          week_start: can("finance:read") ? Number(weekSel.value) : me.prefs.week_start ?? null } });
      toast("Profile saved"); refresh();
    } catch (x) { err.textContent = x.message; }
  };
  return sec("Profile", f);
}

function alertsPrefs(me) {
  const p = me.prefs, err = el("p", { class: "err", role: "alert" });
  const topic = el("input", { name: "ntfy_topic", value: p.ntfy_topic, maxlength: 64, pattern: "[A-Za-z0-9_\\-]*",
    placeholder: "long-random-topic-name", autocomplete: "off" });
  const f = el("form", { class: "stack" },
    el("p", { class: "muted" }, me.email_ready ? "" : "Email isn't set up on the server yet (an admin can do that in Admin → System settings)."),
    check("email_alerts", "Email me alerts", p.email_alerts),
    check("push_alerts", "Push alerts to my phone (ntfy app)", p.push_alerts),
    el("label", {}, `ntfy topic on ${me.push_server}`, topic,
      el("span", { class: "hint" }, "Install the ntfy app and subscribe to the same topic. Use something long and random.")),
    !can("finance:read") ? el("p", { class: "hint" }, "As an admin you'll get sign-ups waiting for approval and system problems.") :
    el("fieldset", {}, el("legend", {}, "What to send"),
      check("bill_reminders", "Upcoming bill reminders", p.bill_reminders), check("due_alerts", "Bills due today", p.due_alerts),
      check("overdue_alerts", "Overdue bills", p.overdue_alerts), check("digest", "Weekly money summary", p.digest),
      check("tracker_alerts", "Tracker milestones", p.tracker_alerts),
      check("balance_alerts", "Low balances, card utilization and card due dates", p.balance_alerts)),
    err, el("div", { class: "actions left" }, el("button", { type: "submit", class: "primary" }, "Save alerts"),
      el("button", { type: "button", onclick: async () => {
        try { const r = await api("POST", "/api/me/notify-test"); toast(`Inbox ✓ · Email: ${r.email ?? "off"} · Push: ${r.push ?? "off"}`); }
        catch (x) { toast(x.message, true); }
      } }, "Send test")));
  f.onsubmit = async (e) => {
    e.preventDefault(); err.textContent = "";
    const prefs = { ...me.prefs, ntfy_topic: topic.value.trim() };
    for (const k of ["email_alerts", "push_alerts", "bill_reminders", "due_alerts", "overdue_alerts", "digest", "tracker_alerts", "balance_alerts"])
      if (f.elements[k]) prefs[k] = f.elements[k].checked;
    try { await api("PUT", "/api/me", { display_name: me.display_name, email: me.email, prefs }); toast("Alert settings saved"); refresh(); }
    catch (x) { err.textContent = x.message; }
  };
  return sec("Alerts", f);
}

function security(me, sessions) {
  const pwErr = el("p", { class: "err", role: "alert" });
  const cur = el("input", { type: "password", autocomplete: "current-password", required: true });
  const nw = el("input", { type: "password", autocomplete: "new-password", minlength: 12, required: true });
  const nw2 = el("input", { type: "password", autocomplete: "new-password", required: true });
  const pf = el("form", { class: "stack" }, el("label", {}, "Current password", cur), el("label", {}, "New password (12+ characters)", nw),
    el("label", {}, "Confirm new password", nw2), pwErr, el("button", { type: "submit", class: "primary" }, "Change password"));
  pf.onsubmit = async (e) => {
    e.preventDefault(); pwErr.textContent = "";
    if (nw.value !== nw2.value) { pwErr.textContent = "New passwords don't match"; return; }
    try { await api("POST", "/api/me/password", { current_password: cur.value, new_password: nw.value }); pf.reset();
      toast("Password changed. Other devices were signed out."); refresh(); } catch (x) { pwErr.textContent = x.message; }
  };
  const tfa = me.totp_enabled
    ? el("div", {}, el("p", {}, "✓ Two-factor authentication is on."),
      el("button", { type: "button", onclick: disable2fa }, "Turn off two-factor"))
    : el("div", {}, el("p", {}, "Add a code from an authenticator app (Google Authenticator, Authy, 1Password, etc.) to every sign-in."),
      el("button", { type: "button", class: "primary", onclick: setup2fa }, "Set up two-factor"));
  const sess = el("ul", { class: "list" }, sessions.map((s) => el("li", {},
    el("span", {}, el("span", {}, uaLabel(s.user_agent), s.current ? el("span", { class: "tag" }, "This device") : null),
      el("div", { class: "meta" }, `${s.ip} · last active ${fmtTs(s.last_seen)} · signed in ${fmtTs(s.created_at)}`)),
    s.current ? null : el("button", { type: "button", onclick: () => api("DELETE", `/api/me/sessions/${s.id}`).then(refresh) }, "Sign out"))));
  return sec("Security", el("h3", {}, "Password"), pf, el("h3", {}, "Two-factor authentication"), tfa,
    el("h3", {}, "Signed-in devices"), sess,
    sessions.length > 1 ? el("button", { type: "button", onclick: () => api("POST", "/api/me/sessions/revoke-others").then(() => { toast("Other devices signed out"); refresh(); }) }, "Sign out all other devices") : null,
    el("div", { class: "actions left" }, el("button", { type: "button", onclick: async () => { await api("POST", "/api/logout"); location.reload(); } }, "Sign out")));
}

function uaLabel(ua) {
  if (!ua) return "Unknown device";
  const os = /iPhone|iPad/.test(ua) ? "iPhone/iPad" : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows" : /Mac OS/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "Device";
  const br = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "";
  return `${br} on ${os}`.trim();
}

async function setup2fa() {
  let s;
  try { s = await api("POST", "/api/me/2fa/setup"); } catch (x) { toast(x.message, true); return; }
  const code = el("input", { inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, pattern: "\\d{6}", required: true });
  const err = el("p", { class: "err", role: "alert" });
  const go = el("button", { type: "submit", class: "primary" }, "Turn on");
  const form = showDialog("Set up two-factor", el("div", { class: "stack" },
    el("p", {}, "1. In your authenticator app, add an account. On this phone you can tap the link; otherwise type the key."),
    el("a", { href: s.uri, class: "btn" }, "Open in authenticator app"),
    el("p", {}, "Setup key:"), el("code", { class: "secret" }, s.secret.match(/.{1,4}/g).join(" ")),
    el("label", {}, "2. Enter the 6-digit code it shows", code), err), [go]);
  form.onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("POST", "/api/me/2fa/enable", { code: code.value });
      showDialog("Save your recovery codes", el("div", { class: "stack" },
        el("p", {}, "Each code works once if you lose your phone. Store them in a password manager. They won't be shown again."),
        el("pre", { class: "codes" }, r.recovery_codes.join("\n")),
        el("button", { type: "button", onclick: () => navigator.clipboard?.writeText(r.recovery_codes.join("\n")).then(() => toast("Copied")) }, "Copy codes")));
      $dlgOnClose(refresh);
    } catch (x) { err.textContent = x.message; }
  };
}
const $dlgOnClose = (fn) => document.getElementById("dlg").addEventListener("close", fn, { once: true });

function disable2fa() {
  const pw = el("input", { type: "password", autocomplete: "current-password", required: true });
  const err = el("p", { class: "err", role: "alert" });
  const form = showDialog("Turn off two-factor", el("div", {}, el("label", {}, "Your password", pw), err),
    [el("button", { type: "submit", class: "danger-solid" }, "Turn off")]);
  form.onsubmit = async (e) => {
    e.preventDefault();
    try { await api("POST", "/api/me/2fa/disable", { password: pw.value }); closeDialog(); toast("Two-factor is off"); refresh(); }
    catch (x) { err.textContent = x.message; }
  };
}

function apiAccess(tokens) {
  const list = el("ul", { class: "list" }, tokens.map((t) => el("li", {},
    el("span", {}, el("span", {}, t.name, el("span", { class: "tag" }, t.scope)),
      el("div", { class: "meta" }, `${t.prefix}… · created ${fmtTs(t.created_at)} · ${t.expires_at ? "expires " + fmtTs(t.expires_at) : "no expiry"} · last used ${fmtTs(t.last_used_at)}`)),
    el("button", { type: "button", class: "danger", onclick: async () => {
      if (await confirmBox("Revoke token?", `Scripts using "${t.name}" will stop working.`, "Revoke")) {
        api("DELETE", `/api/me/tokens/${t.id}`).then(refresh);
      }
    } }, "Revoke"))));
  const create = () => {
    const name = el("input", { maxlength: 100, required: true, placeholder: "Price updater" });
    const scope = el("select", {}, el("option", { value: "read" }, "Read only"), el("option", { value: "write" }, "Read and write"));
    const exp = el("select", {}, [[30, "30 days"], [90, "90 days"], [365, "1 year"], [0, "Never"]].map(([v, l]) => el("option", { value: v, selected: v === 90 }, l)));
    const form = showDialog("New API token", el("div", { class: "stack" }, el("label", {}, "Name", name), el("label", {}, "Access", scope),
      el("label", {}, "Expires", exp), el("p", { class: "hint" }, "Tokens can only do what your account can, never admin actions.")),
    [el("button", { type: "submit", class: "primary" }, "Create")]);
    form.onsubmit = async (e) => {
      e.preventDefault();
      try {
        const r = await api("POST", "/api/me/tokens", { name: name.value, scope: scope.value, expires_days: Number(exp.value) });
        showDialog("Copy your token now", el("div", { class: "stack" }, el("p", {}, "This is the only time it will be shown."),
          el("code", { class: "secret" }, r.token),
          el("button", { type: "button", onclick: () => navigator.clipboard?.writeText(r.token).then(() => toast("Copied")) }, "Copy"),
          el("p", { class: "hint" }, "Use it as: Authorization: Bearer <token>")));
        $dlgOnClose(refresh);
      } catch (x) { toast(x.message, true); }
    };
  };
  return sec("API access", el("p", { class: "muted" }, `Personal tokens for scripts and integrations. API base: ${location.origin}/api — schema at /api/openapi.json.`),
    tokens.length ? list : null, el("button", { type: "button", onclick: create }, "New token"));
}

function calendarFeed(me) {
  const make = async () => {
    if (me.has_feed && !(await confirmBox("New calendar link?", "The old link stops working. Calendars subscribed to it need the new one.", "Make new link", false))) return;
    try {
      const r = await api("POST", "/api/me/calendar-feed");
      const url = r.url && r.url.startsWith("http") ? r.url : location.origin + r.path;
      showDialog("Your calendar link", el("div", { class: "stack" },
        el("p", {}, "Subscribe to this in Google Calendar (Other calendars → From URL), Outlook, or iPhone (Settings → Calendar → Accounts → Add Subscribed Calendar). Keep it private: anyone with it can see your bill dates."),
        el("code", { class: "secret" }, url),
        el("button", { type: "button", onclick: () => navigator.clipboard?.writeText(url).then(() => toast("Copied")) }, "Copy"),
        el("p", { class: "hint" }, "Shown once. You can always make a new one (which turns this one off).")));
      document.getElementById("dlg").addEventListener("close", refresh, { once: true });
    } catch (x) { toast(x.message, true); }
  };
  return sec("Calendar app", el("p", { class: "muted" }, "Show your bills and paydays in your phone or computer calendar."),
    el("div", { class: "actions left" }, el("button", { type: "button", class: "primary", onclick: make }, me.has_feed ? "Make a new link" : "Get my calendar link"),
      me.has_feed ? el("button", { type: "button", class: "danger", onclick: () => api("DELETE", "/api/me/calendar-feed").then(() => { toast("Calendar link turned off"); refresh(); }) }, "Turn off link") : null));
}
