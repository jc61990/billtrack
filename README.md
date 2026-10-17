# BillTrack

Self-hosted bill, paycheck, savings and investment tracker built around the **weekly** pay cycle. Multi-user with
roles, two-factor sign-in, alerts by email and phone push, and encryption at every layer: the database on disk,
secrets inside it, and every byte of browser traffic.

![version](https://img.shields.io/badge/version-3.0.0-blue)

## Features

| Area | What it does |
|---|---|
| Home | The landing page. **Needs attention** (overdue bills, low or soon-low balances, card payments due, cards over your utilization alert, unread alerts) or "All caught up", then cards for this week (in / out / left over, still to pay, next payday), the next 14 days of bills, accounts and credit utilization, net worth with 30-day change and 90-day sparkline, trackers, investments and this week's reminders. Quick-add buttons for bills, overtime, spending and reminders. Each card opens its section. One request, sections shown only if you have permission. |
| Week | Money in / out / left over, day-by-day register with running balance, per-account totals, 12-week chart. Tick bills paid and paychecks received. |
| Calendar | Every bill, payday, OT entry, expense and your ⏰ reminders on its day. Money going out is colored by the account it comes out of (grey = no account, legend under the calendar); money in is green; overdue bills get a red outline and "!". **Drag** to reschedule one occurrence (mouse, or long-press then drag on a phone). **Right-click / long-press** for paid, partial amount, move, skip, reset, edit, duplicate, delete, remind me. Right-click an empty day to add things there. |
| Accounts | **Checking and savings** with balance, 90-day sparkline, and a projected balance at the end of the pay week after scheduled bills and pay. **Credit cards** with balance vs limit, a **utilization bar** (green under 30%, amber 30-49%, red 50%+ or past your alert), your alert threshold marked on the bar, available credit, APR, due date and minimum payment. Overall utilization across all cards. Quick "Update balance" (button, right-click or API). |
| Net worth | Big current number with change over 30 days / 90 days / 1 year / since start, a history chart (net worth plus assets and debts, 3M to All), an assets-vs-debts composition bar, and every asset and debt listed. Bank accounts, cards and loans come from Accounts and investments from Investments automatically; add the rest by hand (home, vehicle, valuables, mortgage, student/auto/personal loans, taxes owed). A snapshot is saved daily and on every change. People without investment access see net worth without investments. |
| Overview | Any date range by week or month: income vs spending, spending by category, income by type, account balances, net worth, trackers, investments. |
| Bills & income | Recurring bills with account, autopay, reminder lead time; weekly pay; overtime auto-calculated (hours × rate × multiplier); bonuses; one-off spending. |
| Trackers | Savings goals, debt payoff, custom counters, with "$/week to stay on track" and milestone alerts. |
| Investments | Accounts and holdings, value / cost / gain, one-year history, bulk price updates (by hand or by API). |
| Alerts | In-app inbox + email + ntfy push, per user: bill reminders, due today, overdue, weekly summary, tracker milestones, **low bank balance** (now, or projected by end of week), **card utilization over your threshold**, **card payment due within 3 days**, and your own recurring reminders. Each balance alert fires once and re-arms when the condition clears. |
| Bank sync | Optional **Plaid** link (free personal trial, up to 10 banks): balances update automatically, card purchases and debits become spending, matching charges **mark bills paid**, payroll deposits **mark paychecks received** (actual amount, OT included). Transfers between your own accounts and card payoffs are skipped so nothing is counted twice. Read-only. |
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
| Database | SQLCipher 4: AES-256 per page + HMAC-SHA512, WAL included, temp tables in memory, keyed by a random 256-bit **data key**. The data key is wrapped by **your passphrase (Argon2id) + this server's key file**: both are needed, neither works alone. After a reboot the app is **locked** until `sudo billtrack-manage unlock`; the unlocked key lives only in `/run` (RAM). |
| Disaster-recovery backups | One `.btdr` file (database, settings, install answers, HTTPS CA): AES-256-GCM in authenticated chunks under a **backup password** (Argon2id), and the data inside still needs the **database passphrase**. Wrong password, wrong passphrase, truncation or any modified byte is refused. |
| Account numbers | Only the last 4 digits are accepted (`^\d{4}$`); full card or account numbers are rejected by the API. BillTrack never connects to a bank. |
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
sudo ./setup.sh
```
There are no command-line options or install variables. The first run asks:

1. Serve on HTTPS port 443 with nginx and a self-signed certificate? (Y, or N for Tailscale / your own proxy)
2. Any extra hostnames or IPs for the certificate (the machine's own names and IPs are always included)
3. Time zone (defaults to the server's)
4. Which day your pay week starts
5. Admin username and password

It then prints the address (e.g. `https://192.168.1.50`) and how to trust the certificate on your devices.
Email, push and everything else are set in **Admin → Settings**.

**Updating:** `git pull && sudo ./setup.sh`. It shows your saved answers and asks *Keep these settings?*. Press
Enter to keep them, or `n` to change the proxy choice or certificate names, issue a fresh certificate, or start a
new certificate authority. Answers are stored in `/etc/billtrack/install.conf` (root-only). Run without a terminal
(e.g. from automation) and it silently reuses the saved answers.

Updates **never** overwrite the env file, the database key, the database, the CA or users. Code:
`/opt/billtrack/src` (root-owned). Data: `/var/lib/billtrack`. Key: `/etc/billtrack/db.key`. **Back up the key
separately** (password manager); without it the data is gone.

Upgrading from 1.x: `git pull && sudo ./setup.sh`. Plaintext 1.0 databases are encrypted automatically; the
original user becomes admin.

#### How HTTPS is set up
- The app only listens on `127.0.0.1:8080`. **nginx** is the only thing on the network: port 443 (TLS 1.2/1.3,
  ECDSA, HTTP/2) proxies to the app; port 80 redirects to HTTPS and serves the CA certificate for download.
- `setup.sh` creates a **local certificate authority** (`/etc/billtrack/tls/ca.crt`, 10 years, private key
  root-only) and a **server certificate** signed by it (397 days, ECDSA P-256) covering the hostname,
  `hostname.local`, `localhost` and every IPv4 address on the box. Re-running setup re-issues the server
  certificate automatically when it's within 30 days of expiry or the machine's names/IPs change (or when you
  ask for a fresh one during an update). Because devices
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
- **Tailscale** (works away from home, real certificate): answer **N** to the HTTPS question, then
  `sudo tailscale serve --bg 8080` and open `https://<host>.<tailnet>.ts.net`.
- **Your own proxy** (Caddy, Traefik, existing nginx): answer **N** to the HTTPS question, proxy to `127.0.0.1:8080`, pass `Host` and
  `X-Forwarded-Proto: https`.
- **Docker** doesn't include a proxy; put one of the above in front of `127.0.0.1:8080`.

HTTPS is required, not optional: login cookies are HTTPS-only and browsers only allow the encrypted channel's
WebCrypto on HTTPS pages. Install to the home screen with *Add to Home Screen* (iPhone) / *Install app* (Android,
Chrome, Edge). Run **one** app process (default): channels and pending 2FA sign-ins are held in memory.

## Accounts, roles and sign-up

| Account type | Can | Can't |
|---|---|---|
| **Admin** | Admin portal: settings (email, sign-ups, security, Plaid keys), users, approvals, invite codes, audit log, backups and maintenance | Open any finance page or see anyone's money |
| **User** | Their **own private** bills, income, accounts, net worth, trackers, investments, bank links, reminders, calendar link | Change any server setting, or see another user's data |

Every financial record has an owner. If a request ever reaches finance code without a user in scope (an admin,
a script, a bug), the server refuses it rather than return anything. A test has a second user try to read,
change or link into another user's data through every route.

**Sign-up** (Admin → Settings → Sign-ups):
- *Anyone can sign up; an admin approves them* (default). The sign-in page shows **Create an account**. New
  sign-ups wait in Admin → Users → *Waiting for approval*. Admins get an in-app alert (and email); approved people
  get an email.
- *Invite code required*, or *Closed* (admins create accounts).
- **Invite codes** (Admin → Invite codes): `BT-XXXX-XXXX-XXXX-XXXX`, 80 random bits, limited uses and expiry,
  optional email straight from the portal (with a link that fills the code in). On the sign-up page the person
  ticks **I have an invite code** and skips approval. The list shows each code's state and who used it (email,
  username, time). Codes are stored hashed for lookup and sealed for display.

**First run** asks only for an admin account. On first sign-in the admin gets a **guided setup** that walks
through the public address, email, security, sign-up mode, invite codes and approvals, highlighting each spot with
Back / Next. Rerun it from Admin → Overview, which also shows a setup checklist and pending sign-ups.

**Upgrading from 2.x:** your existing data moves into your existing login, which becomes a regular **user**;
`setup.sh` then asks you to create a separate **admin** account. Other existing accounts become users with their
own (empty) data. Bank links move from the admin portal to each user's **Bank links** page; the calendar
subscription becomes a private link per user (Settings → Calendar app).

## Admin CLI
`setup.sh` installs `billtrack-manage` (Docker: `docker compose exec billtrack python -m app.manage ...`).
```
billtrack-manage adduser <name> [admin|user]
billtrack-manage passwd <name>          # sign them out everywhere
billtrack-manage reset-2fa <name>       # locked-out user lost their phone and codes
billtrack-manage unlock <name>
billtrack-manage gen-ics-token | gen-db-key
billtrack-manage rekey <new.key>        # re-encrypts DB and re-seals stored secrets
billtrack-manage backup <out.db> | restore <in.db>
billtrack-manage encrypt-db             # 1.0 migration (setup.sh runs it)
billtrack-manage snapshot <dir> [keep]  # verified copy with the server key, keep newest N (setup.sh does this)
billtrack-manage vacuum                 # compact the database (service stopped)
billtrack-manage rollback <snapshot>    # restore an upgrade snapshot (service stopped)
```

Key rotation:
```bash
billtrack-manage gen-db-key | sudo tee /etc/billtrack/db.key.new >/dev/null
sudo chown root:billtrack /etc/billtrack/db.key.new && sudo chmod 440 /etc/billtrack/db.key.new
sudo systemctl stop billtrack && billtrack-manage rekey /etc/billtrack/db.key.new
sudo mv /etc/billtrack/db.key.new /etc/billtrack/db.key && sudo systemctl start billtrack
```

## Time zones

Each person's dates follow **their own time zone**, picked up automatically from the device they use (Settings →
Profile → *Use this device's time zone*, on by default; untick it to choose one by hand). The server works out
"today", overdue bills, this week, paydays, reminder times and when that person's alerts and weekly summary go out
in that zone. Timestamps (last sync, sign-ins, audit log) are always shown in your browser's local time.
Admin → Settings → *Default time zone* only applies to people who haven't got one yet and to shared system tasks
(daily snapshots, account alerts).

## Encryption keys and disaster recovery

### Keys
| Secret | Where | Needed for |
|---|---|---|
| **Database passphrase** | your head / password manager | unlocking after every reboot, restoring any backup |
| **Key file** `/etc/billtrack/db.key` | this server | unlocking on this server (with the passphrase) |
| **Backup password** | password manager (choose per backup; must differ from the passphrase) | opening a `.btdr` file |

- Upgrading from 2.8: `sudo ./setup.sh` explains and asks you to choose the passphrase, then re-encrypts the
  database and every on-server backup with a new data key. New installs choose it during setup.
- After a reboot: `sudo billtrack-manage unlock`. Until then the web page shows **Locked** and nothing is served or
  sent; it refreshes itself once unlocked. `sudo billtrack-manage lock` forgets the key on demand.
- `sudo billtrack-manage change-passphrase` re-wraps the data key (no re-encryption). DR files made earlier still
  open with the passphrase that was current when they were made.
- `billtrack-manage key-status` shows locked/unlocked and the data-key fingerprint.
- **There is no recovery for a lost passphrase.** That is the point.

### Disaster-recovery backup
```bash
sudo billtrack-manage dr-export /root/billtrack-$(date +%F).btdr     # asks for a backup password
```
Includes the database, `billtrack.env`, the installer's answers, and the HTTPS CA certificate **and private key**
(so devices that trust it keep working). Admin → Backup & system → **Download DR backup** (and *Download as DR
backup* on any stored backup) makes the same file from the browser, but the service can't read the CA files, so
those are left out; restoring then makes a new CA that devices must trust again. Copy `.btdr` files off the server.

### Restore onto a fresh server
1. Install the OS, copy the `.btdr` file to it, `git clone` BillTrack, `cd billtrack`.
2. `sudo ./setup.sh` → choose **2) Restore from a disaster-recovery backup** → give the file path.
3. Enter the **backup password**, then the **database passphrase**.

Setup installs everything, restores the database and configuration, makes a new key file for this server and
re-wraps the data key for it (the old server's key file is never needed and can't open the new one), re-issues the
HTTPS certificate for this machine's names/IPs under the restored CA, and starts the service. Users, sessions,
2FA, alerts, bank links and history carry on. Point DNS or bookmarks at the new IP if it changed. To restore onto an
existing install instead: `sudo systemctl stop billtrack && sudo billtrack-manage dr-import <file>` (the previous
database is kept as `billtrack.db.before-import-*`), then start the service.

## Bank sync (Plaid)

1. Sign up at **dashboard.plaid.com** and choose **Personal use**. The free Trial plan allows up to 10 bank
   connections with real data.
2. In the Plaid dashboard: Developers → Keys. Copy the **client ID** and the **Production** secret.
3. BillTrack: **Admin → Settings → Bank sync (Plaid)**, paste both, environment *Production*, save.
4. Signed in as a **user**: **Bank links → Connect a bank.** Pick the date to import spending from (default: 1st of this month),
   then sign in to your bank in Plaid's window (Capital One, Citi and Schwab use their own sign-in pages).
5. For each bank account choose how BillTrack uses it: an existing account, **＋ New BillTrack account**,
   **＋ New investment account** (brokerage/retirement), or *Don't use*.

What syncs (every 6 hours by default, or **Sync now**):

| From the bank | Becomes |
|---|---|
| Checking/savings/card/loan balance | The mapped account's balance (card limits too) → low-balance and utilization alerts, net worth, Home |
| Brokerage/retirement balance | The mapped investment account's total value |
| A charge that matches an unpaid bill (same account or none, ±5 days of the due date, amount within 2% or $1) | Bill **marked paid** with the actual amount and date, tagged *Bank* |
| A payroll deposit near a payday (±3 days, 60%-175% of the usual take-home) | Paycheck **marked received** with the real amount |
| Any other purchase/debit | **Spending** with Plaid's category (Food & Drink, Rent & Utilities, …), tagged *Bank* |
| Transfers between your accounts, credit-card payments, other deposits | Skipped (no double counting) |
| Pending transactions | Wait until they post |
| A transaction the bank later removes | What it created is removed too |

If a bank asks you to sign in again, the link shows **Sign-in needed**, admins get an alert, and **Reconnect**
fixes it without losing anything. **Disconnect** revokes Plaid's access and deletes the tokens; imported spending
stays. Some banks need extra steps in the Plaid dashboard the first time (their OAuth checklist); Plaid's
error message says so.

Security: read-only (Transactions product only); only `production.plaid.com` / `sandbox.plaid.com` are contacted,
over verified TLS, with no extra Python dependency; access tokens and the Plaid secret are AES-GCM sealed in the
encrypted database and never sent to browsers; only admins can link banks (never via API tokens); every link,
mapping, sync and unlink is in the audit log. Plaid's Link script loads **only** on `/connect.html`, which has its
own content-security policy allowing `cdn.plaid.com` (plus inline styles, which Plaid's full-screen frame needs); the rest of the app still allows no third-party code.

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

Update a balance from a script: `POST /api/accounts/{id}/balance` with `{"balance_cents": 254000}` (write token).
Everything at a glance: `GET /api/home`. Account and card summary: `GET /api/accounts/overview`. Net worth: `GET /api/networth`, history
`GET /api/networth/history?days=365`, manual assets/debts `/api/networth/items`.

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
python -m pytest -q        # 45 tests incl. roles, 2FA, lockout, tokens, channel replay/tamper, encryption at rest
pip-audit -r requirements.txt
```

```
app/main.py        app + security headers + per-load index      app/channel.py   encrypted browser channel
app/security.py    auth, roles, sessions, throttles, audit      app/crypto.py    field sealing, token hashing
app/db.py          SQLCipher, migrations, backup/restore        app/settings.py  encrypted system settings
app/ledger.py      money math, trackers, investments, reports   app/notify.py    email, push, inbox, scheduler
app/routes/        auth, me, finance, trackers, admin           static/js/       PWA (ES modules, no build)
```

## Troubleshooting

| Symptom | Check |
|---|---|
| Page won't load / "502" or "504" | `sudo systemctl status billtrack` and `sudo journalctl -u billtrack -n 80 --no-pager` |
| App stops responding | The built-in watchdog writes **FREEZE DETECTED** plus every thread's stack to the journal after 20 s. Find it with `sudo journalctl -u billtrack \| grep -A60 "FREEZE DETECTED"` and share that block. Dump stacks on demand: `sudo systemctl kill -s USR1 billtrack`. |
| `systemctl restart` slow | It now gives in-flight requests 10 s, and systemd force-stops after 20 s. |
| Certificate warning | Trust the CA once per device (see *Trusting the certificate*). |
| nginx | `sudo nginx -t` and `sudo tail -n 30 /var/log/nginx/error.log` |

## Changelog
See [CHANGELOG.md](CHANGELOG.md).
