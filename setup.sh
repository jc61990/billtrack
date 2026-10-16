#!/usr/bin/env bash
# BillTrack installer / updater for Ubuntu 22.04/24.04.  Run:  sudo ./setup.sh
#
# Everything is asked interactively; nothing is passed on the command line. Answers are saved to
# /etc/billtrack/install.conf, so updates (git pull && sudo ./setup.sh) just ask "keep these settings?".
# Safe to re-run:
#   - code in /opt/billtrack is replaced from THIS checkout (git is the source of truth)
#   - env file, database key, database and certificate authority are NEVER overwritten
#   - a v1.0 plaintext database is encrypted automatically (plaintext copy kept as .plain.bak)
#   - HTTPS on 443 via nginx with an auto-generated local CA + server certificate
set -euo pipefail

APP_DIR=/opt/billtrack
DATA_DIR=/var/lib/billtrack
CONF_DIR=/etc/billtrack
ENV_FILE=$CONF_DIR/billtrack.env
KEY_FILE=$CONF_DIR/db.key
INSTALL_CONF=$CONF_DIR/install.conf
TLS_DIR=$CONF_DIR/tls
DB_FILE="$DATA_DIR/billtrack.db"
BIND=127.0.0.1            # app stays on loopback; nginx is the only thing on the network
PORT=8080
SVC_USER=billtrack
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo ./setup.sh"; exit 1; }
[[ -f "$SRC_DIR/app/main.py" ]] || { echo "Run from the repo root"; exit 1; }
[[ $# -eq 0 ]] || { echo "setup.sh takes no options - it will ask you what it needs."; exit 1; }

# ---------- prompt helpers (read from the terminal even if stdin is redirected) ----------
TTY=/dev/tty
INTERACTIVE=1; [[ -r $TTY && -w $TTY ]] || INTERACTIVE=0
ask() {   # ask "Question" "default" -> echoes answer
  local q="$1" def="${2:-}" a=""
  if [[ $INTERACTIVE == 1 ]]; then
    if [[ -n "$def" ]]; then printf '%s [%s]: ' "$q" "$def" >$TTY; else printf '%s: ' "$q" >$TTY; fi
    IFS= read -r a <$TTY || true
  fi
  echo "${a:-$def}"
}
yesno() { # yesno "Question" Y|N -> exit status 0 for yes
  local def="$2" a
  while true; do
    a=$(ask "$1 $( [[ $def == Y ]] && echo '(Y/n)' || echo '(y/N)')" "")
    a="${a:-$def}"
    case "${a,,}" in y|yes) return 0 ;; n|no) return 1 ;; esac
    echo "  Please answer y or n." >$TTY
  done
}
valid_san() {  # comma list of hostnames / IPv4 / IPv6; nothing else reaches openssl
  local x IFS=','
  for x in $1; do
    x="${x// /}"; [[ -z "$x" ]] && continue
    [[ "$x" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ || "$x" =~ ^[0-9A-Fa-f:]+$ || \
       "$x" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$ ]] || return 1
  done
}

# ---------- saved answers (parsed, never sourced) ----------
PROXY=1; EXTRA_SAN=""
if [[ -f $INSTALL_CONF ]]; then
  while IFS='=' read -r k v; do
    case "$k" in
      PROXY) [[ "$v" =~ ^[01]$ ]] && PROXY=$v ;;
      EXTRA_SAN) valid_san "$v" && EXTRA_SAN=$v ;;
    esac
  done < "$INSTALL_CONF"
  FIRST_RUN=0
else
  FIRST_RUN=1
fi
REGEN_CERT=0; REGEN_CA=0

echo
if [[ $FIRST_RUN == 1 ]]; then
  echo "BillTrack setup"
  echo "---------------"
else
  echo "BillTrack update - current settings:"
  echo "  HTTPS on port 443 (nginx + self-signed cert): $( [[ $PROXY == 1 ]] && echo yes || echo 'no (app on 127.0.0.1:8080 only)')"
  [[ $PROXY == 1 ]] && echo "  Extra certificate names/IPs:                  ${EXTRA_SAN:-none}"
fi
if [[ $FIRST_RUN == 1 ]] || ! yesno "Keep these settings?" Y; then
  if yesno "Serve BillTrack on HTTPS port 443 with nginx and a self-signed certificate? (No = you'll use Tailscale or your own proxy)" Y; then
    PROXY=1
    echo "  The certificate automatically covers this machine's hostname, hostname.local, localhost and all its IPs."
    while true; do
      A=$(ask "  Any other hostnames or IPs to add, comma separated (blank for none, '-' to clear)" "$EXTRA_SAN")
      A="${A// /}"; [[ "$A" == "-" ]] && A=""
      if valid_san "$A"; then EXTRA_SAN=$A; break; fi
      echo "  That doesn't look like hostnames/IPs. Example: bills.lan,10.0.0.5" >$TTY
    done
    if [[ -f $TLS_DIR/ca.crt ]]; then
      if yesno "  Issue a fresh server certificate now? (devices stay trusted)" N; then REGEN_CERT=1; fi
      if yesno "  Start over with a NEW certificate authority? (every device must re-trust it)" N; then REGEN_CA=1; REGEN_CERT=1; fi
    fi
  else
    PROXY=0
  fi
fi
[[ $INTERACTIVE == 1 ]] || echo "(no terminal - using saved/default answers)"
install -d -m 750 "$CONF_DIR"
( umask 077; printf 'PROXY=%s\nEXTRA_SAN=%s\n' "$PROXY" "$EXTRA_SAN" > "$INSTALL_CONF" )
echo

echo "==> Packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv rsync curl >/dev/null
if [[ $PROXY == 1 ]]; then apt-get install -y -qq nginx openssl >/dev/null; fi

echo "==> Service user"
id "$SVC_USER" &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER"

echo "==> Code -> $APP_DIR"
install -d -m 755 "$APP_DIR"
rsync -a --delete --exclude '.git' --exclude '.venv' --exclude 'data' --exclude '.env' \
  --exclude '__pycache__' --exclude 'tests' "$SRC_DIR/" "$APP_DIR/src/"
chown -R root:root "$APP_DIR/src"            # service user can read, not modify, its code

echo "==> Python venv"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/src/requirements.txt"

echo "==> Data + config"
install -d -m 700 -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR"
install -d -m 750 -o root -g "$SVC_USER" "$(dirname "$ENV_FILE")"
if [[ ! -f "$ENV_FILE" ]]; then
  install -m 640 -o root -g "$SVC_USER" "$SRC_DIR/.env.example" "$ENV_FILE"
  TOKEN=$("$APP_DIR/.venv/bin/python" -c 'import secrets;print(secrets.token_urlsafe(32))')
  sed -i "s|^BT_ICS_TOKEN=.*|BT_ICS_TOKEN=$TOKEN|" "$ENV_FILE"
  echo "    Created $ENV_FILE  (edit it for timezone, week start, email, push)"
else
  echo "    Keeping existing $ENV_FILE"
fi

echo "==> Database encryption key"
NEW_KEY=0
if [[ ! -f "$KEY_FILE" ]]; then
  ( umask 027; "$APP_DIR/.venv/bin/python" -c 'import secrets;print(secrets.token_hex(32))' > "$KEY_FILE" )
  chown root:"$SVC_USER" "$KEY_FILE"; chmod 440 "$KEY_FILE"
  NEW_KEY=1
  echo "    Created $KEY_FILE"
else
  echo "    Keeping existing $KEY_FILE"
fi

manage() {  # run the admin CLI as the service user with the right env
  sudo -u "$SVC_USER" env BT_DB_PATH="$DB_FILE" BT_DB_KEY_FILE="$KEY_FILE" PYTHONPATH="$APP_DIR/src" \
    "$APP_DIR/.venv/bin/python" -m app.manage "$@"
}

manage_py() {  # run a python snippet against the app as the service user
  sudo -u "$SVC_USER" env BT_DB_PATH="$DB_FILE" BT_DB_KEY_FILE="$KEY_FILE" PYTHONPATH="$APP_DIR/src" \
    "$APP_DIR/.venv/bin/python" -c "$1"
}

cat > /usr/local/bin/billtrack-manage <<WRAP
#!/usr/bin/env bash
exec sudo -u $SVC_USER env BT_DB_PATH=$DB_FILE BT_DB_KEY_FILE=$KEY_FILE PYTHONPATH=$APP_DIR/src \\
  $APP_DIR/.venv/bin/python -m app.manage "\$@"
WRAP
chmod 755 /usr/local/bin/billtrack-manage

if [[ -f "$DB_FILE" ]] && head -c 16 "$DB_FILE" | grep -q "SQLite format 3"; then
  echo "==> Encrypting existing plaintext database"
  systemctl stop billtrack 2>/dev/null || true
  manage encrypt-db
fi

echo "==> systemd unit"
cat > /etc/systemd/system/billtrack.service <<UNIT
[Unit]
Description=BillTrack
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$APP_DIR/src
EnvironmentFile=$ENV_FILE
Environment=BT_DB_PATH=$DB_FILE
Environment=BT_DB_KEY_FILE=$KEY_FILE
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host $BIND --port $PORT --proxy-headers --forwarded-allow-ips=127.0.0.1 --no-server-header --timeout-graceful-shutdown 10
Restart=on-failure
TimeoutStopSec=20
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ReadWritePaths=$DATA_DIR
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
CapabilityBoundingSet=
SystemCallArchitectures=native
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now billtrack
systemctl restart billtrack

USERS=$(sudo -u "$SVC_USER" env BT_DB_PATH="$DB_FILE" BT_DB_KEY_FILE="$KEY_FILE" "$APP_DIR/.venv/bin/python" -c \
  "import sys;sys.path.insert(0,'$APP_DIR/src');from app import db;db.init();c=db.connect();print(c.execute('select count(*) from users').fetchone()[0])")
if [[ "$USERS" == "0" ]]; then
  echo
  echo "==> First-time settings (all changeable later in Admin > Settings)"
  DEF_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo America/Chicago)
  while true; do
    TZ_ANS=$(ask "Time zone" "${DEF_TZ:-America/Chicago}")
    [[ "$TZ_ANS" =~ ^[A-Za-z0-9_+/-]+$ ]] && manage_py "from app import settings; settings.update({'timezone': '$TZ_ANS'})" 2>/dev/null && break
    echo "  Unknown time zone. Examples: America/Chicago, America/New_York, Europe/London"
  done
  while true; do
    WD=$(ask "Which day does your pay week start? (Mon/Tue/Wed/Thu/Fri/Sat/Sun)" "Mon")
    case "${WD,,}" in mon*) N=0;; tue*) N=1;; wed*) N=2;; thu*) N=3;; fri*) N=4;; sat*) N=5;; sun*) N=6;; *) N=;; esac
    [[ -n "$N" ]] && { manage_py "from app import settings; settings.update({'week_start': $N, 'digest_weekday': $N})"; break; }
    echo "  Type a day, e.g. Fri"
  done
  if [[ $INTERACTIVE == 1 ]]; then
    while true; do
      U=$(ask "Admin username (letters, numbers, . _ -)" "")
      [[ "$U" =~ ^[A-Za-z0-9_.-]{3,32}$ ]] && break
      echo "  3-32 characters: letters, numbers, . _ -"
    done
    manage adduser "$U" admin </dev/tty
  else
    echo "  No terminal: create the admin later with  sudo billtrack-manage adduser <name> admin"
  fi
  systemctl restart billtrack   # pick up the new settings
fi

# ---------------------------------------------------------------------------------------------------------
# HTTPS proxy (nginx) + self-signed local CA
# ---------------------------------------------------------------------------------------------------------
URL="http://$BIND:$PORT"
if [[ $PROXY == 1 ]]; then
  echo "==> TLS certificate"
  install -d -m 700 -o root -g root "$TLS_DIR"
  HOST_S=$(hostname -s); HOST_F=$(hostname -f 2>/dev/null || hostname -s)
  mapfile -t IPS < <(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+(\.[0-9]+){3}$' || true)
  NAMES=("$HOST_S" "$HOST_F" "$HOST_S.local" "localhost")
  ADDRS=("127.0.0.1" "${IPS[@]}")
  IFS=',' read -ra EXTRA <<< "$EXTRA_SAN"
  for x in "${EXTRA[@]}"; do
    x="${x// /}"; [[ -z "$x" ]] && continue
    if [[ "$x" =~ ^[0-9]+(\.[0-9]+){3}$ || "$x" == *:* ]]; then ADDRS+=("$x"); else NAMES+=("$x"); fi
  done
  SAN=$( { printf 'DNS:%s\n' "${NAMES[@]}"; printf 'IP:%s\n' "${ADDRS[@]}"; } | awk '!seen[$0]++' | paste -sd, -)

  if [[ $REGEN_CA == 1 || ! -f "$TLS_DIR/ca.key" || ! -f "$TLS_DIR/ca.crt" ]]; then
    openssl ecparam -name prime256v1 -genkey -noout -out "$TLS_DIR/ca.key"
    chmod 600 "$TLS_DIR/ca.key"
    openssl req -x509 -new -key "$TLS_DIR/ca.key" -sha256 -days 3650 -out "$TLS_DIR/ca.crt" \
      -subj "/CN=BillTrack Local CA ($HOST_S)/O=BillTrack" \
      -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
      -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -addext "subjectKeyIdentifier=hash" 2>/dev/null
    REGEN_CERT=1
    echo "    New local CA created ($TLS_DIR/ca.crt). Devices must trust it once (see below)."
  fi

  NEED=$REGEN_CERT
  [[ -f "$TLS_DIR/server.crt" && -f "$TLS_DIR/server.key" ]] || NEED=1
  [[ "$(cat "$TLS_DIR/server.san" 2>/dev/null)" == "$SAN" ]] || NEED=1
  if [[ $NEED == 0 ]] && ! openssl x509 -checkend $((30*86400)) -noout -in "$TLS_DIR/server.crt" >/dev/null; then NEED=1; fi
  if [[ $NEED == 1 ]]; then
    openssl ecparam -name prime256v1 -genkey -noout -out "$TLS_DIR/server.key"
    chmod 600 "$TLS_DIR/server.key"
    EXT=$(mktemp)
    printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n' "$SAN" > "$EXT"
    openssl req -new -key "$TLS_DIR/server.key" -subj "/CN=$HOST_S" -out "$TLS_DIR/server.csr" 2>/dev/null
    # 397 days: inside the limit Apple/Chrome enforce for TLS server certificates
    openssl x509 -req -in "$TLS_DIR/server.csr" -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" -CAcreateserial \
      -days 397 -sha256 -extfile "$EXT" -out "$TLS_DIR/server.crt" 2>/dev/null
    rm -f "$EXT" "$TLS_DIR/server.csr"
    cat "$TLS_DIR/server.crt" "$TLS_DIR/ca.crt" > "$TLS_DIR/fullchain.crt"
    echo "$SAN" > "$TLS_DIR/server.san"
    echo "    Server certificate issued for: $SAN"
  else
    echo "    Keeping existing certificate ($SAN)"
  fi
  install -d -m 755 /var/www/billtrack
  install -m 644 "$TLS_DIR/ca.crt" /var/www/billtrack/billtrack-ca.crt

  echo "==> nginx (443 -> 127.0.0.1:$PORT)"
  # HTTP/2 syntax changed in nginx 1.25.1: "listen ... http2" (older) vs a separate "http2 on;" (newer)
  NGV=$(nginx -v 2>&1 | sed -n 's#.*nginx/\([0-9.]*\).*#\1#p')
  if [[ -n "$NGV" && "$(printf '%s\n%s\n' 1.25.1 "$NGV" | sort -V | head -1)" == "1.25.1" ]]; then
    H2_LISTEN=""; H2_ON="http2 on;"
  else
    H2_LISTEN=" http2"; H2_ON="# http2 enabled on the listen lines (nginx ${NGV:-unknown})"
  fi
  # Only listen on IPv6 if the kernel has it (nginx refuses to start otherwise)
  if [[ -s /proc/net/if_inet6 ]]; then
    L6_80="listen [::]:80 default_server;"; L6_443="listen [::]:443 ssl${H2_LISTEN} default_server;"
  else
    L6_80="# IPv6 not available on this host"; L6_443="$L6_80"
  fi
  cat > /etc/nginx/sites-available/billtrack <<NGINX
# Managed by BillTrack setup.sh - re-running setup overwrites this file.
server {
    listen 80 default_server;
    $L6_80
    server_name _;
    server_tokens off;
    # The CA certificate is public; served over HTTP only so a new device can fetch it before trusting HTTPS.
    location = /billtrack-ca.crt {
        root /var/www/billtrack;
        default_type application/x-x509-ca-cert;
        add_header Content-Disposition 'attachment; filename="billtrack-ca.crt"';
    }
    location / { return 301 https://\$host\$request_uri; }
}

server {
    listen 443 ssl${H2_LISTEN} default_server;
    $L6_443
    $H2_ON
    server_name _;
    server_tokens off;

    ssl_certificate     $TLS_DIR/fullchain.crt;
    ssl_certificate_key $TLS_DIR/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;
    ssl_session_timeout 1d;
    ssl_session_cache shared:BillTrackTLS:10m;
    ssl_session_tickets off;

    client_max_body_size 1m;
    client_body_timeout 15s;
    client_header_timeout 15s;

    location = /billtrack-ca.crt {
        root /var/www/billtrack;
        default_type application/x-x509-ca-cert;
    }
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;   # overwrite, never append client-supplied values
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host "";
        proxy_read_timeout 60s;
        proxy_buffering on;
    }
}
NGINX
  ln -sf /etc/nginx/sites-available/billtrack /etc/nginx/sites-enabled/billtrack
  if [[ -L /etc/nginx/sites-enabled/default ]]; then
    rm /etc/nginx/sites-enabled/default
    echo "    Disabled nginx's default site (it also listens on port 80)"
  fi
  nginx -t -q
  systemctl enable -q nginx
  systemctl reload nginx 2>/dev/null || systemctl restart nginx

  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
    echo "    ufw: opened 80/tcp (redirect + CA download) and 443/tcp"
  fi

  MAIN_IP="${IPS[0]:-127.0.0.1}"
  URL="https://$MAIN_IP"
  # Fill in Admin > Settings > Public URL on first install so email links work
  sudo -u "$SVC_USER" env BT_DB_PATH="$DB_FILE" BT_DB_KEY_FILE="$KEY_FILE" PYTHONPATH="$APP_DIR/src" \
    "$APP_DIR/.venv/bin/python" -c "from app import settings
if not settings.get('base_url'):
    settings.update({'base_url': '$URL'}); print('    Public URL set to $URL (change in Admin > Settings)')"

  systemctl restart billtrack; sleep 2   # pick up Public URL
  if curl -s --cacert "$TLS_DIR/ca.crt" --resolve "$HOST_S:443:127.0.0.1" "https://$HOST_S/healthz" | grep -q ok; then
    echo "    HTTPS check: OK"
  else
    echo "    HTTPS check FAILED - see: sudo journalctl -u billtrack -n 50 ; sudo nginx -t"
  fi
fi

if [[ $NEW_KEY == 1 ]]; then
  echo
  echo "!!  BACK UP $KEY_FILE somewhere safe (password manager). Without it the database cannot be opened."
fi
echo
echo "==> Done. BillTrack is at: $URL"
if [[ $PROXY == 1 ]]; then
  FP=$(openssl x509 -in "$TLS_DIR/ca.crt" -noout -fingerprint -sha256 | cut -d= -f2)
  cat <<MSG

    Trust the BillTrack CA once on each device to get rid of the browser warning:
      1. On the device, open  http://$MAIN_IP/billtrack-ca.crt
      2. Check its SHA-256 fingerprint matches:
         $FP
      3. Install it:
         - iPhone/iPad: Settings > Profile Downloaded > Install, then
                        Settings > General > About > Certificate Trust Settings > enable "BillTrack Local CA"
         - Android:     Settings > Security > Encryption & credentials > Install a certificate > CA certificate
         - Windows:     double-click > Install Certificate > Local Machine > Trusted Root Certification Authorities
         - macOS:       open in Keychain Access (System) > double-click > Trust: Always Trust
         - Linux/Firefox: Firefox has its own store: Settings > Certificates > View > Authorities > Import
    Names on the certificate: $SAN
    New IP or hostname later? Re-run sudo ./setup.sh, answer "n" to keep settings, and add it.
MSG
fi
systemctl --no-pager --lines=0 status billtrack || true
