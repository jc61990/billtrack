import { api, can, el, emptyState, fmtDay, money, onContext, REC_LABEL, acctName, toast, refresh, fill, S } from "../core.js";
import { billForm } from "../forms.js";

export const title = "Bills";

const usd = (c) => money(c);
function suggestionText(s) {
  const p = s.payload;
  if (s.kind === "amount") return [`${p.name} was charged ${usd(p.to)}`, `Expected ${usd(p.from)}. Update the bill to ${usd(p.to)}?`];
  if (s.kind === "due_day") return [`${p.name} is always charged on the ${ord(p.to)}`, `It's set for the ${ord(p.from)}. Move it?`];
  return [`Looks like a bill: ${p.name}`, `${usd(p.amount_cents)} ${REC_LABEL[p.recurrence].toLowerCase()}, seen ${p.seen} times`
    + `${p.amount_mode === "varies" ? " (amount varies)" : ""}. Next around ${fmtDay(p.start_date, { month: "short", day: "numeric" })}.`];
}
const ord = (n) => n + (["th", "st", "nd", "rd"][((n % 100) - 20) % 10] || ["th", "st", "nd", "rd"][n % 100] || "th");

async function suggestionsCard() {
  const list = await api("GET", "/api/bills/suggestions").catch(() => []);
  if (!list.length || !can("finance:write")) return null;
  const act = (s, what) => api("POST", `/api/bills/suggestions/${s.id}/${what}`)
    .then(() => { toast(what === "accept" ? "Done" : "Dismissed"); refresh(); }).catch((e) => toast(e.message, true));
  return el("section", { class: "card suggest" }, el("h2", {}, `Suggestions from your bank (${list.length})`),
    el("ul", { class: "list" }, list.map((s) => {
      const [title, sub] = suggestionText(s);
      return el("li", {}, el("span", {}, el("strong", {}, title), el("div", { class: "meta" }, sub)),
        el("span", { class: "actions left" },
          el("button", { type: "button", class: "primary", onclick: () => act(s, "accept") }, s.kind === "new_bill" ? "Add bill" : s.kind === "due_day" ? "Move it" : "Update"),
          el("button", { type: "button", onclick: () => act(s, "dismiss") }, "Dismiss")));
    })));
}

export async function render(root) {
  const sugg = await suggestionsCard();
  const bills = await api("GET", "/api/bills");
  const w = can("finance:write");
  const monthly = bills.filter((b) => b.active).reduce((s, b) => s + b.amount_cents * ({ weekly: 52 / 12, biweekly: 26 / 12,
    semimonthly: 2, monthly: 1, quarterly: 1 / 3, yearly: 1 / 12, once: 0 }[b.recurrence] || 0), 0);
  const list = el("ul", { class: "list" }, bills.map((b) => {
    const li = el("li", {}, el("button", { type: "button", class: "row", onclick: () => billForm(b) },
      el("span", {}, el("span", {}, b.active ? b.name : `${b.name} (paused)`), el("div", { class: "meta" },
        [REC_LABEL[b.recurrence], `from ${fmtDay(b.start_date, { month: "short", day: "numeric", year: "numeric" })}`,
          acctName(b.account_id), b.category, b.autopay ? "autopay" : "", b.amount_mode === "varies" ? "amount varies" : "",
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
  fill(root, sugg, 
    el("p", { class: "muted" }, `${bills.filter((b) => b.active).length} active bills ≈ ${money(Math.round(monthly))} a month, ${money(Math.round(monthly * 12 / 52))} a week.`),
    bills.length ? list : emptyState("No bills yet.", w ? "Add a bill" : null, () => billForm()),
    w && bills.length ? el("button", { type: "button", class: "primary", onclick: () => billForm() }, "Add bill") : null);
}

function strip(b) {
  const { id, ...rest } = b;
  return { name: rest.name, amount_cents: rest.amount_cents, account_id: rest.account_id, start_date: rest.start_date,
    recurrence: rest.recurrence, end_date: rest.end_date, autopay: !!rest.autopay, remind_days: rest.remind_days,
    category: rest.category, notes: rest.notes, active: !!rest.active,
    amount_mode: rest.amount_mode || "fixed", match_name: rest.match_name || "" };
}
