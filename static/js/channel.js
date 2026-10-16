// Encrypted channel: fresh ECDH P-256 key pair per page load (private key non-extractable, memory only),
// HKDF-SHA256 -> AES-256-GCM. Every API call is one POST /api/x of base64(nonce||ciphertext); method, path,
// query, body, status and data all live inside the ciphertext. Random nonce + random padding = never the same twice.
const enc = new TextEncoder(), dec = new TextDecoder();
const b64 = (u8) => { let s = ""; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode(...u8.subarray(i, i + 0x8000)); return btoa(s); };
const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
let chan = null, pending = null;

async function handshake() {
  const kp = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]);
  const pub = new Uint8Array(await crypto.subtle.exportKey("raw", kp.publicKey));
  const r = await fetch("/api/hs", { method: "POST", credentials: "same-origin", headers: { "X-BT": "1", "Content-Type": "application/json" },
    body: JSON.stringify({ pub: b64(pub) }) });
  if (!r.ok) throw new Error("Couldn't open a secure channel to the server");
  const h = await r.json();
  const serverPub = await crypto.subtle.importKey("raw", unb64(h.pub), { name: "ECDH", namedCurve: "P-256" }, false, []);
  const bits = await crypto.subtle.deriveBits({ name: "ECDH", public: serverPub }, kp.privateKey, 256);
  const ikm = await crypto.subtle.importKey("raw", bits, "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt: unb64(h.salt), info: enc.encode("billtrack-channel-v1") },
    ikm, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  return { cid: h.cid, key, seq: 0 };
}

async function ensure(force) {
  if (force) chan = null;
  if (chan) return chan;
  pending ||= handshake().then((c) => { chan = c; pending = null; return c; }, (e) => { pending = null; throw e; });
  return pending;
}

function pad() { const n = 8 + (crypto.getRandomValues(new Uint8Array(1))[0] % 96); return b64(crypto.getRandomValues(new Uint8Array(n))); }

/** Returns { status, headers, bytes }. Retries once with a fresh channel if the server lost/rejected ours. */
export async function call(method, path, body, retry = true) {
  const c = await ensure(false);
  const seq = ++c.seq;
  const msg = enc.encode(JSON.stringify({ m: method, p: path, b: body === undefined ? null : body, s: seq, t: Date.now(), p2: pad() }));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: enc.encode(`${c.cid}|req`) }, c.key, msg));
  const blob = new Uint8Array(12 + ct.length); blob.set(iv); blob.set(ct, 12);
  const r = await fetch("/api/x", { method: "POST", credentials: "same-origin", headers: { "X-BT": "1", "X-BT-C": c.cid, "Content-Type": "text/plain" }, body: b64(blob) });
  if (r.status === 419 || r.status === 403) {
    if (retry) { await ensure(true); return call(method, path, body, false); }
    throw new Error("Secure channel error. Reload the page.");
  }
  if (r.status !== 200) throw new Error(r.status === 429 ? "Too many requests. Slow down." : `Request failed (${r.status})`);
  const raw = unb64(await r.text());
  const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: raw.subarray(0, 12), additionalData: enc.encode(`${c.cid}|res|${seq}`) }, c.key, raw.subarray(12));
  const res = JSON.parse(dec.decode(pt));
  return { status: res.s, headers: res.h || {}, bytes: unb64(res.b) };
}
export const text = (bytes) => dec.decode(bytes);
