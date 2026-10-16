import { api, can, el, emptyState, fmtDay, money, onContext, progressBar, refresh, safeColor, showDialog, toast , fill } from "../core.js";
import { entryForm, trackerForm } from "../forms.js";

export const title = "Trackers";
let showArchived = false;

export async function render(root) {
  const w = can("trackers:write");
  const ts = await api("GET", `/api/trackers?archived=${showArchived}`);
  const cards = ts.map((t) => {
    const card = el("article", { class: "card tracker", style: `border-left:4px solid ${safeColor(t.color)}` },
      el("div", { class: "spread" }, el("h3", {}, t.name, t.archived ? el("span", { class: "tag" }, "Archived") : null),
        el("span", { class: "muted small" }, { savings: "Savings goal", debt: "Debt payoff", custom: "Custom" }[t.kind])),
      el("div", { class: "spread big" },
        el("strong", {}, money(t.current_cents)),
        el("span", { class: "muted" }, t.kind === "debt" ? `left of ${money(t.start_cents)}` : t.target_cents ? `of ${money(t.target_cents)}` : "")),
      progressBar(t.progress, t.color),
      el("div", { class: "meta" }, [`${Math.round(t.progress * 100)}%`,
        t.target_date ? `target ${fmtDay(t.target_date, { month: "short", day: "numeric", year: "numeric" })}` : null,
        t.weekly_needed_cents ? `${money(t.weekly_needed_cents)}/week to stay on track` : null,
        t.last_entry ? `last entry ${fmtDay(t.last_entry, { month: "short", day: "numeric" })}` : null].filter(Boolean).join(" · ")),
      el("div", { class: "actions left" },
        w ? el("button", { type: "button", class: "primary", onclick: () => entryForm(t) }, t.kind === "debt" ? "Log payment" : "Add money") : null,
        el("button", { type: "button", onclick: () => history(t) }, "History"),
        w ? el("button", { type: "button", onclick: () => trackerForm(t) }, "Edit") : null));
    onContext(card, () => [
      w ? { label: t.kind === "debt" ? "Log payment…" : "Add money…", action: () => entryForm(t) } : null,
      { label: "History…", action: () => history(t) },
      w ? { label: "Edit…", action: () => trackerForm(t) } : null,
      w ? { label: t.archived ? "Unarchive" : "Archive", action: () => api("PUT", `/api/trackers/items/${t.id}`, {
        name: t.name, kind: t.kind, target_cents: t.target_cents, start_cents: t.start_cents, target_date: t.target_date,
        account_id: t.account_id, color: t.color, notes: t.notes, archived: !t.archived }).then(refresh).catch((e) => toast(e.message, true)) } : null,
    ]);
    return card;
  });
  fill(root, 
    el("div", { class: "spread" }, el("p", { class: "muted" }, "Savings goals, debt payoff, or anything you want to count."),
      w ? el("button", { type: "button", class: "primary", onclick: () => trackerForm() }, "New tracker") : null),
    ts.length ? el("div", { class: "grid2" }, cards) : emptyState("No trackers yet.", w ? "Create one" : null, () => trackerForm()),
    el("label", { class: "check" }, el("input", { type: "checkbox", checked: showArchived, onchange: (e) => { showArchived = e.target.checked; render(root); } }), "Show archived"));
}

async function history(t) {
  const es = await api("GET", `/api/trackers/${t.id}/entries`);
  const w = can("trackers:write");
  showDialog(`${t.name}: history`, es.length ? el("ul", { class: "list" }, es.map((e) => el("li", {},
    el("button", { type: "button", class: "row", disabled: !w, onclick: () => entryForm(t, e) },
      el("span", {}, fmtDay(e.date), el("div", { class: "meta" }, e.note)),
      el("span", { class: "amt " + (e.amount_cents < 0 ? "out" : "in") }, money(e.amount_cents)))))) : el("p", { class: "muted" }, "No entries yet."));
}
