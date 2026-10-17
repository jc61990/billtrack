import { api, confirmBox, el, fill, fmtTs, money, refresh, toast } from "../core.js";

export const title = "Bank links";

// ---------- bank links (Plaid) ----------
const STATUS = { ok: ["Connected", ""], login_required: ["Sign-in needed", "overdue"], error: ["Sync error", "due-soon"] };
export async function render(root) {
  const [st, accts, invs] = await Promise.all([api("GET", "/api/plaid/status"), api("GET", "/api/accounts"), api("GET", "/api/investments/accounts")]);
  if (!st.configured) {
    fill(root, el("section", { class: "card" }, el("h2", {}, "Bank sync isn't available yet"),
      el("p", { class: "muted" }, "Your administrator hasn't set up bank sync yet.")));
    return;
  }
  const opts = (pa) => {
    const isInv = pa.type === "investment" || pa.type === "brokerage";
    const cur = pa.map_kind === "none" ? "none" : `${pa.map_kind}:${pa.map_id}`;
    const list = [["none", "Don't use"]];
    if (isInv) { list.push(["investment:new", "＋ New investment account"]); invs.forEach((a) => list.push([`investment:${a.id}`, `Investment: ${a.name}`])); }
    else { list.push(["account:new", "＋ New BillTrack account"]); accts.forEach((a) => list.push([`account:${a.id}`, `${a.name} (${a.kind})`])); }
    const sel = el("select", { "aria-label": `Use ${pa.name} as` }, list.map(([v, l]) => el("option", { value: v, selected: v === cur }, l)));
    sel.addEventListener("change", async () => {
      const [kind, id] = sel.value.split(":");
      try {
        await api("PUT", `/api/plaid/accounts/${pa.id}`, { map_kind: kind, map_id: id && id !== "new" ? Number(id) : null, create: id === "new" });
        toast("Saved"); refresh();
      } catch (e) { toast(e.message, true); refresh(); }
    });
    return sel;
  };
  const items = st.items.map((it) => {
    const [label, cls] = STATUS[it.status] || [it.status, ""];
    const since = el("input", { type: "date", value: it.import_since });
    since.addEventListener("change", () => api("PUT", `/api/plaid/items/${it.id}`, { import_since: since.value }).then(() => toast("Saved")).catch((e) => toast(e.message, true)));
    return el("section", { class: "card" },
      el("div", { class: "spread" }, el("h2", {}, it.institution_name, el("span", { class: `tag ${cls}` }, label)),
        el("span", { class: "muted small" }, `Last sync ${fmtTs(it.last_sync)}`)),
      it.error ? el("p", { class: it.status === "login_required" ? "out" : "muted small" },
        it.status === "login_required" ? "Your bank needs you to sign in again. Click Reconnect." : it.error) : null,
      el("div", { class: "scroll-x" }, el("table", { class: "tbl" },
        el("tr", {}, el("th", {}, "Bank account"), el("th", { class: "r" }, "Balance"), el("th", {}, "Use in BillTrack as"), el("th", {}, "Imported")),
        it.accounts.map((pa) => el("tr", {},
          el("td", {}, pa.name, el("div", { class: "meta" }, [pa.subtype || pa.type, pa.mask ? `••${pa.mask}` : ""].filter(Boolean).join(" · "))),
          el("td", { class: "r" }, pa.current_cents === null ? "—" : money(pa.current_cents)),
          el("td", {}, opts(pa)),
          el("td", { class: "small" }, pa.stats?.n ? `${pa.stats.expenses || 0} spending · ${pa.stats.bills || 0} bills paid · ${pa.stats.pays || 0} paychecks` : "—"))))),
      el("label", {}, "Import spending and paychecks from", since),
      el("div", { class: "actions left" },
        el("button", { type: "button", class: "primary", onclick: async (e) => {
          e.currentTarget.disabled = true;
          try { const r = await api("POST", `/api/plaid/sync?item=${it.id}`); const s = Object.values(r)[0] || {};
            toast(s.error ? `Sync problem: ${s.error}` : `Synced: ${s.added || 0} new, ${s.bills_paid || 0} bills paid, ${s.pay_received || 0} paychecks`, !!s.error);
          } catch (x) { toast(x.message, true); }
          refresh();
        } }, "Sync now"),
        el("button", { type: "button", onclick: () => { location.href = `/connect.html?item=${it.id}`; } }, "Reconnect"),
        el("button", { type: "button", class: "danger", onclick: async () => {
          if (await confirmBox(`Disconnect ${it.institution_name}?`, "Plaid access is revoked and the tokens are deleted. Spending already imported stays.", "Disconnect")) {
            api("DELETE", `/api/plaid/items/${it.id}`).then(() => { toast("Disconnected"); refresh(); }).catch((x) => toast(x.message, true));
          }
        } }, "Disconnect")));
  });
  fill(root,
    el("div", { class: "spread" }, el("p", { class: "muted" }, `Your bank connections are private to you. Balances and transactions sync automatically; matching charges mark bills paid and deposits mark paychecks received.`),
      el("button", { type: "button", class: "primary", onclick: () => { location.href = "/connect.html"; } }, "Connect a bank")),
    items.length ? items : el("div", { class: "empty-state" }, el("p", {}, "No banks connected yet.")),
    el("p", { class: "hint" }, "Read-only. Your bank password goes to your bank (or Plaid), never to BillTrack. Access tokens are stored encrypted."));
}

