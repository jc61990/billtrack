# BillTrack

Self-hosted bill, paycheck, savings and investment tracker built around the **weekly** pay cycle. Multi-user with
roles, two-factor sign-in, alerts by email and phone push, and encryption at every layer: the database on disk,
secrets inside it, and every byte of browser traffic.

![version](https://img.shields.io/badge/version-2.2.0-blue)

## Features

| Area | What it does |
|---|---|
| Week | Money in / out / left over, day-by-day register with running balance, per-account totals, 12-week chart. Tick bills paid and paychecks received. |
| Calendar | Every bill, payday, OT entry and expense on its day. **Drag** to reschedule one occurrence (mouse, or long-press then drag on a phone). **Right-click / long-press** for paid, partial amount, move, skip, reset, edit, duplicate, delete, remind me. Right-click an empty day to add things there. |
| Overview | Any date range by week or month: income vs spending, spending by category, income by type, account balances, net worth, trackers, investments. |
| Bills & income | Recurring bills with account, autopay, reminder lead time; weekly pay; overtime auto-calculated (hours × rate × multiplier); bonuses; one-off spending. |
| Trackers | Savings goals, debt payoff, custom counters, with "$/week to stay on track" and milestone alerts. |
| Investments | Accounts and holdings, value / cost / gain, one-year history, bulk price updates (by hand or by API). |
| Alerts | In-app inbox + email + ntfy push, per user: bill reminders, due today, overdue, weekly summary, tracker milestones, and your own recurring reminders. |
| Users | Admin / member / viewer roles, per-user permission removals, invites by email, password reset by email. |
| Admin portal | Users, invites, system settings (SMTP, push, time zone, pay-week start, 2FA policy, session length), audit log, all API tokens, encrypted backup. Hidden from non-admins and refused by the server. |
| API | Personal tokens (read or read/write, with expiry) for scripts; schema at `/api/openapi.json`. |
| Sync | One server = one source of truth. Installable on phone and PC (PWA); refreshes on focus and every minute. |

## Security model

| Layer | Protection |
|---|---|
| Browser ↔ server | TLS (your proxy / Tailscale) **plus** an application-layer encrypted channel: fresh ECDH P-256 key exchange on every page load → HKDF-SHA256 → AES-256-GCM. Every API call is one `POST /api/x` of random-looking base64; method, path, query, body, status and data are all inside. Random nonce + random padding: the same data never looks the same twice. Replays rejected, responses bound to requests, channel bound to the session. |
| Page source | Only the empty app shell, regenerated per request with a random CSP nonce and filler, so it's different on every load. No data is ever in the HTML. |
| Browser storage | None. Data lives only in JS memory while displayed; the service worker caches static files only, never API data. |
| Database | SQLCipher 4: AES-256 per page + HMAC-SHA512, WAL included, temp tables in memory. Raw 256-bit key from `BT_DB_KEY_FILE`. |
| Secrets in the DB | TOTP seeds, SMTP password, ntfy token additionally sealed with AES-256-GCM (HKDF-derived key). Never returned by the API. |
| Passwords | Argon2id, 12+ characters, same error for every login failure (no username discovery). |
| Sign-in | Optional/required TOTP 2FA with one-time recovery codes and replay protection; lockout after 8 failures; per-IP throttling. |
| Sessions | 256-bit random tokens stored as SHA-256; `HttpOnly; Secure; SameSite=Strict`; idle + absolute expiry; list and revoke per device. |
| Requests | CSRF header + Origin check, strict CSP (no inline script/style), strict input models (unknown fields rejected), parameterized SQL, body size caps, API rate limit. |
| Authorization | Role + permission checks on every endpoint server-side. API tokens are capped by their owner's permissions and can never do admin or account actions. |
| Accountability | Audit log of sign-ins, failures, lockouts, permission changes, settings, tokens, backups, deletes. |
| Backups | Passphrase-encrypted SQLCipher file (PBKDF2-HMAC-SHA512, 256k iterations). |
| Deployment | Hardened systemd unit (no capabilities, read-only system, private tmp); container non-root, read-only, `cap_drop: ALL`, bound to localhost. |
| Dependencies | Fully pinned, no known CVEs at release, all versions ≥ 2 weeks old. CI runs `pip-audit` on every push and weekly; Dependabot PRs. |

**What it can't do:** anything shown on screen is readable on that unlocked, signed-in device (that's how
displaying works). The channel protects traffic in dev tools' network tab, HAR files, proxy/CDN logs and
TLS-inspecting middleboxes; it does not stop someone already sitting at your signed-in browser. The server reads
your data to send alerts and build reports, so this is not zero-knowledge end-to-end encryption.

## Install

### Ubuntu + systemd (HTTPS on 443 out of the box)
```bash
git clone <your-repo> billtrack && cd billtrack
sudo ./setup.sh                 # app + nginx on 443 + self-signed certificate + admin login
```
It prints the address (e.g. `https://192.168.1.50`) and how to trust the certificate on your devices.
Then finish setup in **Admin → Settings** (email, push, time zone, pay-week start).

| Option | Use |
|---|---|
| `--san bills.lan,10.0.0.5` | Extra hostnames / IPs on the certificate (DNS names you've set up, a second NIC, a VPN IP) |
| `--regen-cert` | Force a new server certificate. Devices stay trusted (same CA) |
| `--regen-ca` | New CA + certificate. Every device must re-trust it |
| `--no-proxy` | Skip nginx; app only on `127.0.0.1:8080` (use with Tailscale or your own proxy) |

`setup.sh` is safe to re-run after `git pull`: it updates code and dependencies and **never** overwrites the env
file, the database key, the database or the CA. Code: `/opt/billtrack/src` (root-owned). Data:
`/var/lib/billtrack`. Key: `/etc/billtrack/db.key`. **Back up the key separately** (password manager); without
it the data is gone.

Upgrading from 1.x: `git pull && sudo ./setup.sh`. Plaintext 1.0 databases are encrypted automatically; the
original user becomes admin.

#### How HTTPS is set up
- The app only listens on `127.0.0.1:8080`. **nginx** is the only thing on the network: port 443 (TLS 1.2/1.3,
  ECDSA, HTTP/2) proxies to the app; port 80 redirects to HTTPS and serves the CA certificate for download.
- `setup.sh` creates a **local certificate authority** (`/etc/billtrack/tls/ca.crt`, 10 years, private key
  root-only) and a **server certificate** signed by it (397 days, ECDSA P-256) covering the hostname,
  `hostname.local`, `localhost` and every IPv4 address on the box. Re-running setup re-issues the server
  certificate automatically when it's within 30 days of expiry or the machine's names/IPs change. Because devices
  trust the CA, not the server certificate, renewals never need re-trusting.
- Firewall: if `ufw` is active, 80/tcp and 443/tcp are opened.
- Admin → Settings → Public URL is filled in with `https://<server-ip>` on first install.

#### Trusting the certificate (once per device)
Until you do this the browser shows a warning. Open `http://<server-ip>/billtrack-ca.crt` on the device, check
the SHA-256 fingerprint matches the one `setup.sh` printed, then:

| Device | Steps |
|---|---|
| iPhone / iPad | Settings → *Profile Downloaded* → Install, then Settings → General → About → Certificate Trust Settings → enable *BillTrack Local CA* |
| Android | Settings → Security → Encryption & credentials → Install a certificate → CA certificate |
| Windows | Double-click → Install Certificate → Local Machine → *Trusted Root Certification Authorities* |
| macOS | Open in Keychain Access (System keychain) → double-click → Trust → *Always Trust* |
| Firefox (any OS) | Settings → Privacy & Security → Certificates → View → Authorities → Import |

Print the fingerprint again any time: `sudo openssl x509 -in /etc/billtrack/tls/ca.crt -noout -fingerprint -sha256`

### Docker
```bash
cp .env.example .env && chmod 600 .env
python3 -c 'import secrets;print(secrets.token_hex(32))' > secrets/db.key
sudo chown 10001 secrets/db.key && sudo chmod 400 secrets/db.key
docker compose up -d --build
docker compose exec billtrack python -m app.manage adduser jc admin
```

### Other ways to reach it
- **Tailscale** (works away from home, real certificate): `sudo ./setup.sh --no-proxy` then
  `sudo tailscale serve --bg 8080` and open `https://<host>.<tailnet>.ts.net`.
- **Your own proxy** (Caddy, Traefik, existing nginx): `--no-proxy`, proxy to `127.0.0.1:8080`, pass `Host` and
  `X-Forwarded-Proto: https`.
- **Docker** doesn't include a proxy; put one of the above in front of `127.0.0.1:8080`.

HTTPS is required, not optional: login cookies are HTTPS-only and browsers only allow the encrypted channel's
WebCrypto on HTTPS pages. Install to the home screen with *Add to Home Screen* (iPhone) / *Install app* (Android,
Chrome, Edge). Run **one** app process (default): channels and pending 2FA sign-ins are held in memory.

## Roles

| Role | Can |
|---|---|
| admin | Everything, including the admin portal |
| member | View and edit bills, income, trackers, investments; own reminders and settings |
| viewer | View only; own reminders and settings |

Admins can additionally remove *Edit bills & income*, *See/Edit trackers*, *See/Edit investments*, *Use reminders*
from any member or viewer.

## Admin CLI
`setup.sh` installs `billtrack-manage` (Docker: `docker compose exec billtrack python -m app.manage ...`).
```
billtrack-manage adduser <name> [admin|member|viewer]
billtrack-manage passwd <name>          # sign them out everywhere
billtrack-manage reset-2fa <name>       # locked-out user lost their phone and codes
billtrack-manage unlock <name>
billtrack-manage gen-ics-token | gen-db-key
billtrack-manage rekey <new.key>        # re-encrypts DB and re-seals stored secrets
billtrack-manage backup <out.db> | restore <in.db>
billtrack-manage encrypt-db             # 1.0 migration (setup.sh runs it)
```

Key rotation:
```bash
billtrack-manage gen-db-key | sudo tee /etc/billtrack/db.key.new >/dev/null
sudo chown root:billtrack /etc/billtrack/db.key.new && sudo chmod 440 /etc/billtrack/db.key.new
sudo systemctl stop billtrack && billtrack-manage rekey /etc/billtrack/db.key.new
sudo mv /etc/billtrack/db.key.new /etc/billtrack/db.key && sudo systemctl start billtrack
```

## Alerts
- **Email:** Admin → Settings → Email. TLS is mandatory and certificates are verified. Gmail: `smtp.gmail.com`,
  587, STARTTLS, App Password. Each user sets their address in Settings.
- **Phone push:** install the ntfy app; each user picks a long random topic in Settings → Alerts. Self-hosted ntfy
  with an access token is recommended (topics on ntfy.sh are public to anyone who guesses the name).
- Checks run every `BT_CHECK_INTERVAL_MIN` (15) minutes; each alert is sent once per bill occurrence.

## API (for scripts)
Create a token in **Settings → API access**, then:
```bash
curl -H "Authorization: Bearer bt_..." https://bills.example.com/api/week
curl -H "Authorization: Bearer bt_..." -H "Content-Type: application/json" \
     -d '{"prices":[{"symbol":"VTI","price_cents":30125}]}' https://bills.example.com/api/investments/prices
```
Bearer requests use plain JSON over TLS (scripts can't do the browser channel) and are limited to the token's
scope and its owner's permissions. Full schema: `GET /api/openapi.json` with the token.

Main endpoints: `/api/week`, `/api/summary`, `/api/trend`, `/api/reports/overview`, `/api/bills`,
`/api/income-sources`, `/api/income-extra`, `/api/expenses`, `/api/accounts`, `/api/payments`, `/api/received`,
`/api/overrides`, `/api/trackers`, `/api/trackers/items`, `/api/trackers/entries`, `/api/investments`,
`/api/investments/accounts`, `/api/investments/holdings`, `/api/investments/prices`, `/api/investments/history`.

## Calendar feed
Set `BT_ICS_TOKEN` (`billtrack-manage gen-ics-token`) and subscribe to `https://<host>/cal/<token>.ics` from
Google, Outlook or iPhone Calendar.

## Configuration
Infrastructure settings are environment variables (`.env.example`). Everything else lives in Admin → Settings,
stored encrypted; env values only seed the first run.

## Development
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
export BT_DB_KEY=$(python -c 'import secrets;print(secrets.token_hex(32))') BT_COOKIE_SECURE=false
python -m app.manage adduser dev admin
uvicorn app.main:app --reload --port 8080
python -m pytest -q        # 23 tests incl. roles, 2FA, lockout, tokens, channel replay/tamper, encryption at rest
pip-audit -r requirements.txt
```

```
app/main.py        app + security headers + per-load index      app/channel.py   encrypted browser channel
app/security.py    auth, roles, sessions, throttles, audit      app/crypto.py    field sealing, token hashing
app/db.py          SQLCipher, migrations, backup/restore        app/settings.py  encrypted system settings
app/ledger.py      money math, trackers, investments, reports   app/notify.py    email, push, inbox, scheduler
app/routes/        auth, me, finance, trackers, admin           static/js/       PWA (ES modules, no build)
```

## Changelog
See [CHANGELOG.md](CHANGELOG.md).
