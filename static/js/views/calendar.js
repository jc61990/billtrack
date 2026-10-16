import { addDays, api, can, el, fmtDay, iso, money, moneyShort, onContext, parseIso, S, DOW, fill, safeColor } from "../core.js";
import { collect, isIn, isRem, itemChip, itemRow, reminderItems } from "../items.js";
import { dayMenu } from "./week.js";

export const title = "Calendar";
const MAX_CHIPS = 3;

let latest = 0;
export async function render(root) {
  const tok = ++latest;                  // fast clicking: only the newest month may draw
  const first = `${S.month}-01`;
  const lead = (parseIso(first).getUTCDay() + 6) % 7;
  const start = addDays(first, -lead), end = addDays(start, 41);
  const [data, rems] = await Promise.all([api("GET", `/api/summary?start=${start}&end=${end}&skipped=true`), reminderItems(start, end)]);
  if (tok !== latest) return;
  const by = {};
  for (const it of [...collect(data), ...rems]) (by[it.date] ||= []).push(it);
  const cells = DOW.map((d) => el("div", { class: "dow" }, d));
  for (let i = 0; i < 42; i++) {
    const order = (x) => isRem(x) ? 0 : isIn(x) ? 1 : 2;
    const d = addDays(start, i), list = (by[d] || []).sort((a, b) => order(a) - order(b));
    const tin = list.filter(isIn).reduce((s, x) => s + x.amount_cents, 0);
    const tout = list.filter((x) => !isIn(x) && !isRem(x)).reduce((s, x) => s + x.amount_cents, 0);
    const cell = el("div", { class: ["cell", d.slice(0, 7) !== S.month && "other", d === S.me.today && "today", d === S.calSel && "sel"].filter(Boolean).join(" "),
      dataset: { dropDate: d } },
      el("button", { type: "button", class: "dn", "aria-label": `${fmtDay(d)}: in ${money(tin)}, out ${money(tout)}`,
        onclick: () => { S.calSel = d; render(root); } }, Number(d.slice(8))),
      el("div", { class: "chips" }, list.slice(0, MAX_CHIPS).map(itemChip),
        list.length > MAX_CHIPS ? el("button", { type: "button", class: "more", onclick: () => { S.calSel = d; render(root); } },
          `+${list.length - MAX_CHIPS} more`) : null));
    onContext(cell, () => dayMenu(d));
    cells.push(cell);
  }
  const sel = S.calSel ? (by[S.calSel] || []) : null;
  fill(root, 
    el("div", { class: "weeknav" },
      el("button", { type: "button", "aria-label": "Previous month", onclick: () => shift(-1, root) }, "‹"),
      el("button", { type: "button", class: "weeklabel", onclick: () => { S.month = S.me.today.slice(0, 7); S.calSel = null; render(root); } },
        parseIso(first).toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: "UTC" })),
      el("button", { type: "button", "aria-label": "Next month", onclick: () => shift(1, root) }, "›")),
    el("p", { class: "hint" }, can("finance:write") ? "Drag a bill, payday, entry or ⏰ reminder to another day to reschedule it. Right-click (long-press on phone) for more." : ""),
    el("div", { class: "cal" }, cells),
    el("div", { class: "acct-legend" }, S.accounts.map((a) =>
      el("span", {}, el("i", { style: `background:${safeColor(a.color)}` }), a.name)),
      el("span", {}, el("i", { class: "none" }), "No account"), el("span", {}, el("i", { class: "inc" }), "Money in")),
    sel ? el("div", { class: "card" }, el("h2", {}, fmtDay(S.calSel, { weekday: "long", month: "long", day: "numeric" })),
      el("div", { class: "days flat", dataset: { dropDate: S.calSel } }, sel.length ? sel.map(itemRow) : el("div", { class: "empty" }, "Nothing on this day"))) : null);
}

function shift(n, root) {
  const [y, m] = S.month.split("-").map(Number);
  S.month = iso(new Date(Date.UTC(y, m - 1 + n, 1))).slice(0, 7); S.calSel = null; render(root);
}
