import { api, can, el, emptyState, fmtDay, money, onContext, REC_LABEL, acctName, toast, refresh, fill, S } from "../core.js";
import { billForm } from "../forms.js";

export const title = "Bills";

export async function render(root) {
  const bills = await api("GET", "/api/bills");
  const w = can("finance:write");
  const monthly = bills.filter((b) => b.active).reduce((s, b) => s + b.amount_cents * ({ weekly: 52 / 12, biweekly: 26 / 12,
    semimonthly: 2, monthly: 1, quarterly: 1 / 3, yearly: 1 / 12, once: 0 }[b.recurrence] || 0), 0);
  const list = el("ul", { class: "list" }, bills.map((b) => {
    const li = el("li", {}, el("button", { type: "button", class: "row", onclick: () => billForm(b) },
      el("span", {}, el("span", {}, b.active ? b.name : `${b.name} (paused)`), el("div", { class: "meta" },
        [REC_LABEL[b.recurrence], `from ${fmtDay(b.start_date, { month: "short", day: "numeric", year: "numeric" })}`,
          acctName(b.account_id), b.category, b.autopay ? "autopay" : "",
          b.end_date ? `${b.end_date < S.me.today ? "ENDED" : "stops after"} ${fmtDay(b.end_date, { month: "short", day: "numeric", year: "numeric" })}` : ""].filter(Boolean).join(" · "))),
      el("span", { class: "amt" }, money(b.amount_cents))));
    onContext(li, () => [
      { label: w ? "Edit…" : "View…", action: () => billForm(b) },
      w ? { label: b.active ? "Pause" : "Resume", action: () => api("PUT", `/api/bills/${b.id}`, { ...strip(b), active: !b.active }).then(refresh).catch((e) => toast(e.message, true)) } : null,
      w ? { label: "Duplicate", action: () => api("POST", "/api/bills", { ...strip(b), name: b.name + " (copy)" }).then(refresh) } : null,
      w ? "-" : null,
      w ? { label: "Delete", danger: true, action: () => api("DELETE", `/api/bills/${b.id}`).then(refresh) } : null,
    ]);
    return li;
  }));
  fill(root, 
    el("p", { class: "muted" }, `${bills.filter((b) => b.active).length} active bills ≈ ${money(Math.round(monthly))} a month, ${money(Math.round(monthly * 12 / 52))} a week.`),
    bills.length ? list : emptyState("No bills yet.", w ? "Add a bill" : null, () => billForm()),
    w && bills.length ? el("button", { type: "button", class: "primary", onclick: () => billForm() }, "Add bill") : null);
}

function strip(b) {
  const { id, ...rest } = b;
  return { name: rest.name, amount_cents: rest.amount_cents, account_id: rest.account_id, start_date: rest.start_date,
    recurrence: rest.recurrence, end_date: rest.end_date, autopay: !!rest.autopay, remind_days: rest.remind_days,
    category: rest.category, notes: rest.notes, active: !!rest.active };
}
