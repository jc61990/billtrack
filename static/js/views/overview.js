import { addDays, api, barChart, can, el, fmtTs, hbars, KIND_LABEL, kpis, money, onContext, progressBar, S, weekStartOf , fill } from "../core.js";
import { accountForm } from "../forms.js";

export const title = "Overview";
const RANGES = [["week", "This week"], ["month", "This month"], ["3m", "Last 3 months"], ["ytd", "Year to date"],
  ["12m", "Last 12 months"], ["custom", "Custom"]];
let range = "month", custom = null;

function bounds() {
  const t = S.me.today, y = t.slice(0, 4), m = t.slice(0, 7);
  switch (range) {
    case "week": { const s = weekStartOf(t); return [s, addDays(s, 6), "week"]; }
    case "month": { const n = new Date(Date.UTC(+y, +t.slice(5, 7), 0)).getUTCDate(); return [`${m}-01`, `${m}-${String(n).padStart(2, "0")}`, "week"]; }
    case "3m": return [addDays(t, -91), t, "week"];
    case "ytd": return [`${y}-01-01`, t, "month"];
    case "12m": return [addDays(t, -365), t, "month"];
    default: return [custom?.[0] || addDays(t, -30), custom?.[1] || t, custom?.[2] || "week"];
  }
}

export async function render(root) {
  const [start, end, group] = bounds();
  const o = await api("GET", `/api/reports/overview?start=${start}&end=${end}&group=${group}`);
  const picker = el("div", { class: "seg", role: "tablist" }, RANGES.map(([k, l]) =>
    el("button", { type: "button", role: "tab", "aria-selected": String(range === k), onclick: () => { range = k; render(root); } }, l)));
  const customRow = range === "custom" ? el("div", { class: "row3" },
    el("label", {}, "From", el("input", { type: "date", value: start, onchange: (e) => { custom = [e.target.value, end, group]; render(root); } })),
    el("label", {}, "To", el("input", { type: "date", value: end, onchange: (e) => { custom = [start, e.target.value, group]; render(root); } })),
    el("label", {}, "Group by", el("select", { onchange: (e) => { custom = [start, end, e.target.value]; render(root); } },
      el("option", { value: "week", selected: group === "week" }, "Week"), el("option", { value: "month", selected: group === "month" }, "Month")))) : null;
  const spend = o.bills_cents + o.expenses_cents;
  fill(root, picker, customRow,
    el("p", { class: "muted" }, `${start} to ${end}`),
    kpis([
      ["Income", money(o.in_cents), "in", `Pay ${money(o.pay_cents)} · Extra ${money(o.extra_cents)}`],
      ["Spending", money(o.out_cents), "out", `Bills ${money(o.bills_cents)} · Other ${money(o.expenses_cents)}`],
      ["Left over", money(o.net_cents), o.net_cents < 0 ? "out" : "in", o.ot_hours ? `OT ${o.ot_hours}h = ${money(o.ot_cents)}` : null],
      ["Net worth", money(o.net_worth_cents), o.net_worth_cents < 0 ? "out" : "", "Accounts" + (o.investments ? " + investments" : "")],
    ]),
    el("section", { class: "card" }, el("h2", {}, `Money in and out by ${group}`),
      barChart(o.series, { label: (x) => group === "month" ? x.label.slice(0, 3) : x.label.slice(5) })),
    el("div", { class: "grid2" },
      el("section", { class: "card" }, el("h2", {}, "Spending by category"),
        o.spending_by_category.length ? hbars(o.spending_by_category.map((c) => ({ label: c.category, cents: c.cents })), spend)
          : el("p", { class: "muted" }, "No spending in this range")),
      el("section", { class: "card" }, el("h2", {}, "Income by type"),
        o.income_by_kind.length ? hbars(o.income_by_kind.map((c) => ({ label: c.kind === "pay" ? "Pay" : KIND_LABEL[c.kind] || c.kind, cents: c.cents })), o.in_cents)
          : el("p", { class: "muted" }, "No income in this range"))),
    accountsCard(o),
    o.trackers?.length ? el("section", { class: "card" }, el("h2", {}, "Trackers"), el("ul", { class: "plain" },
      o.trackers.map((t) => el("li", {}, el("div", { class: "spread" }, el("span", {}, t.name),
        el("span", { class: "amt" }, `${money(t.current_cents)}${t.kind !== "debt" && t.target_cents ? " / " + money(t.target_cents) : " left"}`)),
        progressBar(t.progress, "#1f7a4d"))))) : null,
    o.investments ? el("section", { class: "card" }, el("h2", {}, "Investments"),
      kpis([["Value", money(o.investments.value_cents)], ["Cost basis", money(o.investments.cost_cents)],
        ["Gain", money(o.investments.gain_cents), o.investments.gain_cents < 0 ? "out" : "in"]])) : null);
}

function accountsCard(o) {
  const w = can("finance:write");
  const rows = o.accounts.map((a) => {
    const tr = el("tr", { tabindex: 0, class: w ? "click" : "", onclick: () => w && accountForm(a) },
      el("td", {}, a.name), el("td", {}, a.kind), el("td", { class: "r" + (["credit", "loan"].includes(a.kind) ? " out" : "") }, money(a.balance_cents)),
      el("td", { class: "muted small" }, a.balance_updated ? fmtTs(a.balance_updated) : "—"));
    onContext(tr, () => [w ? { label: "Edit / update balance…", action: () => accountForm(a) } : null]);
    return tr;
  });
  return el("section", { class: "card" },
    el("div", { class: "spread" }, el("h2", {}, "Accounts and net worth"), w ? el("button", { type: "button", onclick: () => accountForm() }, "Add account") : null),
    el("div", { class: "scroll-x" }, el("table", { class: "tbl" },
      el("tr", {}, el("th", {}, "Account"), el("th", {}, "Type"), el("th", { class: "r" }, "Balance"), el("th", {}, "Updated")),
      rows.length ? rows : el("tr", {}, el("td", { colspan: 4 }, "Add your accounts to track balances and net worth")))),
    kpis([["Assets", money(o.assets_cents), "in"], ["Debts", money(o.debts_cents), "out"],
      o.investments ? ["Investments", money(o.investments.value_cents)] : null, ["Net worth", money(o.net_worth_cents)]]));
}
