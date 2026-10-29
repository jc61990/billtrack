// App-shell cache only. API responses are never cached (always live = always in sync, nothing sensitive at rest).
const CACHE = "bt-v3.7.1";
const SHELL = ["/", "/index.html", "/style.css", "/icon.svg", "/manifest.webmanifest", "/js/main.js", "/js/core.js",
  "/js/auth.js", "/js/forms.js", "/js/items.js", "/js/channel.js", ...["home", "week", "calendar", "accounts", "networth", "overview", "bills", "income", "trackers",
    "investments", "alerts", "settings", "admin", "banks", "accounts", "networth", "home"].map((v) => `/js/views/${v}.js`)];
self.addEventListener("install", (e) => e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener("activate", (e) => e.waitUntil(
  caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || u.pathname.startsWith("/api/") || u.pathname.startsWith("/cal/")) return;
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request)));
});
