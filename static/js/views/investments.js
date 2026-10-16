import { api, can, el, emptyState, fmtTs, kpis, money, onContext, refresh, showDialog, svg, toast , fill } from "../core.js";
import { holdingForm, invAccountForm } from "../forms.js";

export const title = "Investments";

export async function render(root) {
  const w = can("investments:write");
  const [inv, hist] = await Promise.all([api("GET", "/api/investments"), api("GET", "/api/investments/history?days=365")]);
  const accts = inv.accounts.map((a) => {
    const rows = a.holdings.map((h) => {
      const tr = el("tr", { class: w ? "click" : "", tabindex: 0, onclick: () => w && holdingForm(a.id, h) },
        el("td", {}, el("strong", {}, h.symbol), h.name ? el("div", { class: "meta" }, h.name) : null),
        el("td", { class: "r" }, h.quantity.toLocaleString(undefined, { maximumFractionDigits: 6 })),
        el("td", { class: "r" }, money(h.price_cents)),
        el("td", { class: "r" }, money(h.value_cents)),
        el("td", { class: "r " + (h.gain_cents < 0 ? "out" : "in") }, money(h.gain_cents)));
      onContext(tr, () => [w ? { label: "Edit / update price…", action: () => holdingForm(a.id, h) } : null,
        w ? { label: "Delete", danger: true, action: () => api("DELETE", `/api/investments/holdings/${h.id}`).then(refresh) } : null,
        { label: `Price updated ${fmtTs(h.price_updated)}`, disabled: true, action: () => {} }]);
      return tr;
    });
    const card = el("section", { class: "card" },
      el("div", { class: "spread" }, el("h2", {}, a.name, el("span", { class: "muted small" }, ` ${a.kind}${a.institution ? " · " + a.institution : ""}`)),
        el("span", {}, el("strong", {}, money(a.value_cents)), " ", el("span", { class: a.gain_cents < 0 ? "out" : "in" }, `(${money(a.gain_cents)})`))),
      el("div", { class: "scroll-x" }, el("table", { class: "tbl" },
        el("tr", {}, el("th", {}, "Holding"), el("th", { class: "r" }, "Qty"), el("th", { class: "r" }, "Price"), el("th", { class: "r" }, "Value"), el("th", { class: "r" }, "Gain")),
        rows, a.cash_cents ? el("tr", {}, el("td", {}, "Cash"), el("td"), el("td"), el("td", { class: "r" }, money(a.cash_cents)), el("td")) : null)),
      w ? el("div", { class: "actions left" },
        el("button", { type: "button", onclick: () => holdingForm(a.id) }, "Add holding"),
        el("button", { type: "button", onclick: () => invAccountForm(a) }, "Edit account")) : null);
    onContext(card, () => [w ? { label: "Add holding…", action: () => holdingForm(a.id) } : null,
      w ? { label: "Edit account…", action: () => invAccountForm(a) } : null]);
    return card;
  });
  fill(root, 
    kpis([["Total value", money(inv.value_cents)], ["Cost basis", money(inv.cost_cents)],
      ["Gain / loss", money(inv.gain_cents), inv.gain_cents < 0 ? "out" : "in",
        inv.cost_cents ? `${((inv.gain_cents / inv.cost_cents) * 100).toFixed(1)}%` : null]]),
    hist.length > 1 ? el("section", { class: "card" }, el("h2", {}, "Value over the last year"), lineChart(hist)) : null,
    el("div", { class: "spread" }, el("p", { class: "muted" }, "Prices are entered by hand or pushed by a script using an API token (see Settings > API access)."),
      w ? el("button", { type: "button", class: "primary", onclick: () => invAccountForm() }, "Add account") : null),
    inv.accounts.length ? accts : emptyState("No investment accounts yet.", w ? "Add one" : null, () => invAccountForm()),
    w && inv.accounts.length ? el("button", { type: "button", onclick: bulkPrices }, "Update prices") : null);
}

function lineChart(pts) {
  const W = 640, H = 160, pad = 30;
  const vals = pts.map((p) => p.value_cents), min = Math.min(...vals), max = Math.max(...vals), span = Math.max(1, max - min);
  const xy = pts.map((p, i) => [pad + (i / (pts.length - 1)) * (W - pad * 2), H - 10 - ((p.value_cents - min) / span) * (H - 30)]);
  const g = svg("svg", { viewBox: `0 0 ${W} ${H + 10}`, role: "img", "aria-label": "Investment value over time" });
  g.append(svg("polyline", { points: xy.map((p) => p.join(",")).join(" "), fill: "none", stroke: "var(--ink)", "stroke-width": 2 }));
  const t1 = svg("text", { x: pad, y: H + 8, "font-size": 10, fill: "var(--ink-2)" }); t1.textContent = pts[0].date;
  const t2 = svg("text", { x: W - pad, y: H + 8, "font-size": 10, "text-anchor": "end", fill: "var(--ink-2)" }); t2.textContent = pts.at(-1).date;
  const t3 = svg("text", { x: pad, y: 12, "font-size": 10, fill: "var(--ink-2)" }); t3.textContent = money(max);
  g.append(t1, t2, t3);
  return el("div", { class: "chart" }, g);
}

async function bulkPrices() {
  const inv = await api("GET", "/api/investments");
  const syms = [...new Set(inv.accounts.flatMap((a) => a.holdings.map((h) => h.symbol)))];
  const inputs = syms.map((s) => {
    const h = inv.accounts.flatMap((a) => a.holdings).find((x) => x.symbol === s);
    const i = el("input", { inputmode: "decimal", name: s, value: (h.price_cents / 100).toFixed(2) });
    return el("label", {}, s, i);
  });
  const save = el("button", { type: "submit", class: "primary" }, "Save prices");
  const form = showDialog("Update prices", el("div", {}, inputs), [save]);
  form.onsubmit = async (e) => {
    e.preventDefault();
    const prices = syms.map((s) => ({ symbol: s, price_cents: Math.round(parseFloat(form.elements[s].value.replace(/[$,]/g, "")) * 100) }))
      .filter((p) => Number.isFinite(p.price_cents) && p.price_cents >= 0);
    try { await api("POST", "/api/investments/prices", { prices }); form.closest("dialog").close(); toast("Prices updated"); refresh(); }
    catch (err) { toast(err.message, true); }
  };
}
