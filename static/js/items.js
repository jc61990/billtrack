// Ledger items (bill / pay / extra / expense): rows, chips, context menus, drag-to-reschedule. Shared by Week + Calendar.
import { api, can, draggable, el, KIND_LABEL, money, moneyShort, onContext, refresh, safeColor, showMenu, toast } from "./core.js";
import { billForm, expenseForm, extraForm, moveForm, paidForm, receivedForm, reminderForm, sourceForm } from "./forms.js";

const W = () => can("finance:write");
const run = (p, msg) => p.then(() => { if (msg) toast(msg); refresh(); }).catch((e) => toast(e.message, true));

export function move(item, date) {
  if (item.type === "bill" || item.type === "pay") {
    const kind = item.type, id = kind === "bill" ? item.bill_id : item.source_id;
    return run(api("PUT", "/api/overrides", { kind, item_id: id, orig_date: item.orig_date,
      new_date: date === item.orig_date ? null : date }), `Moved to ${date}`);
  }
  const path = item.type === "extra" ? "/api/income-extra" : "/api/expenses";
  const { id, date: _d, account, source, type, ...rest } = item;
  const body = item.type === "extra"
    ? { date, kind: rest.kind, source_id: rest.source_id, account_id: rest.account_id, hours: rest.hours, amount_cents: rest.amount_cents, note: rest.note }
    : { date, name: rest.name, amount_cents: rest.amount_cents, account_id: rest.account_id, category: rest.category };
  return run(api("PUT", `${path}/${id}`, body), `Moved to ${date}`);
}

const skip = (item, skipped) => run(api("PUT", "/api/overrides", {
  kind: item.type, item_id: item.type === "bill" ? item.bill_id : item.source_id, orig_date: item.orig_date,
  new_date: skipped ? null : (item.moved ? item.date : null), skipped }), skipped ? "Skipped this one" : "Restored");

const reset = (item) => run(api("PUT", "/api/overrides", { kind: item.type,
  item_id: item.type === "bill" ? item.bill_id : item.source_id, orig_date: item.orig_date }), "Back on its usual date");

async function openBill(item) { const b = (await api("GET", "/api/bills")).find((x) => x.id === item.bill_id); if (b) billForm(b); }
async function openSource(item) { const s = (await api("GET", "/api/income-sources")).find((x) => x.id === item.source_id); if (s) sourceForm(s); }

export function menuFor(item) {
  const w = W();
  if (item.type === "bill") return [
    item.status === "paid"
      ? { label: "Mark unpaid", disabled: !w, action: () => run(api("DELETE", `/api/payments/${item.payment_id}`), "Marked unpaid") }
      : { label: "Mark paid", disabled: !w || item.skipped, action: () => run(api("POST", "/api/payments", { bill_id: item.bill_id, due_date: item.orig_date }), "Marked paid") },
    { label: "Paid a different amount…", disabled: !w || item.skipped, action: () => paidForm(item) },
    { label: "Move this one…", disabled: !w, action: () => moveForm(item, (d) => move(item, d)) },
    item.moved ? { label: "Move back to usual date", disabled: !w, action: () => reset(item) } : null,
    item.skipped ? { label: "Un-skip", disabled: !w, action: () => skip(item, false) }
      : { label: "Skip this one", disabled: !w, action: () => skip(item, true) },
    "-",
    { label: can("finance:write") ? "Edit bill…" : "View bill…", action: () => openBill(item) },
    can("reminders") ? { label: "Remind me…", action: () => reminderForm({ title: `Pay ${item.name}` }, item.date) } : null,
  ];
  if (item.type === "pay") return [
    item.received
      ? { label: "Undo received", disabled: !w, action: () => run(api("DELETE", `/api/received/${item.received_id}`)) }
      : { label: "Got paid…", disabled: !w || item.skipped, action: () => receivedForm(item) },
    { label: "Move this payday…", disabled: !w, action: () => moveForm(item, (d) => move(item, d)) },
    item.moved ? { label: "Move back to usual date", disabled: !w, action: () => reset(item) } : null,
    item.skipped ? { label: "Un-skip", disabled: !w, action: () => skip(item, false) }
      : { label: "Skip this payday", disabled: !w, action: () => skip(item, true) },
    "-", { label: "Edit pay…", action: () => openSource(item) },
  ];
  const isExtra = item.type === "extra";
  const path = isExtra ? "/api/income-extra" : "/api/expenses";
  return [
    { label: "Edit…", action: () => (isExtra ? extraForm(item) : expenseForm(item)) },
    { label: "Move…", disabled: !w, action: () => moveForm(item, (d) => move(item, d)) },
    { label: "Duplicate", disabled: !w, action: () => run(api("POST", path, isExtra
      ? { date: item.date, kind: item.kind, source_id: item.source_id, account_id: item.account_id, hours: item.hours, amount_cents: item.amount_cents, note: item.note }
      : { date: item.date, name: item.name, amount_cents: item.amount_cents, account_id: item.account_id, category: item.category }), "Duplicated") },
    "-", { label: "Delete", danger: true, disabled: !w, action: () => run(api("DELETE", `${path}/${item.id}`), "Deleted") },
  ];
}

export const itemName = (it) => it.type === "extra" ? (KIND_LABEL[it.kind] || it.kind) + (it.hours ? ` ${it.hours}h` : "") : it.name;
const isIn = (it) => it.type === "pay" || it.type === "extra";

function attach(node, item) {
  onContext(node, () => menuFor(item));
  if (W()) draggable(node, { label: `${itemName(item)} ${money(item.amount_cents)}`, date: item.date, onDrop: (d) => move(item, d) });
}

/** Full row for the week register. */
export function itemRow(item) {
  let check = el("span");
  if (item.type === "bill" || item.type === "pay") {
    const done = item.type === "bill" ? item.status === "paid" : item.received;
    check = el("input", { type: "checkbox", checked: done, disabled: !W() || item.skipped,
      "aria-label": `${item.type === "bill" ? "Paid" : "Received"}: ${item.name}` });
    check.addEventListener("change", () => {
      if (item.type === "bill") {
        run(check.checked ? api("POST", "/api/payments", { bill_id: item.bill_id, due_date: item.orig_date })
          : api("DELETE", `/api/payments/${item.payment_id}`), check.checked ? "Marked paid" : "Marked unpaid");
      } else if (check.checked) { check.checked = false; receivedForm(item); }
      else run(api("DELETE", `/api/received/${item.received_id}`));
    });
  }
  const tagMap = { overdue: "Overdue", "due-soon": "Due soon", autopay: "Autopay", skipped: "Skipped" };
  const meta = item.type === "bill" ? [el("span", { class: "dot", style: `background:${safeColor(item.account_color)}` }), item.account || "No account"]
    : item.type === "pay" ? [(item.received ? "Received" : "Expected") + (item.account ? ` → ${item.account}` : "")]
      : [[item.source, item.account, item.category, item.note].filter(Boolean).join(" · ")];
  const row = el("div", { class: `item${item.status === "paid" || item.received ? " done" : ""}${item.skipped ? " skipped" : ""}`, tabindex: 0 },
    check,
    el("div", { class: "body" }, el("span", { class: "name" }, itemName(item)),
      tagMap[item.status] ? el("span", { class: `tag ${item.status}` }, tagMap[item.status]) : null,
      item.moved ? el("span", { class: "tag moved", title: `Usually ${item.orig_date}` }, "Moved") : null,
      el("div", { class: "meta" }, meta)),
    el("span", { class: `amt ${isIn(item) ? "in" : "out"}` }, (isIn(item) ? "+" : "−") + money(item.amount_cents)),
    el("button", { type: "button", class: "kebab", "aria-label": `Options for ${itemName(item)}`, onclick: (e) => {
      const r = e.currentTarget.getBoundingClientRect(); showMenu(r.left - 150, r.bottom, menuFor(item));
    } }, "⋯"));
  row.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === row) row.querySelector(".kebab").click(); });
  attach(row, item);
  return row;
}

/** Compact chip for calendar cells. */
export function itemChip(item) {
  const cls = isIn(item) ? "in" : item.status === "overdue" ? "over" : item.status === "paid" ? "paid" : "out";
  const chip = el("button", { type: "button", class: `chip ${cls}${item.skipped ? " skipped" : ""}`,
    title: `${itemName(item)} ${money(item.amount_cents)}${item.moved ? ` (moved from ${item.orig_date})` : ""}`,
    onclick: (e) => { const r = e.currentTarget.getBoundingClientRect(); showMenu(r.left, r.bottom, menuFor(item)); } },
  el("span", { class: "cn" }, itemName(item)), el("span", { class: "ca" }, moneyShort(item.amount_cents)));
  attach(chip, item);
  return chip;
}

export function collect(data) {
  return [
    ...data.pay.map((x) => ({ ...x, type: "pay" })),
    ...data.extras.map((x) => ({ ...x, type: "extra" })),
    ...data.bills.map((x) => ({ ...x, type: "bill" })),
    ...data.expenses.map((x) => ({ ...x, type: "expense" })),
  ];
}
export { isIn };
