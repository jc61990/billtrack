# Changelog

## 2.5.3 - 2026-10-16
### Fixed
- `setup.sh`: nginx 1.25.1+ warned that `listen ... http2` is deprecated. The installer now checks the nginx
  version and writes `http2 on;` for 1.25.1 and newer, or the `listen ... ssl http2` form for older versions
  (Ubuntu 24.04 ships 1.24). HTTP/2 stays enabled either way.

## 2.5.2 - 2026-10-16
### Fixed
- Bills not appearing on the calendar. The bill form's **"Last payment (optional)"** field was easy to read as
  "date I last paid", but it meant "stop repeating after this date", so a past date there silently ended the bill.
  - Renamed to **"Stop after this date (optional)"** with an explanation (also on Pay).
  - The API rejects a stop date earlier than the first due date.
  - Migration clears stop dates on/before the first due date for repeating bills and pay (those could only ever
    occur once, so the value was always a mistake). One-time items and genuine future end dates are untouched.
  - Bills list shows "stops after <date>" / "ENDED <date>" so an end date is never invisible again.

## 2.5.1 - 2026-10-16
### Fixed
- Reminders didn't appear on the Calendar or Week view (including ones added by right-clicking a day). Your
  active reminders now show as ⏰ chips/rows on their dates (repeating ones on every date), can be dragged to
  another day, and have Edit / Move / Delete in the right-click menu. They never count toward money totals.
### Added
- `GET /api/reminders/occurrences?start=&end=` (your own reminders only).

## 2.5.0 - 2026-10-16
### Added
- **Net worth** section: current net worth with 30-day / 90-day / 1-year / since-start change, history chart
  (net worth, assets, debts; 3M/6M/1Y/5Y/All), assets-vs-debts composition bar, and full asset and debt lists.
- Manual assets and debts (`/api/networth/items`): home, vehicle, valuables, untracked pension, money owed to you;
  mortgage, auto/student/personal loans, medical debt, taxes owed. Can be excluded without deleting.
- Daily net worth snapshots (`networth_snapshots`), also refreshed whenever a balance, holding, price or item
  changes. `GET /api/networth`, `GET /api/networth/history`.
### Changed
- Overview's net worth now uses the same calculation (includes manual assets/debts and loan accounts).
- Net worth respects permissions: users without investment access get a figure without investments.
- Charts size themselves to the screen so labels stay readable on phones.
### Fixed
- Right-click menu could close immediately when the page scrolled right as it opened.

## 2.4.0 - 2026-10-16
### Added
- **Accounts** section (bottom-bar tab on phones): checking/savings with balance, 90-day sparkline, last updated,
  and projected balance at the end of the pay week after scheduled bills and pay.
- **Credit card tracker:** balance vs limit, utilization bar (green < 30%, amber 30-49%, red >= 50% or over your
  alert) with your alert threshold and the 30% guideline marked, available credit, APR, due day, minimum payment,
  bills charged to the card this week, and overall utilization across all cards.
- **Balance alerts:** low balance, projected low balance by end of week, card utilization at/above threshold,
  card payment due within 3 days. Fire once, re-arm when cleared; per-user on/off in Settings → Alerts.
- Daily balance history (`account_history`), quick update `POST /api/accounts/{id}/balance`,
  summary `GET /api/accounts/overview`. Add menu gains Bank account / Credit card.
### Security
- Only the last four digits of an account/card can be stored; anything longer is rejected (422).
- Account edits always send the full model so editing one field can't silently reset limits or thresholds.

## 2.3.0 - 2026-10-16
### Changed
- `setup.sh` is fully interactive: no command-line options or environment overrides. First run asks about the
  HTTPS proxy, extra certificate names/IPs, time zone, pay-week start and the admin login. Updates show the saved
  answers and ask "Keep these settings?"; answering no lets you change them, re-issue the certificate or create a
  new CA.
- Answers saved to `/etc/billtrack/install.conf` (root-only, parsed as data, never sourced); without a terminal
  the saved answers are reused.
- Time zone and pay-week start (also used as the weekly-summary day) are set during the first install.
### Security
- Certificate names/IPs are validated against strict hostname/IP patterns before reaching openssl.
- Service restarts after the installer writes settings so the running app never uses stale cached values.

## 2.2.0 - 2026-10-16
### Added
- `setup.sh` now serves BillTrack on **HTTPS port 443** via nginx by default. The app stays on `127.0.0.1:8080`.
- Auto-generated **local CA** (ECDSA P-256, 10 y) + **server certificate** (397 d, SAN = hostname, `.local`,
  `localhost`, all IPv4s, plus `--san` extras). Server cert re-issued automatically on re-run when near expiry or
  when names/IPs change; the CA is kept so devices stay trusted.
- Port 80 redirects to HTTPS and serves `/billtrack-ca.crt`; installer prints its SHA-256 fingerprint and
  per-device trust steps.
- Installer options: `--san`, `--regen-cert`, `--regen-ca`, `--no-proxy`, `--help`.
- Opens 80/443 in ufw when active; fills in Admin → Settings → Public URL on first install; HTTPS self-check.
### Security
- nginx: TLS 1.2/1.3 only, ECDHE-ECDSA AEAD ciphers, no session tickets, `server_tokens off`, 1 MB body cap,
  header/body timeouts. `X-Forwarded-For` overwritten (not appended) and `X-Forwarded-Host` stripped so clients
  can't spoof IPs or origins; the app trusts proxy headers from 127.0.0.1 only.
- IPv6 listeners only added when the host has IPv6 (nginx fails to start otherwise).

## 2.1.0 - 2026-10-14
### Security
- **Encrypted browser channel.** Each page load does a fresh ECDH P-256 key exchange (browser private key is
  non-extractable and memory-only) → HKDF-SHA256 → AES-256-GCM. All browser API traffic is a single
  `POST /api/x` of `base64(nonce‖ciphertext)`; method, path, query, request body, status and response data are
  all inside the ciphertext. Random nonces and random padding mean identical data never produces identical bytes.
  Strictly increasing sequence numbers block replays; responses are bound to their request; channels are bound to
  the session cookie, expire after 30 min idle / 12 h, and are re-negotiated automatically.
- Plaintext cookie-authenticated `/api/*` calls are refused (`BT_REQUIRE_CHANNEL=true`). Bearer-token scripts and
  the ICS feed are unchanged (TLS only).
- `index.html` is generated per request with a random CSP script nonce and random filler, so page source is never
  byte-identical between loads. It contains no user data.
### Added
- `app/channel.py` (ASGI middleware), `static/js/channel.js` (WebCrypto client), 6 channel tests.

## 2.0.0 - 2026-10-14
### Added
- Roles (admin / member / viewer) with per-user permission removals, enforced server-side.
- Login page with optional TOTP two-factor + one-time recovery codes; "require 2FA for everyone" policy.
- Account lockout (8 failures / 15 min), per-IP throttling, idle and absolute session expiry, session list + revoke.
- Email: password reset links, invites, per-user alerts. Admin portal: users, invites, system settings (SMTP,
  ntfy, time zone, week start, digest, security), audit log, all API tokens, encrypted backup, system info.
- Personal settings: profile, alert preferences, password, 2FA, devices, API tokens.
- Calendar shows each item; drag to reschedule (mouse or touch long-press); right-click / long-press menus
  (mark paid, partial amount, move, skip, reset, edit, duplicate, delete, remind me).
- In-app alert inbox, custom recurring reminders, tracker milestone alerts, per-user email/push preferences.
- Trackers (savings, debt payoff, custom) with weekly-needed pacing. Investments (accounts, holdings, value/gain,
  history, bulk price API). Overview reports (any range, by week/month, category, income type, net worth).
- Personal API tokens (read / write scope, expiry; never admin) and `/api/openapi.json` for signed-in users.
### Security
- Secrets (TOTP seeds, SMTP password, ntfy token) additionally AES-256-GCM sealed with an HKDF-derived key.
- `rekey` re-seals them; strict input models reject unknown fields.
### Changed
- Settings moved from env into the encrypted DB (env values are first-run defaults).
- v1 databases migrate automatically; the original user becomes admin.
### Dependencies
- Added `cryptography==50.0.1`.

## 1.1.0 - 2026-10-14
### Security
- Database encrypted at rest with SQLCipher 4 (AES-256-CBC + HMAC-SHA512 per page, WAL included), raw 256-bit key
  from `BT_DB_KEY_FILE` / `BT_DB_KEY`. App refuses to start without a valid key or on an unencrypted file.
- `PRAGMA temp_store=MEMORY` so no plaintext temp files.
- Plaintext JSON export (`GET /api/export`) **removed**; replaced by `POST /api/backup` which returns a
  passphrase-encrypted database file.
### Added
- Admin CLI: `gen-db-key`, `encrypt-db` (1.0 migration), `rekey`, `backup`, `restore`.
- setup.sh: creates `/etc/billtrack/db.key` (root:billtrack 440), auto-encrypts a 1.0 database, installs
  `/usr/local/bin/billtrack-manage`.
- Docker: key supplied as a compose secret.
- Tests: file contains no plaintext, wrong key rejected, backup/restore round trip, 1.0 migration.
### Dependencies
- Added `sqlcipher3==0.6.2` (SQLCipher 4.12.0, SQLite 3.51.1, OpenSSL 3.6.0 bundled).

## 1.0.0 - 2026-10-14
Initial release.
- Bills with recurrence (weekly, biweekly, twice monthly, monthly, quarterly, yearly, one time), account, autopay, reminder lead time, end date
- Pay sources (weekly by default) with actual take-home recorded per payday
- Overtime log: hours x hourly rate x multiplier calculated automatically, plus bonus/side/refund/other income
- One-off spending log
- Weekly view: money in, money out, left over, running balance by day, per-account breakdown, 12-week trend chart
- Month calendar with daily in/out totals
- Alerts by email (SMTP over TLS) and phone push (ntfy): reminder, due today, overdue, weekly summary
- ICS calendar feed for Google/Outlook/iPhone calendar
- Installable PWA (phone + PC), server is the single source of truth
- Security: Argon2id passwords, hashed session tokens, SameSite=Strict HttpOnly cookies, CSRF header + Origin check,
  strict CSP, login throttling, input validation, parameterized SQL, hardened systemd unit and container
