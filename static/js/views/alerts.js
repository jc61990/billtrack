import { api, can, el, emptyState, fmtTs, onContext, refresh, REC_LABEL, S, toast , fill } from "../core.js";
import { reminderForm } from "../forms.js";

export const title = "Alerts and reminders";

export async function render(root) {
  const [notes, rems] = await Promise.all([api("GET", "/api/me/notifications"),
    can("reminders") ? api("GET", "/api/reminders") : Promise.resolve([])]);
  const unread = notes.filter((n) => !n.read_at);
  fill(root, 
    el("div", { class: "spread" }, el("h2", {}, `Inbox${unread.length ? ` (${unread.length} new)` : ""}`),
      el("div", { class: "actions left" },
        unread.length ? el("button", { type: "button", onclick: () => api("POST", "/api/me/notifications/read", { all: true }).then(refresh) }, "Mark all read") : null,
        notes.some((n) => n.read_at) ? el("button", { type: "button", onclick: () => api("DELETE", "/api/me/notifications").then(refresh) }, "Clear read") : null)),
    notes.length ? el("ul", { class: "inbox" }, notes.map((n) => {
      const li = el("li", { class: `${n.read_at ? "" : "unread"} ${n.level}` },
        el("button", { type: "button", class: "row", onclick: async () => {
          if (!n.read_at) await api("POST", "/api/me/notifications/read", { ids: [n.id] });
          li.classList.toggle("open"); li.classList.remove("unread"); n.read_at = n.read_at || "now"; badge();
        } }, el("span", {}, el("strong", {}, n.title), el("div", { class: "meta" }, fmtTs(n.ts)))),
        el("pre", { class: "body" }, n.body));
      return li;
    })) : emptyState("No alerts yet. Bill reminders, due dates, weekly summaries and your reminders show up here."),
    can("reminders") ? el("div", { class: "spread" }, el("h2", {}, "My reminders"),
      el("button", { type: "button", class: "primary", onclick: () => reminderForm() }, "New reminder")) : null,
    can("reminders") ? (rems.length ? el("ul", { class: "list" }, rems.map((r) => {
      const li = el("li", { class: r.active ? "" : "muted" }, el("button", { type: "button", class: "row", onclick: () => reminderForm(r) },
        el("span", {}, el("span", {}, r.title), el("div", { class: "meta" },
          [r.active ? fmtTs(r.next_at) : "Done / paused", REC_LABEL[r.recurrence], r.email ? "email" : null, r.push ? "push" : null].filter(Boolean).join(" · ")))));
      onContext(li, () => [{ label: "Edit…", action: () => reminderForm(r) },
        { label: "Delete", danger: true, action: () => api("DELETE", `/api/reminders/${r.id}`).then(refresh).catch((e) => toast(e.message, true)) }]);
      return li;
    })) : el("p", { class: "muted" }, "No reminders. Right-click a day on the calendar to add one there.")) : null);
}

export async function badge() {
  try {
    const me = await api("GET", "/api/me");
    S.me.unread = me.unread;
    const b = document.getElementById("bellCount");
    if (b) { b.textContent = me.unread > 99 ? "99+" : me.unread; b.hidden = !me.unread; }
  } catch { /* ignore */ }
}
