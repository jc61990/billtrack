// Plaid Link runs only on this page (its own, slightly wider CSP). The main app's CSP never allows third-party script.
import { api } from "./core.js";

const $ = (id) => document.getElementById(id);
const q = new URLSearchParams(location.search);
const reconnect = q.get("item") ? Number(q.get("item")) : null;
const addInv = q.get("add") === "investments";
const fail = (m) => { $("err").textContent = m; $("msg").textContent = ""; };

function loadPlaid() {
  return new Promise((ok, no) => {
    if (window.Plaid) return ok();
    const s = document.createElement("script");
    s.src = "https://cdn.plaid.com/link/v2/stable/link-initialize.js";
    s.onload = ok; s.onerror = () => no(new Error("Couldn't load Plaid. Check your internet connection."));
    document.head.append(s);
  });
}

async function open() {
  $("go").disabled = true; $("err").textContent = "";
  try {
    const { link_token } = await api("POST", "/api/plaid/link-token", reconnect ? { item: reconnect, add_investments: addInv } : {});
    await loadPlaid();
    const h = window.Plaid.create({
      token: link_token,
      onSuccess: async (public_token, meta) => {
        $("msg").textContent = "Saving the connection and pulling your accounts…";
        try {
          if (!reconnect) {
            await api("POST", "/api/plaid/exchange", { public_token, institution_id: meta?.institution?.institution_id || "",
              institution_name: meta?.institution?.name || "Bank", import_since: $("since").value || null });
          } else {
            await api("POST", `/api/plaid/sync?item=${reconnect}`);
          }
          location.replace("/#banks");
        } catch (e) { fail(e.message); $("go").disabled = false; }
      },
      onExit: (err) => { if (err) fail(err.display_message || err.error_message || "Plaid closed before finishing."); $("go").disabled = false; },
    });
    h.open();
  } catch (e) { fail(e.message); $("go").disabled = false; }
}

(async () => {
  try {
    const me = await api("GET", "/api/me");
    if (!me.perms.includes("finance:write")) return fail("Bank links are for user accounts (admins don't have finances).");
  } catch { location.replace("/"); return; }
  const t = new Date(); $("since").value = new Date(Date.UTC(t.getFullYear(), t.getMonth(), 1)).toISOString().slice(0, 10);
  if (reconnect) { $("ttl").textContent = addInv ? "Share investment positions" : "Reconnect your bank"; $("form").querySelector("label").hidden = true; $("form").querySelector(".hint").hidden = true; }
  $("msg").textContent = reconnect ? (addInv ? "Sign in to your bank again and allow it to share investments (holdings). Everything else stays as it is."
    : "Your bank needs you to sign in again. Nothing is changed in BillTrack.") :
    "You'll sign in to your bank through Plaid. BillTrack only gets read-only access to balances and transactions, never your bank password.";
  $("form").hidden = false;
  $("go").addEventListener("click", open);
})();
