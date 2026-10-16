import { api, can, el, fill, fmtDay, money, progressBar, S, safeColor, svg } from "../core.js";
import { billForm, expenseForm, extraForm, reminderForm } from "../forms.js";
import { utilBar } from "./accounts.js";

export const title = "Home";

const go = (v) => () => { location.hash = v; };
const card = (title, view, ...kids) => el("section", { class: "card hcard" },
  el("div", { class: "spread" }, el("h2", {}, title),
    view ? el("button", { type: "button", class: "link", onclick: go(view), "aria-label": `Open ${title}` }, "Open →") : null), ...kids);
const short = (d) => fmtDay(d, { weekday: "short", month: "short", day: "numeric" });
const acctColor = (id) => safeColor((S.accounts.find((a) => a.id === id) || {}).color);

function greeting(name) {
  const h = new Date().getHours();
  return `${h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"}${name ? ", " + name : ""}`;
}

function spark(hist) {
  if (!hist || hist.length < 2) return null;
  const W = 220, H = 44, v = hist.map((x) => x.net_cents), lo = Math.min(...v), hi = Math.max(...v), sp = Math.max(1, hi - lo);
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "hspark", role: "img", "aria-label": "Net worth, last 90 days" });
  g.append(svg("polyline", { points: v.map((x, i) => `${(i / (v.length - 1)) * W},${H - 4 - ((x - lo) / sp) * (H - 8)}`).join(" "),
    fill: "none", stroke: "var(--ink)", "stroke-width": 2 }));
  return g;
}

function attention(h) {
  const items = [];
  for (const b of h.overdue || []) items.push(["bad", `${b.name} was due ${short(b.date)}`, money(b.amount_cents), "cal"]);
  for (const a of h.accounts?.low || []) items.push(["bad", a.now ? `${a.name} is below your alert` : `${a.name} will dip below your alert by ${short(h.accounts.week_end)}`,
    money(a.now ? a.balance_cents : a.projected_cents), "accounts"]);
  for (const c of h.accounts?.cards_due || []) items.push(["warn", `${c.name} payment due ${c.days_to_due === 0 ? "today" : short(c.next_due)}`,
    c.min_payment_cents ? `min ${money(c.min_payment_cents)}` : money(c.balance_cents), "accounts"]);
  for (const c of h.accounts?.cards_over || []) items.push(["warn", `${c.name} is ${Math.round(c.utilization * 100)}% utilized`, `alert at ${c.util_alert_pct}%`, "accounts"]);
  const when = (ts) => { const d = new Date(ts), t = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    return d.toDateString() === new Date().toDateString() ? `today ${t}` : d.toLocaleDateString(undefined, { month: "short", day: "numeric" }); };
  for (const n of (h.alerts || []).slice(0, 3)) items.push([n.level === "warn" ? "warn" : "info", n.title, when(n.ts), "alerts"]);
  if (h.unread > 3) items.push(["info", `${h.unread - 3} more unread alerts`, "Open", "alerts"]);
  if (!items.length) return el("section", { class: "card ok-strip" }, "✓ All caught up. Nothing overdue, no low balances, no unread alerts.");
  return el("section", { class: "card attn" }, el("h2", {}, "Needs attention"),
    el("ul", { class: "attn-list" }, items.slice(0, 8).map(([lvl, text, right, view]) =>
      el("li", { class: lvl }, el("button", { type: "button", class: "row", onclick: go(view) }, el("span", {}, text), el("span", { class: "amt" }, right))))));
}

export async function render(root) {
  const h = await api("GET", "/api/home");
  const w = h.week, wr = can("finance:write");
  const quick = el("div", { class: "quick" },
    wr ? el("button", { type: "button", onclick: () => billForm() }, "+ Bill") : null,
    wr ? el("button", { type: "button", onclick: () => extraForm() }, "+ Overtime") : null,
    wr ? el("button", { type: "button", onclick: () => expenseForm() }, "+ Spending") : null,
    can("reminders") ? el("button", { type: "button", onclick: () => reminderForm() }, "+ Reminder") : null);

  const tot = w ? Math.max(1, w.in_cents + w.out_cents) : 1;
  const weekCard = w ? card("This week", "week",
    el("div", { class: "muted small" }, `${short(w.start)} – ${short(w.end)}`),
    el("div", { class: "trio" },
      el("div", {}, el("small", {}, "In"), el("strong", { class: "in" }, money(w.in_cents))),
      el("div", {}, el("small", {}, "Out"), el("strong", { class: "out" }, money(w.out_cents))),
      el("div", {}, el("small", {}, "Left over"), el("strong", { class: w.net_cents < 0 ? "out" : "in" }, money(w.net_cents)))),
    el("div", { class: "split", "aria-hidden": "true" },
      el("i", { style: `width:${(w.in_cents / tot) * 100}%;background:var(--in)` }),
      el("i", { style: `width:${(w.out_cents / tot) * 100}%;background:var(--out)` })),
    el("div", { class: "meta" }, [`Still to pay ${money(w.unpaid_cents)}`, w.ot_hours ? `OT ${w.ot_hours}h` : null,
      h.next_pay ? `next payday ${short(h.next_pay.date)} (${money(h.next_pay.amount_cents)})` : null].filter(Boolean).join(" · "))) : null;

  const upcoming = h.upcoming ? card("Coming up (14 days)", "cal",
    h.upcoming.length ? el("ul", { class: "up-list" }, h.upcoming.map((b) => el("li", {},
      el("span", { class: "d" }, short(b.date)),
      el("span", { class: "n" }, el("i", { class: "dot", style: `background:${b.account_id ? acctColor(b.account_id) : "var(--ink-2)"}` }), b.name,
        b.autopay ? el("span", { class: "tag autopay" }, "Autopay") : null),
      el("span", { class: "amt out" }, money(b.amount_cents))))) : el("p", { class: "muted" }, "Nothing due in the next two weeks."),
    h.upcoming.length ? el("div", { class: "meta" }, `${money(h.upcoming_total_cents)} due in the next 14 days`) : null) : null;

  const a = h.accounts;
  const acctCard = a ? card("Accounts", "accounts",
    a.has_accounts ? [
      el("div", { class: "trio two" },
        el("div", {}, el("small", {}, "Cash in bank"), el("strong", {}, money(a.cash_cents)),
          el("div", { class: "meta" }, `≈ ${money(a.projected_cash_cents)} by ${short(a.week_end)}`)),
        el("div", {}, el("small", {}, "Card balances"), el("strong", { class: a.card_balance_cents ? "out" : "" }, money(a.card_balance_cents)),
          a.card_limit_cents ? el("div", { class: "meta" }, `of ${money(a.card_limit_cents)}`) : null)),
      a.card_utilization !== null ? utilBar(a.card_utilization, 30, "Overall credit utilization") : null,
    ] : el("p", { class: "muted" }, "Add your checking, savings and cards to see balances here.")) : null;

  const nw = h.networth;
  const nwCard = nw ? card("Net worth", "networth",
    el("div", { class: "spread" },
      el("div", {}, el("div", { class: `big ${nw.net_cents < 0 ? "out" : ""}` }, money(nw.net_cents)),
        nw.change_30d !== undefined && nw.change_30d !== null
          ? el("span", { class: `chg ${nw.change_30d > 0 ? "up" : nw.change_30d < 0 ? "down" : ""}` },
            `${nw.change_30d > 0 ? "▲" : nw.change_30d < 0 ? "▼" : "•"} ${money(Math.abs(nw.change_30d))} `, el("small", {}, "30 days"))
          : el("span", { class: "meta" }, `Assets ${money(nw.assets_cents)} · debts ${money(nw.liabilities_cents)}`)),
      spark(nw.history))) : null;

  const trCard = h.trackers?.length ? card("Trackers", "trackers", el("ul", { class: "plain" }, h.trackers.map((t) => el("li", {},
    el("div", { class: "spread" }, el("span", {}, t.name),
      el("span", { class: "amt small" }, t.kind === "debt" ? `${money(t.current_cents)} left` : `${money(t.current_cents)} / ${money(t.target_cents)}`)),
    progressBar(t.progress, t.color))))) : null;

  const invCard = h.investments && (h.investments.value_cents || h.investments.cost_cents) ? card("Investments", "investments",
    el("div", { class: "trio two" },
      el("div", {}, el("small", {}, "Value"), el("strong", {}, money(h.investments.value_cents))),
      el("div", {}, el("small", {}, "Gain / loss"), el("strong", { class: h.investments.gain_cents < 0 ? "out" : "in" }, money(h.investments.gain_cents)),
        h.investments.cost_cents ? el("div", { class: "meta" }, `${((h.investments.gain_cents / h.investments.cost_cents) * 100).toFixed(1)}%`) : null))) : null;

  const remCard = h.reminders?.length ? card("Reminders this week", "alerts", el("ul", { class: "up-list" }, h.reminders.map((r) => el("li", {},
    el("span", { class: "d" }, short(r.date)), el("span", { class: "n" }, "⏰ ", r.title), el("span", { class: "amt muted" }, r.time))))) : null;

  fill(root,
    el("div", { class: "hello" }, el("div", {}, el("h2", {}, greeting(h.name)),
      el("div", { class: "muted" }, fmtDay(h.today, { weekday: "long", month: "long", day: "numeric", year: "numeric" }))), quick),
    attention(h),
    el("div", { class: "hgrid" }, weekCard, upcoming, acctCard, nwCard, trCard, invCard, remCard));
}
