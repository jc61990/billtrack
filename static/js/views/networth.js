import { api, can, el, fill, fmtTs, money, moneyShort, onContext, refresh, safeColor, svg, toast } from "../core.js";
import { accountForm, balanceForm, nwItemForm, nwValueForm, NW_ASSET, NW_LIAB } from "../forms.js";

export const title = "Net worth";
let days = 365;
const RANGES = [[90, "3M"], [180, "6M"], [365, "1Y"], [1825, "5Y"], [3650, "All"]];
const KIND = { checking: "Checking", savings: "Savings", cash: "Cash", other: "Other account", credit: "Credit card", loan: "Loan",
  brokerage: "Brokerage", retirement: "Retirement", crypto: "Crypto", hsa: "HSA" };

function chart(hist) {
  if (hist.length < 2) return el("p", { class: "muted" }, "History builds automatically, one point per day, starting today. Check back in a few days.");
  // size the drawing to the screen so labels stay readable on phones
  const W = Math.max(320, Math.min(680, (document.getElementById("view")?.clientWidth || 680) - 40)), H = W < 500 ? 200 : 210;
  const L = 52, R = 8, T = 10, B = 24;
  const vals = hist.flatMap((h) => [h.net_cents, h.assets_cents, -h.liabilities_cents, 0]);
  const min = Math.min(...vals), max = Math.max(...vals), span = Math.max(1, max - min);
  const x = (i) => L + (i / (hist.length - 1)) * (W - L - R), y = (v) => T + (1 - (v - min) / span) * (H - T - B);
  const line = (k, sign = 1) => hist.map((h, i) => `${x(i)},${y(sign * h[k])}`).join(" ");
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Net worth over time" });
  for (const v of [max, (max + min) / 2, min]) {
    g.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "var(--rule)" }));
    const t = svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end", "font-size": 10, fill: "var(--ink-2)" }); t.textContent = moneyShort(v); g.append(t);
  }
  if (min < 0 && max > 0) g.append(svg("line", { x1: L, x2: W - R, y1: y(0), y2: y(0), stroke: "var(--ink-2)", "stroke-dasharray": "3 3" }));
  const area = `${x(0)},${y(Math.max(min, 0))} ${line("net_cents")} ${x(hist.length - 1)},${y(Math.max(min, 0))}`;
  g.append(svg("polygon", { points: area, fill: "color-mix(in srgb, var(--ink) 10%, transparent)" }));
  g.append(svg("polyline", { points: line("assets_cents"), fill: "none", stroke: "var(--in)", "stroke-width": 1.2, "stroke-dasharray": "4 3" }));
  g.append(svg("polyline", { points: line("liabilities_cents", -1), fill: "none", stroke: "var(--out)", "stroke-width": 1.2, "stroke-dasharray": "4 3" }));
  g.append(svg("polyline", { points: line("net_cents"), fill: "none", stroke: "var(--ink)", "stroke-width": 2.2 }));
  for (const i of [0, Math.floor((hist.length - 1) / 2), hist.length - 1]) {
    const t = svg("text", { x: x(i), y: H - 6, "text-anchor": i === 0 ? "start" : i === hist.length - 1 ? "end" : "middle", "font-size": 10, fill: "var(--ink-2)" });
    t.textContent = hist[i].date; g.append(t);
  }
  hist.forEach((h, i) => { const c = svg("circle", { cx: x(i), cy: y(h.net_cents), r: 6, fill: "transparent" }); const tt = svg("title");
    tt.textContent = `${h.date}: net ${money(h.net_cents)} (assets ${money(h.assets_cents)}, debts ${money(h.liabilities_cents)})`; c.append(tt); g.append(c); });
  return el("div", { class: "chart" }, g, el("div", { class: "legend" }, el("span", {}, "━ Net worth"), "  ",
    el("span", { class: "in" }, "┅ Assets"), "  ", el("span", { class: "out" }, "┅ Debts (shown below zero)")));
}

function change(label, c) {
  if (c === undefined) return null;
  return el("span", { class: `chg ${c > 0 ? "up" : c < 0 ? "down" : ""}` }, `${c > 0 ? "▲" : c < 0 ? "▼" : "•"} ${money(Math.abs(c))} `, el("small", {}, label));
}

function row(x, side) {
  const w = can("finance:write");
  const isItem = x.source === "item", isInv = x.source === "investment";
  const type = isItem ? (side === "asset" ? NW_ASSET : NW_LIAB)[x.category] || x.category : KIND[x.kind] || x.kind;
  const upd = isItem ? x.value_updated : x.balance_updated;
  const open = () => isInv ? (location.hash = "investments") : isItem ? nwItemForm(x) : accountForm(x);
  const li = el("li", { class: isItem && !x.active ? "muted" : "" }, el("button", { type: "button", class: "row", onclick: open },
    el("span", {}, el("span", {}, x.color ? el("span", { class: "dot", style: `background:${safeColor(x.color)}` }) : null, x.name),
      el("div", { class: "meta" }, [type, isInv ? "from Investments" : upd ? `updated ${fmtTs(upd)}` : "never updated", isItem && !x.active ? "not counted" : null].filter(Boolean).join(" · "))),
    el("span", { class: `amt ${side === "liability" ? "out" : ""}` }, money(x.balance_cents))));
  onContext(li, () => [
    w && !isInv ? { label: side === "asset" ? "Update value…" : "Update amount owed…", action: () => isItem ? nwValueForm(x) : balanceForm(x) } : null,
    { label: isInv ? "Open Investments" : w ? "Edit…" : "Details…", action: open },
    w && isItem ? "-" : null,
    w && isItem ? { label: "Delete", danger: true, action: () => api("DELETE", `/api/networth/items/${x.id}`).then(refresh).catch((e) => toast(e.message, true)) } : null,
  ]);
  return li;
}

function compBars(parts) {
  const A = [["Cash", parts.cash_cents], ["Investments", parts.investments_cents], ["Property and other", parts.other_assets_cents]].filter((x) => x[1] !== undefined);
  const Lb = [["Credit cards", parts.cards_cents], ["Loans", parts.loans_cents], ["Mortgage and other", parts.other_liabilities_cents]];
  const ta = A.reduce((s, x) => s + Math.max(0, x[1]), 0), tl = Lb.reduce((s, x) => s + x[1], 0), big = Math.max(1, ta, tl);
  const stack = (items, total, cls) => el("div", { class: "stack-bar", style: `width:${(total / big) * 100}%` },
    items.filter((x) => x[1] > 0).map(([l, v], i) => el("i", { class: `${cls}${i}`, style: `width:${(v / Math.max(1, total)) * 100}%`, title: `${l}: ${money(v)}` })));
  const legend = (items, cls) => el("div", { class: "stack-legend" }, items.filter((x) => x[1]).map(([l, v], i) =>
    el("span", {}, el("i", { class: `${cls}${i}` }), `${l} ${money(v)}`)));
  return el("div", { class: "comp" },
    el("div", { class: "spread" }, el("strong", {}, "Assets"), el("span", { class: "in" }, money(ta))), stack(A, ta, "a"), legend(A, "a"),
    el("div", { class: "spread" }, el("strong", {}, "Debts"), el("span", { class: "out" }, money(tl))), stack(Lb, tl, "l"), legend(Lb, "l"));
}

export async function render(root) {
  const w = can("finance:write");
  const [nw, hist] = await Promise.all([api("GET", "/api/networth"), api("GET", `/api/networth/history?days=${days}`)]);
  const ratio = nw.assets_cents > 0 ? nw.liabilities_cents / nw.assets_cents : null;
  fill(root,
    el("section", { class: "card nw-hero" },
      el("small", { class: "muted" }, "Net worth"),
      el("div", { class: `nw-big ${nw.net_cents < 0 ? "out" : ""}` }, money(nw.net_cents)),
      el("div", { class: "chgs" }, change("30 days", nw.changes["30d"]), change("90 days", nw.changes["90d"]),
        change("1 year", nw.changes["1y"]), change(`since ${nw.first_snapshot || "start"}`, nw.changes.all)),
      el("div", { class: "meta" }, `Assets ${money(nw.assets_cents)} − debts ${money(nw.liabilities_cents)}`
        + (ratio !== null ? ` · debt is ${Math.round(ratio * 100)}% of assets` : ""))),
    el("section", { class: "card" },
      el("div", { class: "spread" }, el("h2", {}, "Over time"), el("div", { class: "seg" }, RANGES.map(([d, l]) =>
        el("button", { type: "button", "aria-selected": String(days === d), onclick: () => { days = d; render(root); } }, l)))),
      chart(hist)),
    el("section", { class: "card" }, el("h2", {}, "What it's made of"), compBars(nw.parts)),
    el("div", { class: "grid2" },
      el("section", {}, el("div", { class: "spread" }, el("h2", {}, "Assets"),
        w ? el("button", { type: "button", onclick: () => nwItemForm({}, "asset") }, "Add asset") : null),
        el("ul", { class: "list" }, nw.assets.map((x) => row(x, "asset")))),
      el("section", {}, el("div", { class: "spread" }, el("h2", {}, "Debts"),
        w ? el("button", { type: "button", onclick: () => nwItemForm({}, "liability") }, "Add debt") : null),
        el("ul", { class: "list" }, nw.liabilities.map((x) => row(x, "liability"))))),
    el("p", { class: "muted small" }, "Bank accounts, cards and loans come from Accounts; investments from Investments. "
      + "Add things BillTrack doesn't track (home, car, mortgage) here. A snapshot is saved daily and whenever a value changes."));
}
