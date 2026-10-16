import { api, closeDialog, confirmBox, el, fmtTs, money, onContext, openForm, refresh, S, showDialog, toast, fill } from "../core.js";

export const title = "Admin";
let tab = "users", auditQ = "";
const TABS = [["users", "Users"], ["invites", "Invites"], ["banks", "Bank links"], ["settings", "Settings"], ["audit", "Audit log"], ["tokens", "API tokens"], ["system", "Backup & system"]];
const PERM_LABEL = { "finance:write": "Edit bills & income", "trackers:read": "See trackers", "trackers:write": "Edit trackers",
  "investments:read": "See investments", "investments:write": "Edit investments", reminders: "Use reminders" };
const ROLE_HELP = { admin: "Everything, including this portal", member: "Use and edit everything except admin", viewer: "Look but not change" };

export async function render(root) {
  try { const t = sessionStorage.getItem("bt-admin-tab"); if (t) { tab = t; sessionStorage.removeItem("bt-admin-tab"); } } catch { /* ignore */ }
  const body = el("div");
  fill(root, el("div", { class: "seg", role: "tablist" }, TABS.map(([k, l]) =>
    el("button", { type: "button", role: "tab", "aria-selected": String(tab === k), onclick: () => { tab = k; render(root); } }, l))), body);
  await ({ users, invites, banks, settings, audit, tokens, system })[tab](body);
}

// ---------- users ----------
async function users(root) {
  const [list, meta] = await Promise.all([api("GET", "/api/admin/users"), api("GET", "/api/admin/meta")]);
  fill(root, 
    el("div", { class: "spread" }, el("p", { class: "muted" }, "Roles: " + Object.entries(ROLE_HELP).map(([k, v]) => `${k}: ${v}`).join(" · ")),
      el("button", { type: "button", class: "primary", onclick: () => userCreate() }, "Add user")),
    el("div", { class: "scroll-x" }, el("table", { class: "tbl" },
      el("tr", {}, el("th", {}, "User"), el("th", {}, "Role"), el("th", {}, "2FA"), el("th", {}, "Status"), el("th", {}, "Last active")),
      list.map((u) => {
        const locked = u.locked_until && new Date(u.locked_until) > new Date();
        const tr = el("tr", { class: "click", tabindex: 0, onclick: () => userEdit(u, meta) },
          el("td", {}, el("strong", {}, u.username), el("div", { class: "meta" }, [u.display_name, u.email].filter(Boolean).join(" · "))),
          el("td", {}, u.role, u.perms_deny.length ? el("div", { class: "meta" }, `${u.perms_deny.length} restricted`) : null),
          el("td", {}, u.totp_enabled ? "On" : "Off"),
          el("td", {}, u.disabled ? el("span", { class: "tag overdue" }, "Disabled") : locked ? el("span", { class: "tag due-soon" }, "Locked") : "Active"),
          el("td", { class: "small" }, fmtTs(u.last_seen)));
        onContext(tr, () => userMenu(u, meta, locked));
        return tr;
      }))));
}

function userMenu(u, meta, locked) {
  const self = u.id === S.me.id;
  const act = (path, msg, confirmText) => async () => {
    if (confirmText && !(await confirmBox("Are you sure?", confirmText, "Yes"))) return;
    try { await api("POST", `/api/admin/users/${u.id}/${path}`); toast(msg); refresh(); } catch (e) { toast(e.message, true); }
  };
  return [
    { label: "Edit role and permissions…", action: () => userEdit(u, meta) },
    { label: "Set a new password…", action: () => setPw(u) },
    { label: "Email a password reset link", disabled: !u.email, action: act("send-reset", "Reset link sent") },
    locked ? { label: "Unlock", action: act("unlock", "Unlocked") } : null,
    u.totp_enabled ? { label: "Reset two-factor", action: act("reset-2fa", "Two-factor reset", `${u.username} will sign in with just a password and must set up 2FA again.`) } : null,
    { label: "Sign out everywhere", action: act("revoke-sessions", "Signed out") },
    "-",
    { label: "Delete user", danger: true, disabled: self, action: async () => {
      if (await confirmBox(`Delete ${u.username}?`, "Their login, sessions, tokens and reminders are removed. Shared financial data stays.", "Delete")) {
        api("DELETE", `/api/admin/users/${u.id}`).then(() => { toast("Deleted"); refresh(); }).catch((e) => toast(e.message, true));
      }
    } },
  ];
}

function userCreate() {
  openForm("Add user", [
    { name: "username", label: "Username", required: true, max: 32, pattern: "[A-Za-z0-9_.\\-]{3,32}", autocomplete: "off" },
    { name: "display_name", label: "Display name", max: 60 },
    { name: "email", label: "Email", type: "email", max: 254 },
    { name: "role", label: "Role", type: "select", options: [["member", "Member"], ["viewer", "Viewer"], ["admin", "Admin"]], value: "member" },
    { name: "password", label: "Temporary password (12+)", type: "password", required: true, autocomplete: "new-password",
      hint: "Tell them to change it in Settings. Or use Invites to let them pick their own." },
  ], (d) => api("POST", "/api/admin/users", d));
}

function userEdit(u, meta) {
  const roleSel = el("select", { name: "role" }, ["admin", "member", "viewer"].map((r) => el("option", { value: r, selected: u.role === r }, r)));
  const boxes = meta.denyable.map((p) => el("label", { class: "check" },
    el("input", { type: "checkbox", name: p, checked: !u.perms_deny.includes(p) }), PERM_LABEL[p] || p));
  const fs = el("fieldset", {}, el("legend", {}, "Allowed (members and viewers)"), boxes);
  const sync = () => { fs.disabled = roleSel.value === "admin"; };
  roleSel.addEventListener("change", sync); sync();
  const name = el("input", { value: u.display_name, maxlength: 60 }), email = el("input", { type: "email", value: u.email, maxlength: 254 });
  const disabled = el("input", { type: "checkbox", checked: !!u.disabled });
  const err = el("p", { class: "err", role: "alert" });
  const form = showDialog(`User: ${u.username}`, el("div", { class: "stack" },
    el("label", {}, "Display name", name), el("label", {}, "Email", email), el("label", {}, "Role", roleSel),
    el("p", { class: "hint" }, "Viewers can never edit, whatever is ticked below."), fs,
    el("label", { class: "check" }, disabled, "Disabled (blocks sign-in and API tokens)"), err),
  [el("button", { type: "submit", class: "primary" }, "Save")]);
  form.onsubmit = async (e) => {
    e.preventDefault();
    const deny = meta.denyable.filter((p) => !form.elements[p].checked);
    try {
      await api("PUT", `/api/admin/users/${u.id}`, { display_name: name.value, email: email.value.trim(), role: roleSel.value,
        disabled: disabled.checked, perms_deny: deny });
      closeDialog(); toast("User saved"); refresh();
    } catch (x) { err.textContent = x.message; }
  };
}

function setPw(u) {
  openForm(`New password for ${u.username}`, [
    { name: "password", label: "Password (12+ characters)", type: "password", required: true, autocomplete: "new-password",
      hint: "Signs them out everywhere." },
  ], (d) => api("POST", `/api/admin/users/${u.id}/password`, d));
}

// ---------- invites ----------
async function invites(root) {
  const list = await api("GET", "/api/admin/invites");
  const status = (i) => i.used_at ? "Accepted" : new Date(i.expires_at) < new Date() ? "Expired" : "Pending";
  fill(root, 
    el("div", { class: "spread" }, el("p", { class: "muted" }, "Invites email a one-time link (valid 3 days) so people choose their own username and password."),
      el("button", { type: "button", class: "primary", onclick: inviteForm }, "Invite someone")),
    el("ul", { class: "list" }, list.map((i) => el("li", {},
      el("span", {}, el("span", {}, i.email, el("span", { class: "tag" }, i.role)),
        el("div", { class: "meta" }, `${status(i)} · sent ${fmtTs(i.created_at)}`)),
      status(i) === "Pending" ? el("button", { type: "button", class: "danger", onclick: () => api("DELETE", `/api/admin/invites/${i.id}`).then(refresh) }, "Revoke") : null))));
}

function inviteForm() {
  openForm("Invite someone", [
    { name: "email", label: "Email", type: "email", required: true, max: 254 },
    { name: "role", label: "Role", type: "select", options: [["member", "Member"], ["viewer", "Viewer"], ["admin", "Admin"]], value: "member" },
    { name: "send", label: "Email the link now", type: "check", value: S.me.email_ready },
  ], async (d) => {
    const r = await api("POST", "/api/admin/invites", d);
    setTimeout(() => showDialog(r.emailed ? "Invite sent" : "Invite link", el("div", { class: "stack" },
      el("p", {}, r.emailed ? "They'll get an email. You can also send this link yourself:" : "Send this link to them (works once, 3 days):"),
      el("code", { class: "secret" }, r.link),
      el("button", { type: "button", onclick: () => navigator.clipboard?.writeText(r.link).then(() => toast("Copied")) }, "Copy link"))), 50);
  }, null, "Create invite");
}

// ---------- bank links (Plaid) ----------
const STATUS = { ok: ["Connected", ""], login_required: ["Sign-in needed", "overdue"], error: ["Sync error", "due-soon"] };
async function banks(root) {
  const [st, accts, invs] = await Promise.all([api("GET", "/api/plaid/status"), api("GET", "/api/accounts"), api("GET", "/api/investments/accounts")]);
  if (!st.configured) {
    fill(root, el("section", { class: "card" }, el("h2", {}, "Connect your banks with Plaid"),
      el("ol", {}, el("li", {}, "Go to dashboard.plaid.com and sign up. Choose \"Personal use\" (free trial, up to 10 bank connections)."),
        el("li", {}, "In the Plaid dashboard open Developers → Keys and copy the client ID and the Production secret."),
        el("li", {}, "Paste them in the Settings tab here under Bank sync (Plaid), then come back to this tab.")),
      el("button", { type: "button", class: "primary", onclick: () => { tab = "settings"; refresh(); } }, "Open Settings")));
    return;
  }
  const opts = (pa) => {
    const isInv = pa.type === "investment" || pa.type === "brokerage";
    const cur = pa.map_kind === "none" ? "none" : `${pa.map_kind}:${pa.map_id}`;
    const list = [["none", "Don't use"]];
    if (isInv) { list.push(["investment:new", "＋ New investment account"]); invs.forEach((a) => list.push([`investment:${a.id}`, `Investment: ${a.name}`])); }
    else { list.push(["account:new", "＋ New BillTrack account"]); accts.forEach((a) => list.push([`account:${a.id}`, `${a.name} (${a.kind})`])); }
    const sel = el("select", { "aria-label": `Use ${pa.name} as` }, list.map(([v, l]) => el("option", { value: v, selected: v === cur }, l)));
    sel.addEventListener("change", async () => {
      const [kind, id] = sel.value.split(":");
      try {
        await api("PUT", `/api/plaid/accounts/${pa.id}`, { map_kind: kind, map_id: id && id !== "new" ? Number(id) : null, create: id === "new" });
        toast("Saved"); refresh();
      } catch (e) { toast(e.message, true); refresh(); }
    });
    return sel;
  };
  const items = st.items.map((it) => {
    const [label, cls] = STATUS[it.status] || [it.status, ""];
    const since = el("input", { type: "date", value: it.import_since });
    since.addEventListener("change", () => api("PUT", `/api/plaid/items/${it.id}`, { import_since: since.value }).then(() => toast("Saved")).catch((e) => toast(e.message, true)));
    return el("section", { class: "card" },
      el("div", { class: "spread" }, el("h2", {}, it.institution_name, el("span", { class: `tag ${cls}` }, label)),
        el("span", { class: "muted small" }, `Last sync ${fmtTs(it.last_sync)}`)),
      it.error ? el("p", { class: it.status === "login_required" ? "out" : "muted small" },
        it.status === "login_required" ? "Your bank needs you to sign in again. Click Reconnect." : it.error) : null,
      el("div", { class: "scroll-x" }, el("table", { class: "tbl" },
        el("tr", {}, el("th", {}, "Bank account"), el("th", { class: "r" }, "Balance"), el("th", {}, "Use in BillTrack as"), el("th", {}, "Imported")),
        it.accounts.map((pa) => el("tr", {},
          el("td", {}, pa.name, el("div", { class: "meta" }, [pa.subtype || pa.type, pa.mask ? `••${pa.mask}` : ""].filter(Boolean).join(" · "))),
          el("td", { class: "r" }, pa.current_cents === null ? "—" : money(pa.current_cents)),
          el("td", {}, opts(pa)),
          el("td", { class: "small" }, pa.stats?.n ? `${pa.stats.expenses || 0} spending · ${pa.stats.bills || 0} bills paid · ${pa.stats.pays || 0} paychecks` : "—"))))),
      el("label", {}, "Import spending and paychecks from", since),
      el("div", { class: "actions left" },
        el("button", { type: "button", class: "primary", onclick: async (e) => {
          e.currentTarget.disabled = true;
          try { const r = await api("POST", `/api/plaid/sync?item=${it.id}`); const s = Object.values(r)[0] || {};
            toast(s.error ? `Sync problem: ${s.error}` : `Synced: ${s.added || 0} new, ${s.bills_paid || 0} bills paid, ${s.pay_received || 0} paychecks`, !!s.error);
          } catch (x) { toast(x.message, true); }
          refresh();
        } }, "Sync now"),
        el("button", { type: "button", onclick: () => { location.href = `/connect.html?item=${it.id}`; } }, "Reconnect"),
        el("button", { type: "button", class: "danger", onclick: async () => {
          if (await confirmBox(`Disconnect ${it.institution_name}?`, "Plaid access is revoked and the tokens are deleted. Spending already imported stays.", "Disconnect")) {
            api("DELETE", `/api/plaid/items/${it.id}`).then(() => { toast("Disconnected"); refresh(); }).catch((x) => toast(x.message, true));
          }
        } }, "Disconnect")));
  });
  fill(root,
    el("div", { class: "spread" }, el("p", { class: "muted" }, `Plaid ${st.env}. Balances and transactions sync automatically; matching charges mark bills paid and deposits mark paychecks received.`),
      el("button", { type: "button", class: "primary", onclick: () => { location.href = "/connect.html"; } }, "Connect a bank")),
    items.length ? items : el("div", { class: "empty-state" }, el("p", {}, "No banks connected yet.")),
    el("p", { class: "hint" }, "Read-only. Your bank password goes to your bank (or Plaid), never to BillTrack. Access tokens are stored encrypted."));
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
    el("fieldset", {}, el("legend", {}, "General"),
      f("base_url", "Public URL", "url", { placeholder: "https://bills.example.com", hint: "Used in emails for reset and invite links." }),
      f("timezone", "Time zone", "text", { placeholder: "America/Chicago" }),
      sel("week_start", "Week starts on (match the pay week)", days)),
    el("fieldset", {}, el("legend", {}, "Security"),
      f("require_2fa", "Require two-factor for everyone", "checkbox"),
      f("session_days", "Stay signed in for (days)", "number", { min: 1, max: 90 }),
      f("idle_days", "Sign out after idle (days)", "number", { min: 1, max: 90 })),
    el("fieldset", {}, el("legend", {}, "Weekly summary"),
      sel("digest_weekday", "Send on", days), f("digest_hour", "Hour (0-23)", "number", { min: 0, max: 23 })),
    el("fieldset", {}, el("legend", {}, "Email (SMTP, TLS required)"),
      f("smtp_host", "Server", "text", { placeholder: "smtp.gmail.com" }),
      el("div", { class: "row2" }, f("smtp_port", "Port", "number", { min: 1, max: 65535 }),
        sel("smtp_tls", "Security", [["starttls", "STARTTLS (587)"], ["ssl", "SSL/TLS (465)"]])),
      f("smtp_user", "Username"), f("smtp_pass", "Password", "password", { placeholder: s.smtp_pass_set ? "•••••••• (saved, leave blank to keep)" : "",
        hint: "Stored encrypted. Never shown again." }),
      f("smtp_from", "From address", "email")),
    el("fieldset", {}, el("legend", {}, "Bank sync (Plaid)"),
      el("p", { class: "hint" }, "Free personal-use keys: dashboard.plaid.com → sign up → Personal use → Developers → Keys."),
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
  form.onsubmit = async (e) => {
    e.preventDefault(); err.textContent = "";
    const v = {};
    for (const n of form.elements) {
      if (!n.name) continue;
      if (n.type === "checkbox") v[n.name] = n.checked;
      else if (n.type === "number" || n.name.endsWith("weekday") || n.name === "week_start") v[n.name] = Number(n.value);
      else if (n.name === "plaid_env") v[n.name] = n.value;
      else if (n.type === "password") { if (n.value) v[n.name] = n.value; }
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
async function system(root) {
  const m = await api("GET", "/api/admin/meta");
  fill(root, 
    el("section", { class: "card" }, el("h2", {}, "Encrypted backup"),
      el("p", { class: "muted" }, "Downloads the whole database, locked with a passphrase you choose. Restore with billtrack-manage restore."),
      el("button", { type: "button", class: "primary", onclick: backup }, "Download encrypted backup")),
    el("section", { class: "card" }, el("h2", {}, "System"),
      el("table", { class: "tbl" }, [["BillTrack", m.version], ["Database encryption", `SQLCipher ${m.cipher_version}`], ["Crypto library", m.cipher_provider_version]]
        .map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, v))))));
}

function backup() {
  const pw = el("input", { type: "password", autocomplete: "new-password", minlength: 12, required: true });
  const pw2 = el("input", { type: "password", autocomplete: "new-password", required: true });
  const err = el("p", { class: "err", role: "alert" });
  const form = showDialog("Encrypted backup", el("div", { class: "stack" },
    el("p", { class: "muted" }, "You'll need this passphrase to restore. It isn't stored anywhere."),
    el("label", {}, "Passphrase (12+ characters)", pw), el("label", {}, "Confirm", pw2), err),
  [el("button", { type: "submit", class: "primary" }, "Download")]);
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (pw.value.length < 12) { err.textContent = "At least 12 characters"; return; }
    if (pw.value !== pw2.value) { err.textContent = "Passphrases don't match"; return; }
    try {
      const r = await api("POST", "/api/admin/backup", { passphrase: pw.value }, { raw: true });
      const url = URL.createObjectURL(await r.blob());
      const a = el("a", { href: url, download: `billtrack-backup-${S.me.today}.db` });
      document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
      closeDialog(); toast("Backup downloaded");
    } catch (x) { err.textContent = x.message; }
  };
}
