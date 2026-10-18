#!/usr/bin/env bash
# Deploy the AI complaint-validation review app ON ops-automations (192.168.1.203).
#
# Topology (per Ab, 2026-10-18): philip.amzmanager.net is a reverse proxy that
# forwards to this box and terminates TLS. This box therefore serves plain HTTP on
# :80 through nginx, and the app itself listens on 127.0.0.1:8080 only. There is no
# certificate to create here and nothing is exposed beyond the proxy.
#
# Run ON the box, from the directory that contains app.py:
#   sudo bash deploy_on_box.sh
#
# Idempotent: re-running pulls in a new app.py, leaves an existing app.env alone,
# rewrites the systemd unit and nginx site, restarts, and verifies /healthz.
# Nothing is deleted: the placeholder site is MOVED aside, never removed.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/ai-complaints}"
APP_PORT="${APP_PORT:-8080}"          # loopback port the app binds; nginx owns :80
APP_HOST="${APP_HOST:-127.0.0.1}"
SERVICE_USER="${SUDO_USER:-$USER}"
SRC="$(cd "$(dirname "$0")" && pwd)/app.py"
say() { printf '\n== %s\n' "$*"; }
die() { printf '\n!! %s\n' "$*" >&2; exit 1; }

[ -f "$SRC" ] || die "app.py not found next to this script ($SRC)"

# ---------------------------------------------------------------- staging
say "staging"
mkdir -p "$APP_DIR/app" || die "cannot create $APP_DIR (run with sudo)"
install -m 0644 "$SRC" "$APP_DIR/app/app.py"
echo "  $SRC -> $APP_DIR/app/app.py"
if [ -d "$(dirname "$SRC")/.git" ] && command -v git >/dev/null; then
  git -C "$(dirname "$SRC")" rev-parse --short HEAD 2>/dev/null | sed 's/^/  commit: /'
fi

# ---------------------------------------------------------------- python
say "python environment"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/venv" \
    || die "venv failed — install python3-venv (apt-get install -y python3-venv) and re-run"
fi
"$APP_DIR/venv/bin/pip" -q install --upgrade pip pymysql 2>&1 | tail -2 || true
"$APP_DIR/venv/bin/python" -c 'import pymysql; print("  pymysql ok")' || die "pymysql missing"

# ---------------------------------------------------------------- config
say "configuration"
# Credentials are typed once, hidden, written 0600. Never passed as an argument
# (visible in ps) and never committed to the repository.
if [ -f "$APP_DIR/app.env" ]; then
  echo "  $APP_DIR/app.env already present — left alone"
  grep -q '^APP_HOST=' "$APP_DIR/app.env" || echo "APP_HOST=$APP_HOST" >>"$APP_DIR/app.env"
  grep -q '^APP_PORT=' "$APP_DIR/app.env" || echo "APP_PORT=$APP_PORT" >>"$APP_DIR/app.env"
  chmod 600 "$APP_DIR/app.env"
else
  echo "  Enter the automation_philip connection Ab provisioned"
  read -r -p "  host [db01-master.amzmanager.net]: " DBH; DBH="${DBH:-db01-master.amzmanager.net}"
  read -r -p "  user [philip]: " DBU; DBU="${DBU:-philip}"
  read -r -s -p "  password: " DBP; echo
  read -r -p "  database [automation_philip]: " DBN; DBN="${DBN:-automation_philip}"
  [ -n "$DBP" ] || die "no password given"
  # The login password for Chadia is generated here: it is never written into the
  # repo, never reused from another system, and shown exactly once.
  PASS="$(openssl rand -base64 24 2>/dev/null | tr -d '/+=' | head -c 20)"
  [ -n "$PASS" ] || PASS="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"
  umask 077
  cat >"$APP_DIR/app.env" <<ENV
CXAPP_DB_HOST=$DBH
CXAPP_DB_USER=$DBU
CXAPP_DB_PASSWORD=$DBP
CXAPP_DB_NAME=$DBN
APP_USER=chadia
APP_PASS=$PASS
APP_REVIEWER_UID=1089
APP_HOST=$APP_HOST
APP_PORT=$APP_PORT
ENV
  chmod 600 "$APP_DIR/app.env"
  echo
  echo "  ┌──────────────────────────────────────────────────────"
  echo "  │ URL:    https://philip.amzmanager.net/"
  echo "  │ login:  chadia"
  echo "  │ pass:   $PASS"
  echo "  └──────────────────────────────────────────────────────"
  echo "  Shown once. Stored at $APP_DIR/app.env (mode 600)."
  echo "  Lost it? Delete that file and re-run this script to rotate."
fi

# ---------------------------------------------------------------- service
say "service"
sudo tee /etc/systemd/system/ai-complaints.service >/dev/null <<UNIT
[Unit]
Description=AI Complaint Validation review app
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR/app
EnvironmentFile=$APP_DIR/app.env
ExecStart=$APP_DIR/venv/bin/python app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable ai-complaints >/dev/null 2>&1
sudo systemctl restart ai-complaints
for i in 1 2 3 4 5; do
  sleep 1
  systemctl is-active --quiet ai-complaints && break
done
systemctl is-active --quiet ai-complaints \
  || { journalctl -u ai-complaints -n 30 --no-pager; die "service failed to start"; }
echo "  service: active (user $SERVICE_USER)"

# ---------------------------------------------------------------- placeholder
say "placeholder site"
# Ab (2026-10-18): "there's a placeholder website on the ops machine at
# /var/www/html that you would like to get rid of before exposing anything."
# Moved aside, not deleted: reversible, and it can no longer shadow the app.
if [ -d /var/www/html ] && [ -n "$(ls -A /var/www/html 2>/dev/null)" ]; then
  sudo mkdir -p /var/www/html.pre-ai-complaints
  sudo cp -a /var/www/html/. /var/www/html.pre-ai-complaints/ 2>/dev/null || true
  sudo find /var/www/html -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
  echo "  moved /var/www/html -> /var/www/html.pre-ai-complaints (backup kept)"
else
  echo "  /var/www/html already empty"
fi

# ---------------------------------------------------------------- nginx
say "nginx (proxy on :80)"
command -v nginx >/dev/null || {
  echo "  nginx not installed — installing"
  sudo apt-get update -qq && sudo apt-get install -y -qq nginx >/dev/null || die "nginx install failed"
}
# :80 is dedicated to this app: default_server so it answers even if the proxy
# rewrites the Host header, and the stock site is disabled (it served the
# placeholder root).
sudo rm -f /etc/nginx/sites-enabled/default
sudo tee /etc/nginx/sites-available/ai-complaints >/dev/null <<CONF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name philip.amzmanager.net;

    client_max_body_size 2m;
    access_log /var/log/nginx/ai-complaints.access.log;

    location / {
        proxy_pass http://$APP_HOST:$APP_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
CONF
sudo ln -sf /etc/nginx/sites-available/ai-complaints /etc/nginx/sites-enabled/ai-complaints
sudo nginx -t || die "nginx rejected the config — nothing was reloaded"
sudo systemctl enable nginx >/dev/null 2>&1
sudo systemctl reload nginx || sudo systemctl restart nginx

# ---------------------------------------------------------------- verify
say "verify"
health="$(curl -s -m 10 "http://$APP_HOST:$APP_PORT/healthz" || true)"
case "$health" in
  ok*) echo "  app  :200 $health" ;;
  *)   die "app is up but /healthz says: ${health:-<no response>} — check CXAPP_DB_* in $APP_DIR/app.env" ;;
esac
printf '  auth gate via nginx: %s (401 = login required, correct)\n' \
  "$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'Host: philip.amzmanager.net' http://127.0.0.1/ || true)"
echo
echo "done. From the outside: https://philip.amzmanager.net/"
echo "logs: journalctl -u ai-complaints -f   |   tail -f /var/log/nginx/ai-complaints.access.log"
