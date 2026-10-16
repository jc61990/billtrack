import { acctName, addDays, api, can, el, emptyState, fmtDay, KIND_LABEL, money, onContext, REC_LABEL, S , fill } from "../core.js";
import { expenseForm, extraForm, sourceForm } from "../forms.js";

export const title = "Income and spending";

function row(main, meta, right, open, menu) {
  const li = el("li", {}, el("button", { type: "button", class: "row", onclick: open },
    el("span", {}, el("span", {}, main), el("div", { class: "meta" }, meta)), el("span", { class: "amt" }, right)));
  if (menu) onContext(li, menu);
  return li;
}

export async function render(root) {
  const w = can("finance:write");
  const lo = addDays(S.me.today, -120), hi = addDays(S.me.today, 30);
  const [ex, exp] = await Promise.all([api("GET", `/api/income-extra?start=${lo}&end=${hi}`), api("GET", `/api/expenses?start=${lo}&end=${hi}`)]);
  const otH = ex.filter((x) => x.kind === "overtime").reduce((s, x) => s + (x.hours || 0), 0);
  const otC = ex.filter((x) => x.kind === "overtime").reduce((s, x) => s + x.amount_cents, 0);
  fill(root, 
    el("div", { class: "spread" }, el("h2", {}, "Pay"), w ? el("button", { type: "button", onclick: () => sourceForm() }, "Add pay") : null),
    S.sources.length ? el("ul", { class: "list" }, S.sources.map((s) => row(s.name,
      [REC_LABEL[s.recurrence], acctName(s.account_id), s.hourly_rate_cents ? `${money(s.hourly_rate_cents)}/h, OT ×${s.ot_multiplier}` : ""].filter(Boolean).join(" · "),
      money(s.amount_cents), () => sourceForm(s)))) : emptyState("Add your paycheck to see money in each week.", w ? "Add pay" : null, () => sourceForm()),
    el("div", { class: "spread" }, el("h2", {}, "Overtime and extra income"), w ? el("button", { type: "button", onclick: () => extraForm() }, "Log OT / extra") : null),
    el("p", { class: "muted" }, `Last 120 days: ${otH} OT hours = ${money(otC)}`),
    el("ul", { class: "list" }, ex.map((x) => row(`${KIND_LABEL[x.kind]}${x.hours ? ` · ${x.hours}h` : ""}`,
      [fmtDay(x.date), x.note].filter(Boolean).join(" · "), "+" + money(x.amount_cents), () => extraForm(x)))),
    el("div", { class: "spread" }, el("h2", {}, "Other spending"), w ? el("button", { type: "button", onclick: () => expenseForm() }, "Log spending") : null),
    el("ul", { class: "list" }, exp.map((x) => row(x.name, [fmtDay(x.date), acctName(x.account_id), x.category].filter(Boolean).join(" · "),
      "−" + money(x.amount_cents), () => expenseForm(x)))));
}
