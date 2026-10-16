import { $, addDays, api, barChart, can, el, fmtDay, kpis, money, onContext, S , fill } from "../core.js";
import { expenseForm, extraForm, billForm, reminderForm } from "../forms.js";
import { collect, isIn, itemRow } from "../items.js";

export const title = "This week";

export function dayMenu(d) {
  const w = can("finance:write");
  return [
    w ? { label: "Add bill due this day…", action: () => billForm({}, d) } : null,
    w ? { label: "Log overtime / extra pay…", action: () => extraForm({}, d) } : null,
    w ? { label: "Log spending…", action: () => expenseForm({}, d) } : null,
    can("reminders") ? { label: "Add reminder…", action: () => reminderForm({}, d) } : null,
  ];
}

export async function render(root) {
  const [w, trend] = await Promise.all([api("GET", `/api/week?day=${S.day}`), api("GET", "/api/trend?back=8&ahead=4")]);
  const items = collect(w);
  const days = [];
  let run = 0;
  for (let i = 0; i < 7; i++) {
    const d = addDays(w.start, i);
    const todays = items.filter((x) => x.date === d).sort((a, b) => Number(isIn(b)) - Number(isIn(a)));
    todays.forEach((x) => { run += (isIn(x) ? 1 : -1) * x.amount_cents; });
    const li = el("li", { class: "day" + (d === S.me.today ? " today" : ""), dataset: { dropDate: d } },
      el("div", { class: "day-h" }, el("span", {}, fmtDay(d)),
        el("span", { class: "run" + (run < 0 ? " out" : "") }, `Running ${money(run)}`)),
      todays.length ? todays.map(itemRow) : el("div", { class: "empty" }, "Nothing scheduled"));
    onContext(li, () => dayMenu(d));
    days.push(li);
  }
  const tot = Math.max(1, w.in_cents + w.out_cents);
  fill(root, 
    el("div", { class: "weeknav" },
      el("button", { type: "button", "aria-label": "Previous week", onclick: () => { S.day = addDays(S.day, -7); render(root); } }, "‹"),
      el("button", { type: "button", class: "weeklabel", onclick: () => { S.day = S.me.today; render(root); } },
        `${fmtDay(w.start, { month: "short", day: "numeric" })} – ${fmtDay(w.end, { month: "short", day: "numeric" })}`),
      el("button", { type: "button", "aria-label": "Next week", onclick: () => { S.day = addDays(S.day, 7); render(root); } }, "›")),
    kpis([
      ["Money in", money(w.in_cents), "in", `Pay ${money(w.pay_cents)} · OT/extra ${money(w.extra_cents)}${w.ot_hours ? ` (${w.ot_hours}h)` : ""}`],
      ["Money out", money(w.out_cents), "out", `Bills ${money(w.bills_cents)} · Other ${money(w.expenses_cents)}`],
      ["Left over", money(w.net_cents), w.net_cents < 0 ? "out" : "in", `Still to pay ${money(w.unpaid_cents)}`],
    ]),
    el("div", { class: "split", "aria-hidden": "true" },
      el("i", { style: `width:${(w.in_cents / tot) * 100}%;background:var(--in)` }),
      el("i", { style: `width:${(w.out_cents / tot) * 100}%;background:var(--out)` })),
    el("p", { class: "hint" }, can("finance:write") ? "Drag items to another day. Right-click or long-press for options." : ""),
    el("ol", { class: "days" }, days),
    el("h2", {}, "By account"),
    el("div", { class: "scroll-x" }, el("table", { class: "tbl" },
      el("tr", {}, el("th", {}, "Account"), el("th", { class: "r" }, "In"), el("th", { class: "r" }, "Out"), el("th", { class: "r" }, "Net")),
      w.by_account.length ? w.by_account.map((a) => el("tr", {}, el("td", {}, a.account),
        el("td", { class: "r in" }, money(a.in_cents)), el("td", { class: "r out" }, money(a.out_cents)),
        el("td", { class: "r" }, money(a.in_cents - a.out_cents)))) : el("tr", {}, el("td", { colspan: 4 }, "No activity this week")))),
    el("h2", {}, "Last 8 weeks and next 4"),
    barChart(trend, { label: (x) => fmtDay(x.start, { month: "numeric", day: "numeric" }) }));
}
