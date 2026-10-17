// Sign-in screens: password, second factor, forgot password, reset from email link, invite acceptance.
import { $, api, el, fill } from "./core.js";

let onSignedIn = () => {};
const card = (...kids) => { fill($("#auth"), el("div", { class: "auth-card" }, el("div", { class: "brand" }, el("img", { src: "/icon.svg", alt: "" }), "BillTrack"), ...kids)); };
const err = () => el("p", { class: "err", role: "alert" });
const busy = async (btn, fn) => { btn.disabled = true; try { await fn(); } finally { btn.disabled = false; } };

export async function showAuth(cb) {
  if (cb) onSignedIn = cb;
  $("#app").hidden = true; $("#auth").hidden = false;
  try {
    const h = await (await fetch("/healthz", { cache: "no-store" })).json();
    if (h.locked) {
      fill($("#auth"), el("div", { class: "auth-card" }, el("div", { class: "brand" }, el("img", { src: "/icon.svg", alt: "" }), "BillTrack"),
        el("h1", {}, "🔒 Locked"), el("p", {}, "The server was restarted and its data is encrypted. An admin needs to unlock it on the server:"),
        el("code", { class: "secret" }, "sudo billtrack-manage unlock"),
        el("p", { class: "muted" }, "This page refreshes by itself once it's unlocked.")));
      setTimeout(() => showAuth(), 5000);
      return;
    }
  } catch { /* fall through to sign-in */ }
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get("reset")) return resetScreen(hash.get("reset"));
  if (hash.has("register")) return registerScreen(hash.get("register") || "");
  let msg;
  try { msg = sessionStorage.getItem("bt-msg"); sessionStorage.removeItem("bt-msg"); } catch { /* ignore */ }
  loginScreen(msg || undefined);
}

function clearHash() { history.replaceState(null, "", location.pathname); }

function loginScreen(msg) {
  const e = err(), user = el("input", { name: "username", autocomplete: "username", required: true, maxlength: 64, autocapitalize: "none" });
  const pw = el("input", { name: "password", type: "password", autocomplete: "current-password", required: true, maxlength: 256 });
  const btn = el("button", { type: "submit", class: "primary wide" }, "Sign in");
  const f = el("form", { class: "stack" }, el("h1", {}, "Sign in"), msg ? el("p", { class: "ok" }, msg) : null,
    el("label", {}, "Username", user), el("label", {}, "Password", pw), e, btn,
    el("div", { class: "spread" },
      el("button", { type: "button", class: "link", onclick: forgotScreen }, "Forgot password?"),
      el("button", { type: "button", class: "link", id: "toRegister", hidden: true, onclick: () => registerScreen("") }, "Create an account")));
  api("GET", "/api/register/info").then((i) => { const b = document.getElementById("toRegister"); if (b) b.hidden = i.mode === "closed"; }).catch(() => {});
  f.onsubmit = (ev) => { ev.preventDefault(); e.textContent = ""; busy(btn, async () => {
    try {
      const r = await api("POST", "/api/login", { username: user.value.trim(), password: pw.value });
      if (r.mfa_required) return mfaScreen(r.mfa_token);
      pw.value = ""; onSignedIn();
    } catch (x) { e.textContent = x.message; pw.select(); }
  }); };
  card(f); user.focus();
}

function mfaScreen(token) {
  const e = err(), code = el("input", { inputmode: "numeric", autocomplete: "one-time-code", required: true, maxlength: 12 });
  const btn = el("button", { type: "submit", class: "primary wide" }, "Verify");
  const f = el("form", { class: "stack" }, el("h1", {}, "Two-factor code"),
    el("p", { class: "muted" }, "Enter the 6-digit code from your authenticator app, or one of your recovery codes."),
    el("label", {}, "Code", code), e, btn, el("button", { type: "button", class: "link", onclick: () => loginScreen() }, "Start over"));
  f.onsubmit = (ev) => { ev.preventDefault(); e.textContent = ""; busy(btn, async () => {
    try { await api("POST", "/api/login/mfa", { mfa_token: token, code: code.value.trim() }); onSignedIn(); }
    catch (x) { e.textContent = x.message; code.select(); if (/expired/.test(x.message)) setTimeout(() => loginScreen(), 1500); }
  }); };
  card(f); code.focus();
}

function forgotScreen() {
  const e = err(), email = el("input", { type: "email", autocomplete: "email", required: true, maxlength: 254 });
  const btn = el("button", { type: "submit", class: "primary wide" }, "Email me a reset link");
  const f = el("form", { class: "stack" }, el("h1", {}, "Reset password"),
    el("p", { class: "muted" }, "Enter the email on your account. If it matches, you'll get a link that works for 30 minutes."),
    el("label", {}, "Email", email), e, btn, el("button", { type: "button", class: "link", onclick: () => loginScreen() }, "Back to sign in"));
  f.onsubmit = (ev) => { ev.preventDefault(); busy(btn, async () => {
    try { const r = await api("POST", "/api/password/forgot", { email: email.value.trim() }); loginScreen(r.message); }
    catch (x) { e.textContent = x.message; }
  }); };
  card(f); email.focus();
}

function newPwFields() {
  const a = el("input", { type: "password", autocomplete: "new-password", required: true, minlength: 12, maxlength: 256 });
  const b = el("input", { type: "password", autocomplete: "new-password", required: true, maxlength: 256 });
  return [a, b, [el("label", {}, "New password (12+ characters)", a), el("label", {}, "Confirm password", b)]];
}

function resetScreen(token) {
  const e = err(), [a, b, fields] = newPwFields(), btn = el("button", { type: "submit", class: "primary wide" }, "Set password");
  const f = el("form", { class: "stack" }, el("h1", {}, "Choose a new password"), fields, e, btn);
  f.onsubmit = (ev) => { ev.preventDefault(); e.textContent = "";
    if (a.value !== b.value) { e.textContent = "Passwords don't match"; return; }
    busy(btn, async () => {
      try { await api("POST", "/api/password/reset", { token, password: a.value }); clearHash(); loginScreen("Password changed. Sign in with your new password."); }
      catch (x) { e.textContent = x.message; }
    });
  };
  card(f); a.focus();
}

async function registerScreen(prefill) {
  let mode = "approval";
  try { mode = (await api("GET", "/api/register/info")).mode; } catch { /* ignore */ }
  if (mode === "closed") { clearHash(); return loginScreen("Sign-ups are closed. Ask an admin for an account."); }
  const e = err(), [a, b, fields] = newPwFields(), btn = el("button", { type: "submit", class: "primary wide" }, "Create account");
  const user = el("input", { required: true, minlength: 3, maxlength: 32, pattern: "[A-Za-z0-9_.\\-]+", autocomplete: "username", autocapitalize: "none" });
  const name = el("input", { maxlength: 60, autocomplete: "name" });
  const email = el("input", { type: "email", required: true, maxlength: 254, autocomplete: "email" });
  const has = el("input", { type: "checkbox", checked: !!prefill || mode === "invite_only", disabled: mode === "invite_only" });
  const code = el("input", { value: prefill, maxlength: 40, autocomplete: "off", autocapitalize: "characters", placeholder: "BT-XXXX-XXXX-XXXX-XXXX" });
  const codeRow = el("label", {}, "Invite code", code);
  const sync = () => { codeRow.hidden = !has.checked; code.required = has.checked; };
  has.addEventListener("change", sync); sync();
  const f = el("form", { class: "stack" }, el("h1", {}, "Create your account"),
    el("p", { class: "muted" }, mode === "invite_only" ? "You need an invite code from an admin."
      : "An admin approves new accounts. With an invite code you can start right away."),
    el("label", {}, "Username (letters, numbers, . _ -)", user), el("label", {}, "Your name", name),
    el("label", {}, "Email (for approval, alerts and password resets)", email), fields,
    el("label", { class: "check" }, has, "I have an invite code"), codeRow, e, btn,
    el("button", { type: "button", class: "link", onclick: () => { clearHash(); loginScreen(); } }, "I already have an account"));
  f.onsubmit = (ev) => { ev.preventDefault(); e.textContent = "";
    if (a.value !== b.value) { e.textContent = "Passwords don't match"; return; }
    busy(btn, async () => {
      try {
        const r = await api("POST", "/api/register", { username: user.value.trim(), display_name: name.value.trim(), email: email.value.trim(),
          password: a.value, invite_code: has.checked ? code.value.trim() : "" });
        clearHash();
        if (r.status === "active") return onSignedIn();
        fill($("#auth"), el("div", { class: "auth-card" }, el("div", { class: "brand" }, el("img", { src: "/icon.svg", alt: "" }), "BillTrack"),
          el("h1", {}, "Thanks! ✉️"), el("p", {}, "Your account is waiting for an admin to approve it. You'll get an email at "),
          el("p", {}, el("strong", {}, email.value.trim())), el("p", { class: "muted" }, "Then sign in with the username and password you just chose."),
          el("button", { type: "button", class: "primary wide", onclick: () => loginScreen() }, "Back to sign in")));
      } catch (x) { e.textContent = x.message; }
    });
  };
  card(f); user.focus();
}
