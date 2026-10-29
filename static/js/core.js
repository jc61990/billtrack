// Core helpers. Every DOM node is built with textContent/attributes (never innerHTML with data) to prevent XSS.
export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];

export const S = { me: null, perms: new Set(), view: "week", day: null, month: null, accounts: [], sources: [],
  calSel: null, refreshers: {} };
export const can = (p) => S.perms.has(p);

export const REC = ["weekly", "biweekly", "semimonthly", "monthly", "quarterly", "yearly", "once"];
export const REC_LABEL = { weekly: "Weekly", biweekly: "Every 2 weeks", semimonthly: "Twice a month", monthly: "Monthly",
  quarterly: "Quarterly", yearly: "Yearly", once: "One time" };
export const KIND_LABEL = { overtime: "Overtime", bonus: "Bonus", side: "Side work", refund: "Refund", other: "Other" };
export const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (k === "style") n.style.cssText = v;          // CSSOM: allowed under CSP style-src 'self'
    else if (k === "dataset") Object.assign(n.dataset, v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) if (c !== null && c !== undefined && c !== false)
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
}
export function svg(tag, attrs = {}) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "fill" || k === "stroke") n.style[k] = v; else n.setAttribute(k, v);
  }
  return n;
}

export const money = (c) => (c < 0 ? "-" : "") + "$" + (Math.abs(c) / 100).toLocaleString(undefined,
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const moneyShort = (c) => {
  const a = Math.abs(c) / 100, s = c < 0 ? "-" : "";
  return s + "$" + (a >= 10000 ? Math.round(a / 1000) + "k" : a >= 1000 ? (a / 1000).toFixed(1) + "k" : Math.round(a));
};
export function parseMoney(s, allowNeg = false) {
  s = String(s ?? "").replace(/[$,\s]/g, "");
  if (s === "") return null;
  const neg = allowNeg && s.startsWith("-");
  if (neg) s = s.slice(1);
  if (!/^\d{1,11}(\.\d{0,2})?$/.test(s)) return NaN;
  const [w, f = ""] = s.split(".");
  const c = Number(w) * 100 + Number((f + "00").slice(0, 2));
  return neg ? -c : c;
}
export const centsIn = (c) => (c === null || c === undefined || c === "") ? "" : (c / 100).toFixed(2);
export const iso = (d) => d.toISOString().slice(0, 10);
export const parseIso = (s) => new Date(s + "T00:00:00Z");
export const addDays = (s, n) => { const d = parseIso(s); d.setUTCDate(d.getUTCDate() + n); return iso(d); };
export const fmtDay = (s, opt = { weekday: "short", month: "short", day: "numeric" }) =>
  parseIso(s).toLocaleDateString(undefined, { ...opt, timeZone: "UTC" });
export const fmtTs = (s) => s ? new Date(s).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "never";
export const acctName = (id) => (S.accounts.find((a) => a.id === id) || {}).name || "";
export const safeColor = (c) => /^#[0-9a-fA-F]{6}$/.test(c || "") ? c : "#888888";
export const weekStartOf = (s) => { const wd = (parseIso(s).getUTCDay() + 6) % 7; return addDays(s, -((wd - S.me.week_start + 7) % 7)); };

export function toast(msg, isErr = false) {
  const t = $("#toast"); t.textContent = msg; t.classList.toggle("err", isErr); t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 3200);
}

let onUnauthorized = () => {};
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn; };

import { call, text } from "./channel.js";

/** All browser traffic goes through the encrypted channel; nothing readable crosses the network tab. */
export async function api(method, path, body, { raw = false } = {}) {
  const r = await call(method, path, body);
  const auth = path.startsWith("/api/login") || path.startsWith("/api/password") || path.startsWith("/api/invite");
  if (r.status === 401 && !auth) { onUnauthorized(); throw new Error("Sign in required"); }
  const ok = r.status >= 200 && r.status < 300;
  if (raw && ok) return { blob: async () => new Blob([r.bytes], { type: r.headers["content-type"] || "application/octet-stream" }) };
  const data = (r.headers["content-type"] || "").includes("json") && r.bytes.length ? JSON.parse(text(r.bytes)) : null;
  if (!ok) {
    let d = data?.detail;
    if (Array.isArray(d)) d = d.map((e) => `${(e.loc || []).slice(-1)[0]}: ${e.msg}`).join("; ");
    throw new Error(typeof d === "string" ? d : `Request failed (${r.status})`);
  }
  return data;
}

// ---------- dialogs ----------
const dlg = () => $("#dlg");
export function closeDialog() { if (dlg().open) dlg().close(); }

export function showDialog(title, body, actions = []) {
  const form = $("#dlgForm"); form.replaceChildren();
  form.onsubmit = (e) => e.preventDefault();
  form.append(el("h3", {}, title), body, el("div", { class: "actions" }, el("span", { class: "spacer" }),
    ...actions, el("button", { type: "button", onclick: closeDialog }, actions.length ? "Cancel" : "Close")));
  dlg().showModal();
  return form;
}

export function confirmBox(title, text, okLabel = "Confirm", danger = true) {
  return new Promise((res) => {
    const ok = el("button", { type: "button", class: danger ? "danger-solid" : "primary", onclick: () => { closeDialog(); res(true); } }, okLabel);
    showDialog(title, el("p", {}, text), [ok]);
    dlg().addEventListener("close", () => res(false), { once: true });
  });
}

function field(f) {
  if (f.type === "custom") return f.node;   // caller-built block; value read back with f.get()
  const v = f.value ?? "";
  if (f.type === "check")
    return el("label", { class: "check" }, el("input", { type: "checkbox", name: f.name, checked: !!v }), f.label);
  let input;
  if (f.type === "select") {
    input = el("select", { name: f.name },
      f.options.map(([val, lab]) => el("option", { value: val, selected: String(val) === String(v) }, lab)));
  } else if (f.type === "textarea") {
    input = el("textarea", { name: f.name, rows: 3, maxlength: 2000 }); input.value = v;
  } else {
    const t = f.type === "money" ? "text" : f.type || "text";
    input = el("input", { name: f.name, type: t, required: f.required, maxlength: t === "text" ? (f.max || 100) : null,
      inputmode: f.type === "money" || f.type === "number" ? "decimal" : null, step: f.step, min: f.min, max: f.max,
      placeholder: f.placeholder, autocomplete: f.autocomplete || "off", pattern: f.pattern });
    input.value = f.type === "money" ? centsIn(v === "" ? null : v) : v;
  }
  return el("label", {}, f.label, input, f.hint ? el("span", { class: "hint" }, f.hint) : null);
}

/** fields: [{name,label,type,value,options,required,pair,hint,int,nullable,neg}] */
export function openForm(title, fields, onSave, onDelete, saveLabel = "Save") {
  const form = $("#dlgForm"); form.replaceChildren();
  const err = el("p", { class: "err", role: "alert" });
  let pair = [];
  for (const f of fields) {
    const node = field(f);
    if (f.pair) { pair.push(node); if (pair.length === 2) { form.append(el("div", { class: "row2" }, pair)); pair = []; } }
    else { if (pair.length) { form.append(...pair); pair = []; } form.append(node); }
  }
  if (pair.length) form.append(...pair);
  form.prepend(el("h3", {}, title));
  form.append(err, el("div", { class: "actions" },
    onDelete ? el("button", { type: "button", class: "danger", onclick: async () => {
      if (!(await confirmBox("Delete?", "This can't be undone.", "Delete"))) { openForm(title, fields, onSave, onDelete, saveLabel); return; }
      try { await onDelete(); toast("Deleted"); refresh(); } catch (e) { toast(e.message, true); }
    } }, "Delete") : null,
    el("span", { class: "spacer" }),
    el("button", { type: "button", onclick: closeDialog }, "Cancel"),
    el("button", { type: "submit", class: "primary" }, saveLabel)));
  form.onsubmit = async (e) => {
    e.preventDefault();
    const out = {};
    for (const f of fields) {
      if (f.skip) continue;
      const n = form.elements[f.name];
      if (f.type === "custom") {
        try { out[f.name] = f.get(); } catch (x) { err.textContent = x.message; return; }
        continue;
      }
      if (f.type === "check") out[f.name] = n.checked;
      else if (f.type === "money") {
        const c = parseMoney(n.value, f.neg);
        if (Number.isNaN(c)) { err.textContent = `${f.label}: enter an amount like 125.50`; return; }
        if (c === null && f.required) { err.textContent = `${f.label} is required`; return; }
        out[f.name] = c === null && f.zero ? 0 : c;
      } else if (f.type === "number") out[f.name] = n.value === "" ? (f.nullable ? null : 0) : Number(n.value);
      else if (f.int) out[f.name] = n.value === "" ? null : Number(n.value);
      else out[f.name] = n.value === "" && f.nullable ? null : n.value;
    }
    const btn = form.querySelector("button[type=submit]"); btn.disabled = true;
    try { await onSave(out); closeDialog(); toast("Saved"); refresh(); }
    catch (e2) { err.textContent = e2.message; }
    finally { btn.disabled = false; }
  };
  dlg().showModal();
  form.querySelector("input,select,textarea")?.focus();
}

// ---------- context menu (right-click, long-press, keyboard menu key) ----------
let menuEl = null, menuAt = 0;
export function closeMenu() { menuEl?.remove(); menuEl = null; }
export function showMenu(x, y, items) {
  closeMenu();
  items = items.filter(Boolean);
  if (!items.length) return;
  menuAt = performance.now();
  menuEl = el("div", { class: "ctx", role: "menu" }, items.map((it) => it === "-" ? el("hr") :
    el("button", { type: "button", role: "menuitem", class: it.danger ? "danger" : "", disabled: it.disabled,
      onclick: () => { closeMenu(); it.action(); } }, it.label)));
  document.body.append(menuEl);
  const r = menuEl.getBoundingClientRect();
  menuEl.style.left = Math.max(4, Math.min(x, innerWidth - r.width - 4)) + "px";
  menuEl.style.top = Math.max(4, Math.min(y, innerHeight - r.height - 4)) + "px";
  menuEl.querySelector("button:not([disabled])")?.focus();
}
document.addEventListener("pointerdown", (e) => { if (menuEl && !menuEl.contains(e.target)) closeMenu(); });
document.addEventListener("keydown", (e) => {
  if (!menuEl) return;
  const btns = $$("button:not([disabled])", menuEl), i = btns.indexOf(document.activeElement);
  if (e.key === "Escape") closeMenu();
  else if (e.key === "ArrowDown") { e.preventDefault(); btns[(i + 1) % btns.length]?.focus(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); btns[(i - 1 + btns.length) % btns.length]?.focus(); }
});
// Close on scroll, but ignore the scroll that often lands right after opening (scroll-into-view, momentum)
addEventListener("scroll", () => { if (performance.now() - menuAt > 250) closeMenu(); }, true);
addEventListener("resize", closeMenu);

/** Right-click / menu key on desktop; long-press (when not dragging) on touch. */
export function onContext(node, itemsFn) {
  node.addEventListener("contextmenu", (e) => {
    e.preventDefault(); e.stopPropagation();
    const r = node.getBoundingClientRect();
    showMenu(e.clientX || r.left + 8, e.clientY || r.bottom, itemsFn());
  });
  node._ctx = itemsFn;
}

// ---------- drag and drop (pointer events: mouse, pen, touch) ----------
// Mouse: drag after moving 6px. Touch: hold 400 ms to pick up, then move; hold and release opens the menu.
let drag = null;
export function draggable(node, { label, date, onDrop }) {
  node.classList.add("draggable");
  node.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest("input,button:not(.chip)")) return;
    const touch = e.pointerType !== "mouse";
    drag = { node, label, date, onDrop, x: e.clientX, y: e.clientY, active: false, armed: !touch, touch, id: e.pointerId };
    if (touch) drag.timer = setTimeout(() => { if (drag && !drag.active) { drag.armed = true; node.classList.add("armed"); navigator.vibrate?.(15); } }, 400);
  });
}
function startDrag(e) {
  drag.active = true;
  drag.ghost = el("div", { class: "ghost" }, drag.label);
  document.body.append(drag.ghost);
  drag.node.classList.add("dragging");
  document.body.classList.add("is-dragging");
}
function target(e) {
  drag.ghost.style.display = "none";
  const t = document.elementFromPoint(e.clientX, e.clientY)?.closest("[data-drop-date]");
  drag.ghost.style.display = "";
  return t;
}
document.addEventListener("pointermove", (e) => {
  if (!drag || e.pointerId !== drag.id) return;
  const dist = Math.hypot(e.clientX - drag.x, e.clientY - drag.y);
  if (!drag.active) {
    if (!drag.armed) { if (dist > 10) { clearTimeout(drag.timer); drag = null; } return; }  // touch scroll wins
    if (dist < 6) return;
    startDrag(e);
  }
  drag.ghost.style.left = e.clientX + 12 + "px"; drag.ghost.style.top = e.clientY + 12 + "px";
  $$(".drop-over").forEach((x) => x.classList.remove("drop-over"));
  target(e)?.classList.add("drop-over");
});
document.addEventListener("touchmove", (e) => { if (drag?.armed) e.preventDefault(); }, { passive: false });
function endDrag(e, cancel) {
  if (!drag || (e && e.pointerId !== drag.id)) return;
  clearTimeout(drag.timer);
  const d = drag; drag = null;
  d.node.classList.remove("armed", "dragging");
  document.body.classList.remove("is-dragging");
  $$(".drop-over").forEach((x) => x.classList.remove("drop-over"));
  if (d.active) {
    d.ghost.remove();
    d.node.addEventListener("click", (ev) => { ev.stopPropagation(); ev.preventDefault(); }, { capture: true, once: true });
    if (!cancel) {
      const t = (() => { const g = document.elementFromPoint(e.clientX, e.clientY); return g?.closest("[data-drop-date]"); })();
      const nd = t?.dataset.dropDate;
      if (nd && nd !== d.date) d.onDrop(nd);
    }
  } else if (d.armed && d.touch && !cancel && d.node._ctx) {
    const r = d.node.getBoundingClientRect();   // long-press without moving = context menu
    d.node.addEventListener("click", (ev) => { ev.stopPropagation(); ev.preventDefault(); }, { capture: true, once: true });
    showMenu(r.left + 8, r.bottom, d.node._ctx());
  }
}
document.addEventListener("pointerup", (e) => endDrag(e, false));
document.addEventListener("pointercancel", (e) => endDrag(e, true));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drag) endDrag(null, true); });

// ---------- refresh plumbing ----------
let refreshFn = () => {};
export const setRefresh = (fn) => { refreshFn = fn; };
export const refresh = () => refreshFn();

export function emptyState(text, actionLabel, action) {
  return el("div", { class: "empty-state" }, el("p", {}, text),
    actionLabel ? el("button", { type: "button", class: "primary", onclick: action }, actionLabel) : null);
}
export function section(title, ...kids) { return el("section", { class: "card" }, el("h2", {}, title), ...kids); }
export function kpis(items) {
  return el("div", { class: "kpis" }, items.filter(Boolean).map(([label, value, cls, sub]) =>
    el("div", {}, el("small", {}, label), el("strong", { class: cls || "" }, value), sub ? el("div", { class: "sub" }, sub) : null)));
}
export function barChart(series, { h = 180, label = (x) => x.label } = {}) {
  const W = 640, pad = 28, bw = (W - pad * 2) / Math.max(1, series.length);
  const max = Math.max(1, ...series.map((x) => Math.max(x.in_cents, x.out_cents)));
  const g = svg("svg", { viewBox: `0 0 ${W} ${h + 24}`, role: "img", "aria-label": "Money in and out over time" });
  const every = Math.ceil(series.length / 12);
  series.forEach((x, i) => {
    const x0 = pad + i * bw, hi = (x.in_cents / max) * (h - 10), ho = (x.out_cents / max) * (h - 10);
    const grp = svg("g");
    grp.append(svg("rect", { x: x0 + bw * 0.12, y: h - hi, width: bw * 0.36, height: hi, fill: "var(--in)", rx: 2 }),
      svg("rect", { x: x0 + bw * 0.52, y: h - ho, width: bw * 0.36, height: ho, fill: "var(--out)", rx: 2 }));
    const t = svg("title"); t.textContent = `${label(x)}: in ${money(x.in_cents)}, out ${money(x.out_cents)}, net ${money(x.net_cents)}`;
    grp.append(t); g.append(grp);
    if (i % every === 0) {
      const tx = svg("text", { x: x0 + bw / 2, y: h + 16, "text-anchor": "middle", "font-size": 10, fill: "var(--ink-2)" });
      tx.textContent = label(x); g.append(tx);
    }
  });
  g.append(svg("line", { x1: pad, x2: W - pad, y1: h, y2: h, stroke: "var(--rule)" }));
  return el("div", { class: "chart" }, g, el("div", { class: "legend" },
    el("span", { class: "in" }, "■ In"), " ", el("span", { class: "out" }, "■ Out")));
}
export function hbars(items, total) {
  const max = Math.max(1, ...items.map((x) => x.cents));
  return el("ul", { class: "hbars" }, items.map((x) => el("li", {},
    el("span", { class: "lbl" }, x.label), el("span", { class: "bar" }, el("i", { style: `width:${(x.cents / max) * 100}%` })),
    el("span", { class: "amt" }, money(x.cents), total ? el("small", {}, ` ${Math.round((x.cents / total) * 100)}%`) : null))));
}
export function progressBar(p, color) {
  return el("div", { class: "progress", role: "progressbar", "aria-valuenow": Math.round(p * 100), "aria-valuemin": 0, "aria-valuemax": 100 },
    el("i", { style: `width:${Math.min(100, p * 100)}%;background:${safeColor(color)}` }));
}

/** replaceChildren that skips null/false (conditional sections). */
export function fill(root, ...kids) {
  root.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
}
