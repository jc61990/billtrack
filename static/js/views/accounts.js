import { api, can, el, emptyState, fill, fmtDay, fmtTs, kpis, money, onContext, refresh, safeColor, svg, toast } from "../core.js";
import { balanceForm, bankForm, cardForm } from "../forms.js";

export const title = "Accounts";

const pct = (x) => `${Math.round(x * 100)}%`;
const level = (u, alert) => u === null ? "" : (alert && u * 100 >= alert) || u >= 0.5 ? "bad" : u >= 0.3 ? "warn" : "good";

/** Utilization bar: fill colored by level, tick at the user's alert threshold, 30% guideline tick. */
export function utilBar(u, alertPct, label) {
  if (u === null || u === undefined) return el("p", { class: "muted small" }, "Add a credit limit to see utilization.");
  const w = Math.min(100, u * 100);
  return el("div", { class: "util" },
    el("div", { class: "util-track", role: "meter", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(u * 100),
      "aria-label": label || "Credit utilization" },
      el("i", { class: `util-fill ${level(u, alertPct)}`, style: `width:${w}%` }),
      alertPct ? el("span", { class: "util-tick alert", style: `left:${Math.min(100, alertPct)}%`, title: `Alert at ${alertPct}%` }) : null,
      alertPct !== 30 ? el("span", { class: "util-tick guide", style: "left:30%", title: "30% guideline" }) : null),
    el("div", { class: "util-legend" }, el("strong", { class: level(u, alertPct) }, `${pct(u)} used`),
      alertPct ? el("span", { class: "muted" }, `alert at ${alertPct}%`) : null));
}

function spark(hist) {
  if (!hist || hist.length < 2) return null;
  const W = 160, H = 36, v = hist.map((h) => h.balance_cents), min = Math.min(...v), max = Math.max(...v), span = Math.max(1, max - min);
  const pts = v.map((x, i) => `${(i / (v.length - 1)) * W},${H - 3 - ((x - min) / span) * (H - 6)}`).join(" ");
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark", role: "img", "aria-label": "Balance over the last 90 days" });
  g.append(svg("polyline", { points: pts, fill: "none", stroke: "var(--ink-2)", "stroke-width": 1.5 }));
  const t = svg("title"); t.textContent = `${hist[0].date}: ${money(v[0])} → ${hist.at(-1).date}: ${money(v.at(-1))}`; g.append(t);
  return g;
}

function menu(a) {
  const w = can("finance:write");
  return [
    w ? { label: "Update balance…", action: () => balanceForm(a) } : null,
    { label: w ? "Edit…" : "Details…", action: () => (a.kind === "credit" ? cardForm(a) : bankForm(a)) },
    w ? "-" : null,
    w ? { label: "Delete", danger: true, action: () => api("DELETE", `/api/accounts/${a.id}`).then(refresh).catch((e) => toast(e.message, true)) } : null,
  ];
}

function bankCard(a, weekEnd) {
  const w = can("finance:write");
  const card = el("article", { class: `card acct${a.low ? " alert" : ""}`, style: `border-left:4px solid ${safeColor(a.color)}`, tabindex: 0 },
    el("div", { class: "spread" },
      el("div", {}, el("h3", {}, a.name), el("div", { class: "meta" }, [a.kind, a.institution, a.last4 ? `••${a.last4}` : ""].filter(Boolean).join(" · "))),
      spark(a.history)),
    el("div", { class: "spread big" }, el("strong", { class: a.balance_cents < 0 ? "out" : "" }, money(a.balance_cents)),
      a.low ? el("span", { class: "tag overdue" }, "Below alert") : null),
    el("div", { class: "meta" }, a.linked ? `🏦 From your bank · updated ${a.balance_updated ? fmtTs(a.balance_updated) : "never"}`
      : el("span", {}, `Updated ${a.balance_updated ? fmtTs(a.balance_updated) : "never"} by hand · `,
        el("a", { href: "#banks" }, "link this bank to update it automatically"))),
    a.pending_cents ? el("p", { class: a.projected_low ? "out" : "muted" },
      `${a.projected_low ? "⚠ " : ""}About ${money(a.projected_cents)} by ${fmtDay(weekEnd, { weekday: "short", month: "short", day: "numeric" })} after `
      + `scheduled bills and pay (${a.pending_cents < 0 ? "−" : "+"}${money(Math.abs(a.pending_cents))})`) : null,
    a.low_balance_cents ? el("div", { class: "meta" }, `Alert below ${money(a.low_balance_cents)}`) : null,
    w ? el("div", { class: "actions left" }, el("button", { type: "button", class: "primary", onclick: () => balanceForm(a) }, "Update balance"),
      el("button", { type: "button", onclick: () => bankForm(a) }, "Edit")) : null);
  onContext(card, () => menu(a));
  return card;
}

function creditCard(c) {
  const w = can("finance:write");
  const card = el("article", { class: `card acct${c.over_alert ? " alert" : ""}`, style: `border-left:4px solid ${safeColor(c.color)}`, tabindex: 0 },
    el("div", { class: "spread" },
      el("div", {}, el("h3", {}, c.name), el("div", { class: "meta" }, [c.institution, c.last4 ? `••${c.last4}` : "", c.apr_bps ? `${(c.apr_bps / 100).toFixed(2)}% APR` : ""].filter(Boolean).join(" · "))),
      spark(c.history)),
    el("div", { class: "spread big" },
      el("span", {}, el("strong", { class: c.balance_cents > 0 ? "out" : "" }, money(c.balance_cents)),
        c.credit_limit_cents ? el("span", { class: "muted" }, ` of ${money(c.credit_limit_cents)}`) : null),
      c.available_cents !== null ? el("span", { class: "muted small" }, `${money(c.available_cents)} available`) : null),
    utilBar(c.utilization, c.util_alert_pct, `${c.name} utilization`),
    el("div", { class: "meta" }, [
      c.next_due ? `Due ${fmtDay(c.next_due, { month: "short", day: "numeric" })}${c.days_to_due === 0 ? " (today)" : ` (${c.days_to_due}d)`}` : null,
      c.min_payment_cents ? `min ${money(c.min_payment_cents)}` : null,
      c.upcoming_charges_cents ? `${money(c.upcoming_charges_cents)} in bills charged this week` : null,
      c.linked ? `🏦 from your bank, updated ${c.balance_updated ? fmtTs(c.balance_updated) : "never"}`
        : `updated ${c.balance_updated ? fmtTs(c.balance_updated) : "never"} by hand`].filter(Boolean).join(" · ")),
    c.next_due && c.days_to_due <= 3 && c.balance_cents > 0 ? el("p", { class: "out" }, "⚠ Payment due soon") : null,
    w ? el("div", { class: "actions left" }, el("button", { type: "button", class: "primary", onclick: () => balanceForm(c) }, "Update balance"),
      el("button", { type: "button", onclick: () => cardForm(c) }, "Edit")) : null);
  onContext(card, () => menu(c));
  return card;
}

export async function render(root) {
  const v = await api("GET", "/api/accounts/overview");
  const w = can("finance:write");
  fill(root,
    kpis([
      ["Cash in bank", money(v.cash_cents), "in", `≈ ${money(v.projected_cash_cents)} by end of week`],
      ["Card balances", money(v.card_balance_cents), v.card_balance_cents ? "out" : "", v.card_limit_cents ? `of ${money(v.card_limit_cents)} total limit` : null],
      ["Cash after cards", money(v.cash_cents - v.card_balance_cents), v.cash_cents - v.card_balance_cents < 0 ? "out" : ""],
    ]),
    v.cards.length ? el("section", { class: "card" }, el("h2", {}, "Overall credit utilization"),
      utilBar(v.card_utilization, 30, "Overall credit utilization")) : null,
    el("div", { class: "spread" }, el("h2", {}, "Checking and savings"),
      w ? el("div", { class: "actions left" },
        el("button", { type: "button", onclick: () => bankForm({}, "checking") }, "Add checking"),
        el("button", { type: "button", onclick: () => bankForm({}, "savings") }, "Add savings")) : null),
    v.banks.length ? el("div", { class: "grid2" }, v.banks.map((a) => bankCard(a, v.week_end)))
      : emptyState("No bank accounts yet.", w ? "Add checking" : null, () => bankForm({}, "checking")),
    el("div", { class: "spread" }, el("h2", {}, "Credit cards"),
      w ? el("button", { type: "button", onclick: () => cardForm() }, "Add credit card") : null),
    v.cards.length ? el("div", { class: "grid2" }, v.cards.map(creditCard))
      : emptyState("No credit cards yet.", w ? "Add credit card" : null, () => cardForm()),
    v.other.length ? el("h2", {}, "Loans and other") : null,
    v.other.length ? el("div", { class: "grid2" }, v.other.map((a) => bankCard(a, v.week_end))) : null,
    el("p", { class: "muted small" }, "Balances are entered by you (or a script with an API token: POST /api/accounts/{id}/balance). "
      + "BillTrack never connects to your bank and never stores full account or card numbers."));
}
