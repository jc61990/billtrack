import { api, closeDialog, confirmBox, el, fmtTs, money, onContext, openForm, refresh, S, showDialog, showMenu, toast, fill } from "../core.js";

export const title = "Admin";
let tab = "overview", auditQ = "";
const TABS = [["overview", "Overview"], ["users", "Users"], ["codes", "Invite codes"], ["settings", "Settings"],
  ["audit", "Audit log"], ["tokens", "API tokens"], ["system", "Backup & system"]];
const ROLE_HELP = { admin: "runs the system (settings, users, backups); no finance pages", user: "their own private finances; no system settings" };

export async function render(root) {
  try { const t = sessionStorage.getItem("bt-admin-tab"); if (t) { tab = t; sessionStorage.removeItem("bt-admin-tab"); } } catch { /* ignore */ }
  const body = el("div");
  fill(root, el("div", { class: "seg", role: "tablist" }, TABS.map(([k, l]) =>
    el("button", { type: "button", role: "tab", "aria-selected": String(tab === k), dataset: { tab: k }, onclick: () => { tab = k; render(root); } }, l))), body);
  await ({ overview, users, codes, settings, audit, tokens, system })[tab](body);
  if (tourStep !== null) showTourStep();
}
export const openTab = (t) => { tab = t; };

// ---------- overview + guided setup ----------
async function overview(root) {
  const o = await api("GET", "/api/admin/overview");
  const c = o.checklist;
  const item = (ok, label, hint, go) => el("li", { class: ok ? "done" : "" },
    el("span", { class: "ck" }, ok ? "✓" : "○"), el("span", {}, el("strong", {}, label), el("div", { class: "meta" }, hint)),
    go ? el("button", { type: "button", onclick: go }, ok ? "Review" : "Set up") : null);
  fill(root,
    el("div", { class: "kpis" },
      el("div", {}, el("small", {}, "Users"), el("strong", {}, o.users)),
      el("div", {}, el("small", {}, "Waiting for approval"), el("strong", { class: o.pending ? "out" : "" }, o.pending)),
      el("div", {}, el("small", {}, "Open invite codes"), el("strong", {}, o.codes_open)),
      el("div", {}, el("small", {}, "Admins"), el("strong", {}, o.admins))),
    o.pending ? el("div", { class: "banner warn" }, `${o.pending} sign-up${o.pending > 1 ? "s" : ""} waiting. `,
      el("button", { type: "button", onclick: () => { tab = "users"; refresh(); } }, "Review")) : null,
    el("section", { class: "card" },
      el("div", { class: "spread" }, el("h2", {}, "Setup checklist"),
        el("button", { type: "button", class: "primary", onclick: () => startTour() }, o.setup_done ? "Run the setup guide again" : "Start the setup guide")),
      el("ul", { class: "checklist" },
        item(c.base_url, "Public web address", "Used in emails: sign-up approvals, password resets, invite links.", () => goStep(1)),
        item(c.email, "Email", "So people get approval emails, password resets and their alerts.", () => goStep(2)),
        item(c.my_2fa, "Two-factor on your admin account", "Settings → Security. Admin accounts deserve it most.", () => { location.hash = "settings"; }),
        item(c.registration !== "closed", `Sign-ups: ${({ approval: "open, admin approves", invite_only: "invite code required", closed: "closed" })[c.registration]}`,
          "How new people get accounts.", () => goStep(4)),
        item(c.passphrase, "Database passphrase", c.passphrase ? "Data needs your passphrase + this server's key file." : "Run sudo ./setup.sh on the server.", null),
        item(c.backups > 0, "Backups", `${c.backups} on the server. Download a disaster-recovery copy and keep it off the server.`, () => { tab = "system"; refresh(); }))));
}

const TOUR = [
  { tab: "overview", sel: ".checklist", title: "Welcome, admin 👋",
    text: "Admin accounts run the system. They don't have bills or money pages; people get their own private accounts for that. This guide sets up the basics in about five minutes." },
  { tab: "settings", sel: "fieldset[data-k=general]", title: "1. Web address and time zone",
    text: "Enter the address people use to reach BillTrack (e.g. https://10.45.1.7 or https://bills.example.com). It goes into emails. The time zone is only a default: each person's own device sets theirs." },
  { tab: "settings", sel: "fieldset[data-k=email]", title: "2. Email",
    text: "Fill in your mail server, save, then press Send test email. Without email, nobody hears that their sign-up was approved or can reset a password." },
  { tab: "settings", sel: "fieldset[data-k=security]", title: "3. Security",
    text: "Consider requiring two-factor for everyone. Then turn it on for yourself under Settings → Security." },
  { tab: "settings", sel: "fieldset[data-k=signup]", title: "4. How people sign up",
    text: "Open sign-up with admin approval, invite code only, or closed. Remember to Save settings." },
  { tab: "codes", sel: "[data-tour=newcode]", title: "5. Invite people",
    text: "Make an invite code to let someone skip approval. Email it straight from here, or copy the link. You'll see who used each code." },
  { tab: "users", sel: "[data-tour=pending]", title: "6. Approvals",
    text: "Sign-ups without a code land here. Approve or reject them; approved people get an email saying they can sign in. That's it: people can now open the site and press Create an account." },
];
let tourStep = null, tourRoot = null;
function startTour() { tourStep = 0; tab = TOUR[0].tab; refresh(); }
function goStep(n) { tourStep = n; tab = TOUR[n].tab; refresh(); }
function endTour(done) {
  tourStep = null; document.querySelector(".tour")?.remove();
  document.querySelectorAll(".tour-target").forEach((x) => x.classList.remove("tour-target"));
  if (done) api("POST", "/api/admin/setup-done").then(() => toast("Setup guide finished. You can rerun it from Admin → Overview."));
}
function showTourStep() {
  document.querySelector(".tour")?.remove();
  document.querySelectorAll(".tour-target").forEach((x) => x.classList.remove("tour-target"));
  const st = TOUR[tourStep];
  const target = document.querySelector(st.sel);
  if (target) { target.classList.add("tour-target"); target.scrollIntoView({ behavior: "smooth", block: "center" }); }
  const last = tourStep === TOUR.length - 1;
  const box = el("div", { class: "tour", role: "dialog", "aria-label": st.title },
    el("div", { class: "muted small" }, `Step ${tourStep + 1} of ${TOUR.length}`), el("h3", {}, st.title), el("p", {}, st.text),
    el("div", { class: "actions" },
      el("button", { type: "button", class: "link", onclick: () => endTour(false) }, "Skip guide"), el("span", { class: "spacer" }),
      tourStep > 0 ? el("button", { type: "button", onclick: () => goStep(tourStep - 1) }, "Back") : null,
      el("button", { type: "button", class: "primary", onclick: () => (last ? endTour(true) : goStep(tourStep + 1)) }, last ? "Finish" : "Next")));
  document.body.append(box);
}
export async function maybeAutoTour() {
  try { const o = await api("GET", "/api/admin/overview"); if (!o.setup_done && tourStep === null) startTour(); } catch { /* ignore */ }
}

// ---------- users ----------
async function users(root) {
  const [list, pend] = await Promise.all([api("GET", "/api/admin/users"), api("GET", "/api/admin/registrations")]);
  const waiting = pend.filter((u) => u.status === "pending"), rejected = pend.filter((u) => u.status === "rejected");
  const decide = (u, ok) => api("POST", `/api/admin/registrations/${u.id}/${ok ? "approve" : "reject"}`)
    .then(() => { toast(ok ? `${u.username} approved` : `${u.username} rejected`); refresh(); }).catch((e) => toast(e.message, true));
  fill(root,
    el("section", { class: "card", dataset: { tour: "pending" } }, el("h2", {}, `Waiting for approval (${waiting.length})`),
      waiting.length ? el("ul", { class: "list" }, waiting.map((u) => el("li", {},
        el("span", {}, el("strong", {}, u.username), el("div", { class: "meta" }, [u.display_name, u.email, `signed up ${fmtTs(u.registered_at)}`].filter(Boolean).join(" · "))),
        el("span", { class: "actions left" }, el("button", { type: "button", class: "primary", onclick: () => decide(u, true) }, "Approve"),
          el("button", { type: "button", class: "danger", onclick: () => decide(u, false) }, "Reject")))))
        : el("p", { class: "muted" }, "Nobody waiting. New sign-ups without an invite code appear here."),
      rejected.length ? el("details", {}, el("summary", { class: "muted small" }, `${rejected.length} rejected`),
        el("ul", { class: "list" }, rejected.map((u) => el("li", {}, el("span", {}, u.username, el("div", { class: "meta" }, u.email)),
          el("button", { type: "button", onclick: () => decide(u, true) }, "Approve after all"))))) : null),
    el("div", { class: "spread" }, el("p", { class: "muted" }, `admin: ${ROLE_HELP.admin} · user: ${ROLE_HELP.user}`),
      el("button", { type: "button", class: "primary", onclick: () => userCreate() }, "Add user")),
    el("div", { class: "scroll-x" }, el("table", { class: "tbl" },
      el("tr", {}, el("th", {}, "Account"), el("th", {}, "Role"), el("th", {}, "2FA"), el("th", {}, "Status"), el("th", {}, "Last active")),
      list.filter((u) => u.status === "active").map((u) => {
        const locked = u.locked_until && new Date(u.locked_until) > new Date();
        const tr = el("tr", { class: "click", tabindex: 0, onclick: () => userEdit(u) },
          el("td", {}, el("strong", {}, u.username), el("div", { class: "meta" }, [u.display_name, u.email].filter(Boolean).join(" · "))),
          el("td", {}, el("span", { class: `tag ${u.role === "admin" ? "moved" : ""}` }, u.role)),
          el("td", {}, u.totp_enabled ? "On" : "Off"),
          el("td", {}, u.disabled ? el("span", { class: "tag overdue" }, "Disabled") : locked ? el("span", { class: "tag due-soon" }, "Locked") : "Active"),
          el("td", { class: "small" }, fmtTs(u.last_seen)));
        onContext(tr, () => userMenu(u, null, locked));
        return tr;
      }))));
}

function userMenu(u, _meta, locked) {
  const self = u.id === S.me.id;
  const act = (path, msg, confirmText) => async () => {
    if (confirmText && !(await confirmBox("Are you sure?", confirmText, "Yes"))) return;
    try { await api("POST", `/api/admin/users/${u.id}/${path}`); toast(msg); refresh(); } catch (e) { toast(e.message, true); }
  };
  return [
    { label: "Edit…", action: () => userEdit(u) },
    { label: "Set a new password…", action: () => setPw(u) },
    { label: "Email a password reset link", disabled: !u.email, action: act("send-reset", "Reset link sent") },
    locked ? { label: "Unlock", action: act("unlock", "Unlocked") } : null,
    u.totp_enabled ? { label: "Reset two-factor", action: act("reset-2fa", "Two-factor reset", `${u.username} will sign in with just a password and must set up 2FA again.`) } : null,
    { label: "Sign out everywhere", action: act("revoke-sessions", "Signed out") },
    "-",
    { label: "Delete account", danger: true, disabled: self, action: async () => {
      if (await confirmBox(`Delete ${u.username}?`, u.role === "user" ? "Their login AND all their private financial data are deleted permanently." : "Their admin login is removed.", "Delete")) {
        api("DELETE", `/api/admin/users/${u.id}`).then(() => { toast("Deleted"); refresh(); }).catch((e) => toast(e.message, true));
      }
    } },
  ];
}

function userCreate() {
  openForm("Add an account", [
    { name: "username", label: "Username", required: true, max: 32, pattern: "[A-Za-z0-9_.\\-]{3,32}", autocomplete: "off" },
    { name: "display_name", label: "Display name", max: 60 },
    { name: "email", label: "Email", type: "email", max: 254 },
    { name: "role", label: "Account type", type: "select", options: [["user", "User (their own finances)"], ["admin", "Admin (runs the system, no finances)"]], value: "user" },
    { name: "password", label: "Temporary password (12+)", type: "password", required: true, autocomplete: "new-password",
      hint: "Tell them to change it in Settings. Easier: make an invite code and let them sign up themselves." },
  ], (d) => api("POST", "/api/admin/users", d));
}

function userEdit(u) {
  const roleSel = el("select", { name: "role" }, [["user", "User (their own finances)"], ["admin", "Admin (runs the system, no finances)"]]
    .map(([v, l]) => el("option", { value: v, selected: u.role === v }, l)));
  const name = el("input", { value: u.display_name, maxlength: 60 }), email = el("input", { type: "email", value: u.email, maxlength: 254 });
  const disabled = el("input", { type: "checkbox", checked: !!u.disabled });
  const err = el("p", { class: "err", role: "alert" });
  const form = showDialog(`Account: ${u.username}`, el("div", { class: "stack" },
    el("label", {}, "Display name", name), el("label", {}, "Email", email), el("label", {}, "Account type", roleSel),
    el("p", { class: "hint" }, "Changing the type doesn't move or delete their data; an admin simply can't open finance pages."),
    el("label", { class: "check" }, disabled, "Disabled (blocks sign-in and API tokens)"), err),
  [el("button", { type: "submit", class: "primary" }, "Save")]);
  form.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("PUT", `/api/admin/users/${u.id}`, { display_name: name.value, email: email.value.trim(), role: roleSel.value, disabled: disabled.checked, perms_deny: [] });
      closeDialog(); toast("Saved"); refresh();
    } catch (x) { err.textContent = x.message; }
  };
}

// ---------- invite codes ----------
async function codes(root) {
  const list = await api("GET", "/api/admin/codes");
  const label = { unused: ["Unused", ""], active: ["Partly used", "moved"], used: ["Used", "bank"], expired: ["Expired", "due-soon"], revoked: ["Revoked", "overdue"] };
  fill(root,
    el("div", { class: "spread" }, el("p", { class: "muted" }, "Someone with a code signs up without waiting for approval. Each code can be limited to a number of uses and an expiry date."),
      el("button", { type: "button", class: "primary", dataset: { tour: "newcode" }, onclick: newCode }, "New invite code")),
    list.length ? el("div", { class: "scroll-x" }, el("table", { class: "tbl" },
      el("tr", {}, el("th", {}, "Code"), el("th", {}, "Note"), el("th", {}, "Status"), el("th", {}, "Used by"), el("th", {}, "Expires"), el("th")),
      list.map((c) => el("tr", {},
        el("td", {}, el("code", {}, c.code), el("div", { class: "meta" }, `made ${fmtTs(c.created_at)}${c.created_by_name ? " by " + c.created_by_name : ""}${c.emailed_to ? " · emailed to " + c.emailed_to : ""}`)),
        el("td", {}, c.note || "—"),
        el("td", {}, el("span", { class: `tag ${label[c.state][1]}` }, label[c.state][0]), el("div", { class: "meta" }, `${c.uses} of ${c.max_uses} used`)),
        el("td", { class: "small" }, c.used_by.length ? c.used_by.map((x) => el("div", {}, `${x.email} (${x.username}) · ${fmtTs(x.used_at)}`)) : "—"),
        el("td", { class: "small" }, c.expires_at ? fmtTs(c.expires_at) : "never"),
        el("td", {}, ["unused", "active"].includes(c.state) ? el("span", { class: "actions left" },
          el("button", { type: "button", onclick: () => navigator.clipboard?.writeText(c.code).then(() => toast("Code copied")) }, "Copy"),
          el("button", { type: "button", class: "danger", onclick: () => api("DELETE", `/api/admin/codes/${c.id}`).then(() => { toast("Revoked"); refresh(); }) }, "Revoke")) : null)))))
      : el("p", { class: "muted" }, "No invite codes yet."));
}

function newCode() {
  openForm("New invite code", [
    { name: "note", label: "Note (who it's for)", max: 100, placeholder: "For Kim" },
    { name: "max_uses", label: "Can be used", type: "select", int: 1, options: [[1, "Once"], [2, "2 times"], [5, "5 times"], [10, "10 times"], [50, "50 times"]], value: 1, pair: 1 },
    { name: "expires_days", label: "Expires after", type: "select", int: 1, options: [[1, "1 day"], [7, "1 week"], [14, "2 weeks"], [30, "30 days"], [0, "Never"]], value: 14, pair: 1 },
    { name: "email", label: "Email it to (optional)", type: "email", max: 254, hint: S.me.email_ready ? "" : "Email isn't set up yet; copy the code instead." },
  ], async (d) => {
    const r = await api("POST", "/api/admin/codes", { note: d.note, max_uses: d.max_uses, expires_days: d.expires_days, email: d.email || "" });
    setTimeout(() => showDialog("Invite code", el("div", { class: "stack" },
      el("code", { class: "secret big-code" }, r.code),
      r.link ? el("p", {}, "Or send this link (fills the code in for them):") : null, r.link ? el("code", { class: "secret" }, r.link) : null,
      el("p", { class: "muted" }, r.emailed ? "Emailed." : "On the sign-up page they tick \"I have an invite code\" and enter it."),
      el("button", { type: "button", onclick: () => navigator.clipboard?.writeText(r.link || r.code).then(() => toast("Copied")) }, "Copy")), []), 50);
  }, null, "Create code");
}

function setPw(u) {
  openForm(`New password for ${u.username}`, [
    { name: "password", label: "Password (12+ characters)", type: "password", required: true, autocomplete: "new-password",
      hint: "Signs them out everywhere." },
  ], (d) => api("POST", `/api/admin/users/${u.id}/password`, d));
}

// ---------- system settings ----------
async function settings(root) {
  const s = await api("GET", "/api/admin/settings");
  const f = (name, label, type = "text", extra = {}) => {
    const i = el("input", { name, type, value: type === "password" ? "" : s[name] ?? "", autocomplete: "off", ...extra });
    if (type === "checkbox") { i.checked = !!s[name]; i.value = "on"; return el("label", { class: "check" }, i, label); }
    return el("label", {}, label, i, extra.hint ? el("span", { class: "hint" }, extra.hint) : null);
  };
  const sel = (name, label, opts) => el("label", {}, label, el("select", { name }, opts.map(([v, l]) => el("option", { value: v, selected: String(s[name]) === String(v) }, l))));
  const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((d, i) => [i, d]);
  const err = el("p", { class: "err", role: "alert" });
  const form = el("form", { class: "stack" },
    el("fieldset", { dataset: { k: "general" } }, el("legend", {}, "General"),
      f("base_url", "Public URL", "url", { placeholder: "https://bills.example.com", hint: "Used in emails for reset and invite links." }),
      f("timezone", "Default time zone (for people who haven't got one, and system tasks)", "text",
        { placeholder: "America/New_York", hint: `This browser: ${Intl.DateTimeFormat().resolvedOptions().timeZone}. Each person's own zone is set automatically from their device.` }),
      sel("week_start", "Default pay-week start for new users (each user can change theirs)", days)),
    el("fieldset", { dataset: { k: "signup" } }, el("legend", {}, "Sign-ups"),
      sel("registration_mode", "New accounts", [["approval", "Anyone can sign up; an admin approves them"],
        ["invite_only", "Invite code required"], ["closed", "Closed (admins create accounts)"]]),
      el("p", { class: "hint" }, "People with a valid invite code always skip approval (unless sign-ups are closed).")),
    el("fieldset", { dataset: { k: "security" } }, el("legend", {}, "Security"),
      f("require_2fa", "Require two-factor for everyone", "checkbox"),
      f("session_days", "Stay signed in for (days)", "number", { min: 1, max: 90 }),
      f("idle_days", "Sign out after idle (days)", "number", { min: 1, max: 90 })),
    el("fieldset", {}, el("legend", {}, "Weekly summary"),
      sel("digest_weekday", "Send on", days), f("digest_hour", "Hour (0-23)", "number", { min: 0, max: 23 })),
    el("fieldset", { dataset: { k: "email" } }, el("legend", {}, "Email (SMTP, TLS required)"),
      f("smtp_host", "Server", "text", { placeholder: "smtp.gmail.com" }),
      el("div", { class: "row2" }, f("smtp_port", "Port", "number", { min: 1, max: 65535 }),
        sel("smtp_tls", "Encryption (always on)", [["starttls", "STARTTLS (587 or 25)"], ["ssl", "SSL/TLS (465)"]])),
      sel("smtp_auth", "Sign in to the mail server", [["password", "Username and password"],
        ["none", "None: this server's IP is allow-listed (e.g. Google Workspace SMTP relay)"]]),
      f("smtp_user", "Username"), f("smtp_pass", "Password", "password", { placeholder: s.smtp_pass_set ? "•••••••• (saved, leave blank to keep)" : "",
        hint: "Stored encrypted. Never shown again. Not used when sign-in is None." }),
      f("smtp_helo", "EHLO name (optional)", "text", { placeholder: "bills.example.com",
        hint: "The hostname announced to the mail server. Leave blank to use this machine's name. Google's relay may want a real domain name here." }),
      f("smtp_from", "From address", "email")),
    el("fieldset", {}, el("legend", {}, "Bank sync (Plaid)"),
      el("p", { class: "hint" }, "Lets users link their own banks. Free personal-use keys: dashboard.plaid.com → sign up → Personal use → Developers → Keys."),
      f("plaid_client_id", "Client ID", "text", { autocomplete: "off" }),
      f("plaid_secret", "Secret", "password", { placeholder: s.plaid_secret_set ? "•••••••• (saved, leave blank to keep)" : "", hint: "Stored encrypted. Never shown again." }),
      el("div", { class: "row2" }, sel("plaid_env", "Environment", [["production", "Production (real banks)"], ["sandbox", "Sandbox (test data)"]]),
        f("plaid_sync_hours", "Sync every (hours)", "number", { min: 1, max: 24 }))),
    el("fieldset", {}, el("legend", {}, "Phone push (ntfy)"),
      f("ntfy_server", "ntfy server", "url"), f("ntfy_token", "Access token (self-hosted ntfy)", "password",
        { placeholder: s.ntfy_token_set ? "•••••••• (saved, leave blank to keep)" : "" })),
    err,
    el("div", { class: "actions left" }, el("button", { type: "submit", class: "primary" }, "Save settings"),
      el("button", { type: "button", onclick: () => api("POST", "/api/admin/settings/test-email").then(() => toast("Test email sent")).catch((e) => toast(e.message, true)) }, "Send test email")));
  // Email: hide username/password when the relay trusts our IP; keep the port in step with the security mode.
  const auth = form.elements.smtp_auth, tls = form.elements.smtp_tls, port = form.elements.smtp_port;
  const credRows = ["smtp_user", "smtp_pass"].map((n) => form.elements[n].closest("label"));
  const syncAuth = () => credRows.forEach((r) => { r.hidden = auth.value === "none"; });
  auth.addEventListener("change", syncAuth); syncAuth();
  tls.addEventListener("change", () => {
    if (["", "25", "465", "587"].includes(port.value)) port.value = tls.value === "ssl" ? "465" : "587";
  });
  form.onsubmit = async (e) => {
    e.preventDefault(); err.textContent = "";
    if (form.elements.smtp_host.value.trim()) {
      if (tls.value === "ssl" && port.value !== "465") { err.textContent = "SSL/TLS normally uses port 465. Use STARTTLS for ports 587 and 25."; return; }
      if (tls.value === "starttls" && port.value === "465") { err.textContent = "Port 465 needs SSL/TLS, not STARTTLS."; return; }
    }
    const v = {};
    for (const n of form.elements) {
      if (!n.name) continue;
      if (n.type === "checkbox") v[n.name] = n.checked;
      else if (n.type === "number" || n.name.endsWith("weekday") || n.name === "week_start") v[n.name] = Number(n.value);
      else if (n.name === "plaid_env" || n.name === "smtp_auth" || n.name === "registration_mode") v[n.name] = n.value;
      else if (n.type === "password") { if (n.value && !n.closest("label")?.hidden) v[n.name] = n.value; }
      else v[n.name] = n.value.trim();
    }
    try { await api("PUT", "/api/admin/settings", v); toast("Settings saved"); refresh(); } catch (x) { err.textContent = x.message; }
  };
  fill(root, form, s.smtp_pass_set || s.ntfy_token_set ? el("div", { class: "actions left" },
    s.smtp_pass_set ? el("button", { type: "button", class: "danger", onclick: () => api("PUT", "/api/admin/settings", { smtp_pass: "" }).then(refresh) }, "Clear saved SMTP password") : null,
    s.ntfy_token_set ? el("button", { type: "button", class: "danger", onclick: () => api("PUT", "/api/admin/settings", { ntfy_token: "" }).then(refresh) }, "Clear saved ntfy token") : null) : null,
    s.plaid_secret_set ? el("button", { type: "button", class: "danger", onclick: () => api("PUT", "/api/admin/settings", { plaid_secret: "" }).then(refresh) }, "Clear saved Plaid secret") : null);
}

// ---------- audit ----------
async function audit(root) {
  const rows = await api("GET", `/api/admin/audit?limit=200&q=${encodeURIComponent(auditQ)}`);
  const q = el("input", { type: "search", value: auditQ, placeholder: "Filter by user, action, IP…", maxlength: 60 });
  q.addEventListener("change", () => { auditQ = q.value; audit(root); });
  fill(root, q, el("div", { class: "scroll-x" }, el("table", { class: "tbl small" },
    el("tr", {}, el("th", {}, "When"), el("th", {}, "User"), el("th", {}, "Action"), el("th", {}, "Target"), el("th", {}, "IP")),
    rows.map((r) => el("tr", { class: /fail|lock/.test(r.action) ? "warn-row" : "" }, el("td", {}, fmtTs(r.ts)), el("td", {}, r.username || "—"),
      el("td", {}, r.action, r.detail ? el("div", { class: "meta" }, r.detail) : null), el("td", {}, r.target), el("td", {}, r.ip))))),
  rows.length === 200 ? el("p", { class: "muted" }, "Showing the latest 200. Use the filter to narrow down.") : null);
}

// ---------- tokens ----------
async function tokens(root) {
  const list = await api("GET", "/api/admin/tokens");
  fill(root, el("p", { class: "muted" }, "Every active API token in the system. Revoke anything you don't recognise."),
    list.length ? el("ul", { class: "list" }, list.map((t) => el("li", {},
      el("span", {}, el("span", {}, `${t.username}: ${t.name}`, el("span", { class: "tag" }, t.scope)),
        el("div", { class: "meta" }, `${t.prefix}… · last used ${fmtTs(t.last_used_at)} · ${t.expires_at ? "expires " + fmtTs(t.expires_at) : "no expiry"}`)),
      el("button", { type: "button", class: "danger", onclick: () => api("DELETE", `/api/admin/tokens/${t.id}`).then(refresh) }, "Revoke")))) : el("p", {}, "No API tokens."));
}

// ---------- backup / system ----------
const fmtBytes = (b) => b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(0, Math.round(b / 1024))} KB`;
const OPS = [
  ["compact", "Compact & defragment", "Rebuilds the database file so it's stored contiguously and reclaims space left by deleted data.", true],
  ["integrity", "Check integrity", "Verifies every page, the encryption authentication codes and links between records.", false],
  ["optimize", "Optimize", "Refreshes the statistics the database uses to plan fast queries.", false],
  ["checkpoint", "Flush write-ahead log", "Moves recent changes from the side log into the main file and shrinks the log.", false],
  ["prune", "Clean up old data", "Removes read alerts older than 30 days, expired sign-ins, used reset links and old invites.", false],
];

function lastLine(op, r) {
  if (!r) return "Never run";
  const when = fmtTs(r.at);
  if (op === "compact") return `${when} · saved ${fmtBytes(r.saved || 0)} (${fmtBytes(r.before)} → ${fmtBytes(r.after)})`;
  if (op === "integrity") return `${when} · ${r.ok ? "✓ no problems" : `⚠ problems found: ${(r.integrity || []).slice(0, 2).join("; ")}`}`;
  if (op === "prune") return `${when} · removed ${Object.entries(r).filter(([k, v]) => typeof v === "number" && k !== "seconds").reduce((a, [, v]) => a + v, 0)} rows`;
  if (op === "checkpoint") return `${when} · ${r.ok ? "done" : "busy, try again"}`;
  return `${when} · done in ${r.seconds}s`;
}

async function system(root) {
  const m = await api("GET", "/api/admin/system");
  const usedPct = m.disk_total ? (m.disk_used / m.disk_total) * 100 : 0;
  const btPct = m.disk_total ? ((m.db_bytes + m.wal_bytes + m.backups_bytes) / m.disk_total) * 100 : 0;

  const storage = el("section", { class: "card" }, el("h2", {}, "Storage"),
    el("div", { class: "kpis" },
      el("div", {}, el("small", {}, "Database"), el("strong", {}, fmtBytes(m.db_bytes + m.wal_bytes)),
        el("div", { class: "sub" }, m.wal_bytes ? `incl. ${fmtBytes(m.wal_bytes)} write-ahead log` : `${m.pages} pages × ${m.page_size} B`)),
      el("div", {}, el("small", {}, "Reclaimable"), el("strong", { class: m.free_bytes > 1e6 ? "out" : "" }, fmtBytes(m.free_bytes)),
        el("div", { class: "sub" }, "freed by Compact")),
      el("div", {}, el("small", {}, "Backups on disk"), el("strong", {}, fmtBytes(m.backups_bytes)), el("div", { class: "sub" }, `${m.backups} files`)),
      el("div", {}, el("small", {}, "Disk free"), el("strong", { class: m.disk_free < 1e9 ? "out" : "in" }, fmtBytes(m.disk_free)),
        el("div", { class: "sub" }, `of ${fmtBytes(m.disk_total)}`))),
    el("div", { class: "disk", role: "meter", "aria-label": "Disk usage", "aria-valuenow": Math.round(usedPct), "aria-valuemin": 0, "aria-valuemax": 100 },
      el("i", { class: "used", style: `width:${usedPct}%` }), el("i", { class: "bt", style: `width:${Math.max(btPct, 0.5)}%` })),
    el("div", { class: "legend" }, el("span", {}, `■ Disk used ${usedPct.toFixed(0)}%`), "  ",
      el("span", { class: "in" }, `■ BillTrack ${fmtBytes(m.db_bytes + m.wal_bytes + m.backups_bytes)}`), "  ",
      el("span", { class: "muted" }, m.db_path)));

  const maint = el("section", { class: "card" }, el("h2", {}, "Maintenance"),
    el("ul", { class: "list maint" }, OPS.map(([op, label, desc, confirmIt]) => {
      const btn = el("button", { type: "button", onclick: async () => {
        if (confirmIt && !(await confirmBox(label, `${desc} Takes a few seconds; the app may pause briefly. Run it now?`, "Run", false))) return;
        btn.disabled = true; btn.textContent = "Running…";
        try { await api("POST", `/api/admin/maintenance/${op}`); toast(`${label}: done`); } catch (e) { toast(e.message, true); }
        refresh();
      } }, "Run");
      return el("li", {}, el("span", {}, el("strong", {}, label), el("div", { class: "meta" }, desc),
        el("div", { class: "meta" }, lastLine(op, m.last[op]))), btn);
    })));

  const list = await api("GET", "/api/admin/backups");
  const bkMenu = (b) => [
    { label: "Download as DR backup…", action: () => downloadStored(b) },
    { label: "Restore this backup…", action: () => restoreStored(b) },
    "-", { label: "Delete", danger: true, action: async () => {
      if (await confirmBox("Delete backup?", `${b.name} (${fmtBytes(b.bytes)}) will be removed from the server.`, "Delete"))
        api("DELETE", `/api/admin/backups/${encodeURIComponent(b.name)}`).then(() => { toast("Deleted"); refresh(); }).catch((e) => toast(e.message, true));
    } },
  ];
  const rows = list.map((b) => {
    const tr = el("tr", { class: "click", tabindex: 0, onclick: (e) => { const r = e.currentTarget.getBoundingClientRect(); showMenu(Math.min(e.clientX || r.left, innerWidth - 240), r.bottom, bkMenu(b)); } },
      el("td", {}, fmtTs(b.created)), el("td", {}, el("span", { class: `tag ${b.kind === "Before restore" ? "due-soon" : ""}` }, b.kind)),
      el("td", { class: "r" }, fmtBytes(b.bytes)), el("td", { class: "r" }, "⋯"));
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") tr.click(); });
    onContext(tr, () => bkMenu(b));
    return tr;
  });
  const backupsCard = el("section", { class: "card" },
    el("div", { class: "spread" }, el("h2", {}, "Backups on this server"),
      el("div", { class: "actions left" },
        el("button", { type: "button", class: "primary", onclick: async (e) => {
          e.currentTarget.disabled = true;
          try { const r = await api("POST", "/api/admin/backups", {}); toast(`Backed up${r.rotated.length ? `, rotated out ${r.rotated.length} old` : ""}`); }
          catch (x) { toast(x.message, true); }
          refresh();
        } }, "Back up now"),
        el("button", { type: "button", onclick: backup }, "Download DR backup…"))),
    el("p", { class: "muted small" }, `Stored in ${m.backup_dir}. The newest 7 of each kind are kept (upgrade, manual, before-restore). `
      + "Click a backup to download, restore or delete it."),
    list.length ? el("div", { class: "scroll-x" }, el("table", { class: "tbl" },
      el("tr", {}, el("th", {}, "Created"), el("th", {}, "Kind"), el("th", { class: "r" }, "Size"), el("th")), rows))
      : el("p", { class: "muted" }, "No backups on the server yet."));

  const enc = el("section", { class: "card" }, el("h2", {}, "Encryption"),
    m.key_mode === "passphrase"
      ? el("div", {}, el("p", {}, "✓ Database locked with a passphrase + this server's key file (both needed). ",
          el("span", { class: "muted small" }, `Data key fingerprint ${m.key_fingerprint}`)),
        el("p", { class: "muted small" }, "After a reboot BillTrack stays locked until: sudo billtrack-manage unlock · "
          + "Change passphrase: sudo billtrack-manage change-passphrase"))
      : el("p", { class: "out" }, "Key-file only. Add a passphrase (re-encrypts the data) by running sudo ./setup.sh, or: sudo billtrack-manage set-passphrase"));
  const sys = el("section", { class: "card" }, el("h2", {}, "System"),
    el("table", { class: "tbl" }, [["BillTrack", m.version], ["Database encryption", `SQLCipher ${m.cipher_version}`],
      ["Crypto library", m.cipher_provider_version],
      ["Records", Object.entries(m.rows).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · ")]]
      .map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", { class: "small" }, v)))));

  fill(root, enc, storage, maint, backupsCard, sys);
}

function passphraseDialog(title, note, onOk) {
  const pw = el("input", { type: "password", autocomplete: "new-password", minlength: 12, required: true });
  const pw2 = el("input", { type: "password", autocomplete: "new-password", required: true });
  const err = el("p", { class: "err", role: "alert" });
  const form = showDialog(title, el("div", { class: "stack" }, el("p", { class: "muted" }, note),
    el("label", {}, "Backup password (12+ characters, different from the database passphrase)", pw), el("label", {}, "Confirm", pw2), err),
  [el("button", { type: "submit", class: "primary" }, "Download")]);
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (pw.value.length < 12) { err.textContent = "At least 12 characters"; return; }
    if (pw.value !== pw2.value) { err.textContent = "Passwords don't match"; return; }
    const btn = form.querySelector("button[type=submit]"); btn.disabled = true; btn.textContent = "Encrypting…";
    try { await onOk(pw.value); closeDialog(); toast("Downloaded"); } catch (x) { err.textContent = x.message; btn.disabled = false; btn.textContent = "Download"; }
  };
}

const DR_NOTE = "A complete disaster-recovery file: database, settings, install answers and certificates. It's encrypted with the backup "
  + "password you choose here, and the data inside stays locked with the database passphrase. Restoring on any server needs BOTH. "
  + "The HTTPS CA's private key is only included when exported as root: sudo billtrack-manage dr-export <file>.";

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: name });
  document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function backup() {
  passphraseDialog("Download DR backup", DR_NOTE,
    async (pass) => saveBlob(await (await api("POST", "/api/admin/dr-backup", { password: pass }, { raw: true })).blob(), `billtrack-${S.me.today}.btdr`));
}

function downloadStored(b) {
  passphraseDialog(`Download ${b.kind.toLowerCase()} backup`, DR_NOTE,
    async (pass) => saveBlob(await (await api("POST", `/api/admin/backups/${encodeURIComponent(b.name)}/download`, { password: pass }, { raw: true })).blob(), b.name.replace(/\.db$/, ".btdr")));
}

function restoreStored(b) {
  const inp = el("input", { autocomplete: "off", placeholder: "RESTORE" });
  const err = el("p", { class: "err", role: "alert" });
  const form = showDialog("Restore this backup?", el("div", { class: "stack" },
    el("p", {}, `Everything goes back to ${fmtTs(b.created)}. Changes made since then are replaced.`),
    el("p", { class: "muted" }, "A safety copy of the current data is saved first (kind \"Before restore\"), so you can undo this. You may need to sign in again."),
    el("label", {}, "Type RESTORE to confirm", inp), err),
  [el("button", { type: "submit", class: "danger-solid" }, "Restore")]);
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (inp.value !== "RESTORE") { err.textContent = "Type RESTORE in capitals"; return; }
    form.querySelector("button[type=submit]").disabled = true;
    try { const r = await api("POST", `/api/admin/backups/${encodeURIComponent(b.name)}/restore`, { confirm: "RESTORE" });
      closeDialog(); toast(`Restored. Safety copy: ${r.safety}`); setTimeout(() => location.reload(), 1200);
    } catch (x) { err.textContent = x.message; form.querySelector("button[type=submit]").disabled = false; }
  };
}
