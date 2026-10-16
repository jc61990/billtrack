// Sign-in screens: password, second factor, forgot password, reset from email link, invite acceptance.
import { $, api, el, fill } from "./core.js";

let onSignedIn = () => {};
const card = (...kids) => { fill($("#auth"), el("div", { class: "auth-card" }, el("div", { class: "brand" }, el("img", { src: "/icon.svg", alt: "" }), "BillTrack"), ...kids)); };
const err = () => el("p", { class: "err", role: "alert" });
const busy = async (btn, fn) => { btn.disabled = true; try { await fn(); } finally { btn.disabled = false; } };

export function showAuth(cb) {
  if (cb) onSignedIn = cb;
  $("#app").hidden = true; $("#auth").hidden = false;
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get("reset")) return resetScreen(hash.get("reset"));
  if (hash.get("invite")) return inviteScreen(hash.get("invite"));
  loginScreen();
}

function clearHash() { history.replaceState(null, "", location.pathname); }

function loginScreen(msg) {
  const e = err(), user = el("input", { name: "username", autocomplete: "username", required: true, maxlength: 64, autocapitalize: "none" });
  const pw = el("input", { name: "password", type: "password", autocomplete: "current-password", required: true, maxlength: 256 });
  const btn = el("button", { type: "submit", class: "primary wide" }, "Sign in");
  const f = el("form", { class: "stack" }, el("h1", {}, "Sign in"), msg ? el("p", { class: "ok" }, msg) : null,
    el("label", {}, "Username", user), el("label", {}, "Password", pw), e, btn,
    el("button", { type: "button", class: "link", onclick: forgotScreen }, "Forgot password?"));
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

async function inviteScreen(token) {
  let info;
  try { info = await api("GET", `/api/invite/info?token=${encodeURIComponent(token)}`); }
  catch (x) { clearHash(); return loginScreen(); }
  const e = err(), [a, b, fields] = newPwFields(), btn = el("button", { type: "submit", class: "primary wide" }, "Create account");
  const user = el("input", { required: true, minlength: 3, maxlength: 32, pattern: "[A-Za-z0-9_.\\-]+", autocomplete: "username", autocapitalize: "none" });
  const name = el("input", { maxlength: 60, autocomplete: "name" });
  const f = el("form", { class: "stack" }, el("h1", {}, "Create your account"),
    el("p", { class: "muted" }, `Invited as ${info.role} (${info.email}).`),
    el("label", {}, "Username (letters, numbers, . _ -)", user), el("label", {}, "Your name", name), fields, e, btn);
  f.onsubmit = (ev) => { ev.preventDefault(); e.textContent = "";
    if (a.value !== b.value) { e.textContent = "Passwords don't match"; return; }
    busy(btn, async () => {
      try { await api("POST", "/api/invite/accept", { token, username: user.value.trim(), display_name: name.value.trim(), password: a.value });
        clearHash(); onSignedIn(); } catch (x) { e.textContent = x.message; }
    });
  };
  card(f); user.focus();
}
