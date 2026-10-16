// Entity forms shared by several views. Field names match the API models exactly (unknown fields are rejected).
import { api, KIND_LABEL, openForm, REC, REC_LABEL, S } from "./core.js";

const recOpts = () => REC.map((r) => [r, REC_LABEL[r]]);
export const acctOptions = () => [["", "No account"], ...S.accounts.map((a) => [a.id, a.name])];
const srcOptions = () => [["", "None"], ...S.sources.map((s) => [s.id, s.name])];
const save = (path, id) => (d) => id ? api("PUT", `${path}/${id}`, d) : api("POST", path, d);
const del = (path, id) => id ? () => api("DELETE", `${path}/${id}`) : null;

export function billForm(b = {}, presetDate) {
  openForm(b.id ? "Edit bill" : "Add bill", [
    { name: "name", label: "Bill", value: b.name, required: true, placeholder: "Electric" },
    { name: "amount_cents", label: "Amount", type: "money", value: b.amount_cents, required: true, pair: 1 },
    { name: "account_id", label: "Comes out of", type: "select", int: 1, options: acctOptions(), value: b.account_id ?? "", pair: 1 },
    { name: "start_date", label: "Next (or first) due date", type: "date", value: b.start_date || presetDate || S.me.today, required: true, pair: 1 },
    { name: "recurrence", label: "Repeats", type: "select", options: recOpts(), value: b.recurrence || "monthly", pair: 1 },
    { name: "remind_days", label: "Remind days before", type: "number", min: 0, max: 30, value: b.remind_days ?? 3, pair: 1 },
    { name: "category", label: "Category", value: b.category, placeholder: "Utilities", max: 60, pair: 1 },
    { name: "end_date", label: "Last payment (optional)", type: "date", value: b.end_date, nullable: 1 },
    { name: "autopay", label: "Autopay (no overdue alerts)", type: "check", value: b.autopay },
    { name: "active", label: "Active", type: "check", value: b.id ? b.active : true },
    { name: "notes", label: "Notes", type: "textarea", value: b.notes },
  ], save("/api/bills", b.id), del("/api/bills", b.id));
}

export function sourceForm(s = {}) {
  openForm(s.id ? "Edit pay" : "Add pay", [
    { name: "name", label: "Name", value: s.name, required: true, placeholder: "Paycheck" },
    { name: "amount_cents", label: "Take-home per check", type: "money", value: s.amount_cents, required: true, pair: 1 },
    { name: "account_id", label: "Deposits to", type: "select", int: 1, options: acctOptions(), value: s.account_id ?? "", pair: 1 },
    { name: "start_date", label: "Next (or first) payday", type: "date", value: s.start_date || S.me.today, required: true, pair: 1 },
    { name: "recurrence", label: "Paid", type: "select", options: recOpts(), value: s.recurrence || "weekly", pair: 1 },
    { name: "hourly_rate_cents", label: "Hourly rate (for OT)", type: "money", value: s.hourly_rate_cents ?? 0, zero: 1, pair: 1 },
    { name: "ot_multiplier", label: "OT multiplier", type: "number", step: "0.05", min: 1, max: 4, value: s.ot_multiplier ?? 1.5, pair: 1 },
    { name: "end_date", label: "Ends (optional)", type: "date", value: s.end_date, nullable: 1 },
    { name: "active", label: "Active", type: "check", value: s.id ? s.active : true },
  ], save("/api/income-sources", s.id), del("/api/income-sources", s.id));
}

export function extraForm(x = {}, presetDate) {
  openForm(x.id ? "Edit extra income" : "Log overtime or extra pay", [
    { name: "kind", label: "Type", type: "select", options: Object.entries(KIND_LABEL), value: x.kind || "overtime", pair: 1 },
    { name: "date", label: "Paid on", type: "date", value: x.date || presetDate || S.me.today, required: true, pair: 1 },
    { name: "source_id", label: "Job", type: "select", int: 1, options: srcOptions(), value: x.source_id ?? (S.sources[0]?.id ?? ""), pair: 1 },
    { name: "hours", label: "Hours", type: "number", step: "0.25", min: 0, max: 200, value: x.hours ?? "", nullable: 1, pair: 1 },
    { name: "amount_cents", label: "Amount", type: "money", value: x.amount_cents,
      hint: "Leave blank for overtime to calculate hours × rate × multiplier." },
    { name: "account_id", label: "Deposits to", type: "select", int: 1, options: acctOptions(), value: x.account_id ?? "" },
    { name: "note", label: "Note", type: "textarea", value: x.note },
  ], save("/api/income-extra", x.id), del("/api/income-extra", x.id));
}

export function expenseForm(x = {}, presetDate) {
  openForm(x.id ? "Edit spending" : "Log other spending", [
    { name: "name", label: "What", value: x.name, required: true, placeholder: "Groceries" },
    { name: "amount_cents", label: "Amount", type: "money", value: x.amount_cents, required: true, pair: 1 },
    { name: "date", label: "Date", type: "date", value: x.date || presetDate || S.me.today, required: true, pair: 1 },
    { name: "account_id", label: "From account", type: "select", int: 1, options: acctOptions(), value: x.account_id ?? "", pair: 1 },
    { name: "category", label: "Category", value: x.category, max: 60, pair: 1 },
  ], save("/api/expenses", x.id), del("/api/expenses", x.id));
}

export function accountForm(a = {}) {
  openForm(a.id ? "Edit account" : "Add account", [
    { name: "name", label: "Account name", value: a.name, required: true, placeholder: "Checking" },
    { name: "kind", label: "Type", type: "select", options: [["checking", "Checking"], ["savings", "Savings"], ["cash", "Cash"],
      ["credit", "Credit card"], ["loan", "Loan"], ["other", "Other"]], value: a.kind || "checking", pair: 1 },
    { name: "color", label: "Color", type: "color", value: a.color || "#3b6ea5", pair: 1 },
    { name: "balance_cents", label: "Current balance (amount owed for cards/loans)", type: "money", neg: 1, zero: 1, value: a.balance_cents ?? 0 },
  ], save("/api/accounts", a.id), del("/api/accounts", a.id));
}

export function receivedForm(p) {
  openForm(`Got paid: ${p.name}`, [
    { name: "amount_cents", label: "Actual take-home", type: "money", value: p.amount_cents, required: true },
  ], (d) => api("POST", "/api/received", { source_id: p.source_id, pay_date: p.orig_date, amount_cents: d.amount_cents }));
}

export function paidForm(b) {
  openForm(`Paid: ${b.name}`, [
    { name: "amount_cents", label: "Amount paid", type: "money", value: b.amount_cents, required: true, pair: 1 },
    { name: "paid_on", label: "Paid on", type: "date", value: S.me.today, required: true, pair: 1 },
  ], (d) => api("POST", "/api/payments", { bill_id: b.bill_id, due_date: b.orig_date, ...d }));
}

export function moveForm(item, onMove) {
  openForm(`Move ${item.name}`, [
    { name: "date", label: "New date", type: "date", value: item.date, required: true },
  ], (d) => onMove(d.date), null, "Move");
}

export function trackerForm(t = {}) {
  openForm(t.id ? "Edit tracker" : "New tracker", [
    { name: "name", label: "Name", value: t.name, required: true, placeholder: "Emergency fund" },
    { name: "kind", label: "Type", type: "select", options: [["savings", "Savings goal"], ["debt", "Debt payoff"], ["custom", "Custom"]],
      value: t.kind || "savings", pair: 1 },
    { name: "color", label: "Color", type: "color", value: t.color || "#1f7a4d", pair: 1 },
    { name: "start_cents", label: "Starting amount (debt: balance owed)", type: "money", zero: 1, value: t.start_cents ?? 0, pair: 1 },
    { name: "target_cents", label: "Goal (savings/custom)", type: "money", zero: 1, value: t.target_cents ?? 0, pair: 1 },
    { name: "target_date", label: "Target date (optional)", type: "date", value: t.target_date, nullable: 1, pair: 1 },
    { name: "account_id", label: "Linked account", type: "select", int: 1, options: acctOptions(), value: t.account_id ?? "", pair: 1 },
    { name: "notes", label: "Notes", type: "textarea", value: t.notes },
    { name: "archived", label: "Archived", type: "check", value: t.archived },
  ], save("/api/trackers/items", t.id), del("/api/trackers/items", t.id));
}

export function entryForm(t, e = {}) {
  openForm(e.id ? "Edit entry" : `Add to ${t.name}`, [
    { name: "amount_cents", label: t.kind === "debt" ? "Payment" : "Amount (negative to withdraw)", type: "money", neg: 1,
      value: e.amount_cents, required: true, pair: 1 },
    { name: "date", label: "Date", type: "date", value: e.date || S.me.today, required: true, pair: 1 },
    { name: "note", label: "Note", type: "textarea", value: e.note },
  ], (d) => (e.id ? api("PUT", `/api/trackers/entries/${e.id}`, { tracker_id: t.id, ...d })
    : api("POST", "/api/trackers/entries", { tracker_id: t.id, ...d })), del("/api/trackers/entries", e.id));
}

export function invAccountForm(a = {}) {
  openForm(a.id ? "Edit investment account" : "Add investment account", [
    { name: "name", label: "Name", value: a.name, required: true, placeholder: "Roth IRA" },
    { name: "kind", label: "Type", type: "select", options: [["brokerage", "Brokerage"], ["retirement", "Retirement"],
      ["crypto", "Crypto"], ["hsa", "HSA"], ["other", "Other"]], value: a.kind || "brokerage", pair: 1 },
    { name: "institution", label: "Institution", value: a.institution, max: 60, pair: 1 },
    { name: "cash_cents", label: "Cash in account", type: "money", zero: 1, value: a.cash_cents ?? 0 },
    { name: "notes", label: "Notes", type: "textarea", value: a.notes },
  ], save("/api/investments/accounts", a.id), del("/api/investments/accounts", a.id));
}

export function holdingForm(acctId, h = {}) {
  openForm(h.id ? `Edit ${h.symbol}` : "Add holding", [
    { name: "symbol", label: "Symbol", value: h.symbol, required: true, max: 20, placeholder: "VTI", pair: 1 },
    { name: "name", label: "Name", value: h.name, max: 60, pair: 1 },
    { name: "quantity", label: "Shares / units", type: "number", step: "any", min: 0, value: h.quantity ?? "", pair: 1 },
    { name: "price_cents", label: "Current price", type: "money", zero: 1, value: h.price_cents ?? 0, pair: 1 },
    { name: "cost_basis_cents", label: "Total cost basis", type: "money", zero: 1, value: h.cost_basis_cents ?? 0 },
  ], (d) => (h.id ? api("PUT", `/api/investments/holdings/${h.id}`, { inv_account_id: acctId, ...d })
    : api("POST", "/api/investments/holdings", { inv_account_id: acctId, ...d })), del("/api/investments/holdings", h.id));
}

export function reminderForm(r = {}, presetDate) {
  const at = r.next_at || `${presetDate || S.me.today}T09:00`;
  openForm(r.id ? "Edit reminder" : "New reminder", [
    { name: "title", label: "Remind me to", value: r.title, required: true, placeholder: "Call the insurance company" },
    { name: "date", label: "Date", type: "date", value: at.slice(0, 10), required: true, pair: 1, skip: 0 },
    { name: "time", label: "Time", type: "time", value: at.slice(11, 16), required: true, pair: 1 },
    { name: "recurrence", label: "Repeats", type: "select", options: recOpts(), value: r.recurrence || "once" },
    { name: "email", label: "Email me", type: "check", value: r.id ? r.email : true },
    { name: "push", label: "Push to my phone", type: "check", value: r.id ? r.push : true },
    { name: "active", label: "Active", type: "check", value: r.id ? r.active : true },
    { name: "note", label: "Note", type: "textarea", value: r.note },
  ], (d) => {
    const body = { title: d.title, note: d.note, next_at: `${d.date}T${d.time}`, recurrence: d.recurrence,
      email: d.email, push: d.push, active: d.active };
    return r.id ? api("PUT", `/api/reminders/${r.id}`, body) : api("POST", "/api/reminders", body);
  }, r.id ? () => api("DELETE", `/api/reminders/${r.id}`) : null);
}
