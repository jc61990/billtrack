#!/usr/bin/env bash
# BillTrack bare-metal installer for Ubuntu 22.04/24.04. Safe to re-run:
#   - code in $APP_DIR is replaced from THIS checkout (git is the source of truth)
#   - env file, database key and database are NEVER overwritten
#   - a v1.0 plaintext database is encrypted automatically (plaintext copy kept as .plain.bak)
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/billtrack}
DATA_DIR=${DATA_DIR:-/var/lib/billtrack}
ENV_FILE=${ENV_FILE:-/etc/billtrack/billtrack.env}
KEY_FILE=${KEY_FILE:-/etc/billtrack/db.key}
DB_FILE="$DATA_DIR/billtrack.db"
BIND=${BIND:-127.0.0.1}
PORT=${PORT:-8080}
SVC_USER=billtrack
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo ./setup.sh"; exit 1; }
[[ -f "$SRC_DIR/app/main.py" ]] || { echo "Run from the repo root"; exit 1; }

echo "==> Packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv rsync >/dev/null

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
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host $BIND --port $PORT --proxy-headers --forwarded-allow-ips=127.0.0.1 --no-server-header
Restart=on-failure
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
  read -rp "Create the admin login now. Username: " U
  manage adduser "$U" admin
fi

if [[ $NEW_KEY == 1 ]]; then
  echo
  echo "!!  BACK UP $KEY_FILE somewhere safe (password manager). Without it the database cannot be opened."
  echo
fi
echo "==> Done. Listening on http://$BIND:$PORT  (put HTTPS in front; see README)"
systemctl --no-pager --lines=0 status billtrack || true
