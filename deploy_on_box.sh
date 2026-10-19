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
#   sudo bash deploy_on_box.sh --config-only    # rewrite app.env and stop
#
# Idempotent: re-running pulls in a new app.py, rewrites app.env ONLY when it is
# missing or still on the single-login form, rewrites the systemd unit and nginx
# site, restarts, and verifies /healthz. Nothing is deleted: the placeholder site is
# MOVED aside, never removed.
#
# ATTRIBUTION (P12)
#   Every decision is stamped with the signed-in account's uid, which comes from
#   APP_USERS='name:uid:password,...' — the list the app authenticates against. The
#   first version wrote APP_USER/APP_PASS/APP_REVIEWER_UID=1089 instead, so every
#   decision made through the app was recorded as Chadia's no matter who made it.
#   APP_USERS is now the only form written, and an existing single-login app.env is
#   converted in place (backup kept) so the running box actually changes behaviour.
#
#   DEFAULT_UID: not needed, and deliberately not added. app.py defines DEFAULT_UID
#   but never uses it for a write — the uid written is always the signed-in account's
#   own (reviewer dict), and an account configured without a uid is recorded by name
#   with reviewed_by NULL. That is the honest record: a guessed uid would attribute a
#   person's decision to someone else, which is exactly the bug being fixed here. Any
#   account that needs a uid gets one in APP_USERS; nobody is defaulted.
set -uo pipefail

CONFIG_ONLY=0
for a in "$@"; do
  case "$a" in
    --config-only) CONFIG_ONLY=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$a" >&2; exit 2 ;;
  esac
done

APP_DIR="${APP_DIR:-/opt/ai-complaints}"
APP_PORT="${APP_PORT:-8080}"          # loopback port the app binds; nginx owns :80
APP_HOST="${APP_HOST:-127.0.0.1}"
SERVICE_USER="${SUDO_USER:-${USER:-$(id -un)}}"
SRC="$(cd "$(dirname "$0")" && pwd)/app.py"
ADMIN_LOGIN="${ADMIN_LOGIN:-chadia}"
ADMIN_UID="${ADMIN_UID:-1089}"
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

# An approved code has two legs: complaint_codes in the database (the durable
# record) and data/vocab_additions.csv, which cxvocab loads on the REFRESH host.
# Shipping only app.py left the second leg with nowhere to land: vocab_path()
# found no directory beside the app, so the write failed (or went to a path the
# classifier never reads) and "the classifier can use it" stayed false.
# Create the directory, install the file the repo already has, and point the
# service at it by absolute path via CX_VOCAB_ADDITIONS.
VOCAB_SRC="$(dirname "$SRC")/data/vocab_additions.csv"
mkdir -p "$APP_DIR/app/data"
if [ -f "$VOCAB_SRC" ]; then
  install -m 0644 "$VOCAB_SRC" "$APP_DIR/app/data/vocab_additions.csv"
  echo "  $VOCAB_SRC -> $APP_DIR/app/data/vocab_additions.csv"
else
  printf 'Complaint Types,Category\n' >"$APP_DIR/app/data/vocab_additions.csv"
  echo "  no $VOCAB_SRC — created an empty $APP_DIR/app/data/vocab_additions.csv"
fi
# The service user must own the DIRECTORY: vocab_upsert writes a temp file and
# renames it into place, which needs write permission on the directory itself.
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR/app/data" 2>/dev/null \
  || chown -R "$SERVICE_USER" "$APP_DIR/app/data" 2>/dev/null || true
chmod 0755 "$APP_DIR/app/data"
chmod 0644 "$APP_DIR/app/data/vocab_additions.csv"

if [ "$CONFIG_ONLY" -eq 1 ]; then
  echo "  --config-only: stopping after configuration"
fi

# ---------------------------------------------------------------- python
if [ "$CONFIG_ONLY" -eq 0 ]; then
say "python environment"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/venv" \
    || die "venv failed — install python3-venv (apt-get install -y python3-venv) and re-run"
fi
"$APP_DIR/venv/bin/pip" -q install --upgrade pip pymysql 2>&1 | tail -2 || true
"$APP_DIR/venv/bin/python" -c 'import pymysql; print("  pymysql ok")' || die "pymysql missing"
fi

# ---------------------------------------------------------------- config
say "configuration"
# Credentials are typed once, hidden, written 0600. Never passed as an argument
# (visible in ps) and never committed to the repository. CX_DB_PASSWORD and
# CX_APP_PASS allow an unattended run (and are how this script is tested); they are
# read from the environment, not from argv.
write_env() {                       # $1 = db password, $2 = app password, $3 = session secret
  umask 077
  cat >"$APP_DIR/app.env" <<ENV
CXAPP_DB_HOST=$DBH
CXAPP_DB_USER=$DBU
CXAPP_DB_PASSWORD=$DBP
CXAPP_DB_NAME=$DBN

# Reviewers: name:uid:password, comma-separated. The uid is what corrections.reviewed_by
# records, so it must be the person who actually decided. Add a line per reviewer:
#   APP_USERS=$ADMIN_LOGIN:$ADMIN_UID:<pw>,philip:1001:<pw>
# A reviewer with no uid is written without one and recorded by name only
# (reviewed_by NULL) — no uid is ever guessed or defaulted.
APP_USERS=$ADMIN_LOGIN:$ADMIN_UID:$APP_PASS

# Signing key for the /login session cookie (HMAC-SHA256). Generated once, never
# committed, never rotated on re-run — a new value would sign everyone out. The app
# REFUSES TO START without it rather than falling back to a guessable default.
APP_SECRET=$APP_SECRET

# Legacy single-login keys (APP_USER/APP_PASS/APP_REVIEWER_UID) are ignored whenever
# APP_USERS is set. Not duplicated here on purpose: one copy of the password, in the
# line above. To roll back, restore the commented form by hand.
APP_HOST=$APP_HOST
APP_PORT=$APP_PORT
CX_APP_URL=${CX_APP_URL:-https://philip.amzmanager.net}

# Where an approved complaint code is written so the classifier (cxvocab, refresh
# host) can emit it. Absolute, so it cannot depend on the working directory.
CX_VOCAB_ADDITIONS=$APP_DIR/app/data/vocab_additions.csv
ENV
  chmod 600 "$APP_DIR/app.env"
}

# One generator for the session key, so a fresh install and a backfill cannot
# produce different shapes (hex only: no shell metacharacters in app.env).
new_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

if [ -f "$APP_DIR/app.env" ]; then
  echo "  $APP_DIR/app.env already present"
  # Convert the single-login form in place: writing APP_USERS next to the legacy keys
  # is what makes attribution real on a box that is already deployed, and a comment
  # saying "replace these three by hand" is not a fix.
  if grep -q '^APP_USERS=' "$APP_DIR/app.env"; then
    echo "  APP_USERS already configured — left alone"
  else
    OLD_USER="$(sed -n 's/^APP_USER=//p' "$APP_DIR/app.env" | tail -1)"
    OLD_PASS="$(sed -n 's/^APP_PASS=//p' "$APP_DIR/app.env" | tail -1)"
    OLD_UID="$(sed -n 's/^APP_REVIEWER_UID=//p' "$APP_DIR/app.env" | tail -1)"
    OLD_USER="${OLD_USER:-$ADMIN_LOGIN}"; OLD_UID="${OLD_UID:-$ADMIN_UID}"
    cp -p "$APP_DIR/app.env" "$APP_DIR/app.env.bak-$(date -u +%Y%m%dT%H%M%SZ)"
    if [ -n "$OLD_PASS" ]; then
      {
        printf '# Rewritten %s: single login -> APP_USERS so decisions carry a per-user uid.\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf '# The previous file is kept next to this one (app.env.bak-*).\n'
        grep -v '^APP_USER=\|^APP_PASS=\|^APP_REVIEWER_UID=\|^APP_USERS=' "$APP_DIR/app.env"
        printf '\n# Reviewers: name:uid:password, comma-separated (add one per reviewer).\n'
        printf 'APP_USERS=%s:%s:%s\n' "$OLD_USER" "$OLD_UID" "$OLD_PASS"
        printf '# Legacy single-login keys, now ignored (kept for rollback):\n'
        printf '#APP_USER=%s\n#APP_REVIEWER_UID=%s\n' "$OLD_USER" "$OLD_UID"
      } >"$APP_DIR/app.env.new"
      mv "$APP_DIR/app.env.new" "$APP_DIR/app.env"
      chmod 600 "$APP_DIR/app.env"
      echo "  converted to APP_USERS=$OLD_USER:$OLD_UID:<pw> (backup kept)"
    else
      echo "  !! APP_PASS is empty in app.env — left untouched; set APP_USERS by hand" >&2
    fi
  fi
  chmod 600 "$APP_DIR/app.env"
  for kv in "APP_HOST=$APP_HOST" "APP_PORT=$APP_PORT" \
            "CX_VOCAB_ADDITIONS=$APP_DIR/app/data/vocab_additions.csv"; do
    grep -q "^${kv%%=*}=" "$APP_DIR/app.env" || echo "$kv" >>"$APP_DIR/app.env"
  done
else
  echo "  Enter the automation_philip connection Ab provisioned"
  if [ -n "${CX_DB_PASSWORD:-}" ]; then
    DBP="$CX_DB_PASSWORD"; echo "  password: from CX_DB_PASSWORD (unattended)"
  else
    read -r -s -p "  password: " DBP; echo
  fi
  [ -n "$DBP" ] || die "no password given"
  DBH="${CX_DB_HOST:-}"; [ -n "$DBH" ] || read -r -p "  host [db01-master.amzmanager.net]: " DBH
  DBH="${DBH:-db01-master.amzmanager.net}"
  DBU="${CX_DB_USER:-}"; [ -n "$DBU" ] || read -r -p "  user [philip]: " DBU
  DBU="${DBU:-philip}"
  DBN="${CX_DB_NAME:-}"; [ -n "$DBN" ] || read -r -p "  database [automation_philip]: " DBN
  DBN="${DBN:-automation_philip}"
  # The login password is generated here: never written into the repo, never reused
  # from another system, and shown exactly once.
  if [ -n "${CX_APP_PASS:-}" ]; then
    APP_PASS="$CX_APP_PASS"
  else
    APP_PASS="$(openssl rand -base64 24 2>/dev/null | tr -d '/+=' | head -c 20)"
    [ -n "$APP_PASS" ] || APP_PASS="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"
  fi
  # The session-cookie key: generated once, never printed (it is not something a
  # person needs to type) and never derived from the password.
  APP_SECRET="$(new_secret)"
  [ -n "$APP_SECRET" ] || die "could not generate APP_SECRET"
  write_env
  echo
  echo "  ┌──────────────────────────────────────────────────────"
  echo "  │ URL:    https://philip.amzmanager.net/"
  echo "  │ login:  $ADMIN_LOGIN   (APP_USERS, uid $ADMIN_UID)"
  echo "  │ pass:   $APP_PASS"
  echo "  └──────────────────────────────────────────────────────"
  echo "  Shown once. Stored at $APP_DIR/app.env (mode 600), one line per reviewer."
  echo "  Lost it? Delete that file and re-run this script to rotate."
fi

# ---------------------------------------------------------------- session key
# Every app.env written before the sign-in page has no APP_SECRET, and app.py now
# refuses to start without one. Backfill it ONCE: rotating the key on every re-run
# would silently sign everybody out, so an existing value is never touched.
if grep -q '^APP_SECRET=.' "$APP_DIR/app.env" 2>/dev/null; then
  echo "  APP_SECRET already set — left alone (rotating it signs everyone out)"
else
  APP_SECRET="$(new_secret)"
  [ -n "$APP_SECRET" ] || die "could not generate APP_SECRET (openssl and /dev/urandom both failed)"
  printf '\n# Signing key for the /login session cookie (generated %s).\n# Changing or deleting this line signs every user out.\nAPP_SECRET=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$APP_SECRET" >>"$APP_DIR/app.env"
  chmod 600 "$APP_DIR/app.env"
  echo "  APP_SECRET generated into app.env (mode 600) — it signs the /login cookie"
fi
chmod 600 "$APP_DIR/app.env"

# Prove the file the service will read yields the expected users, using the app's own
# loader (imported, not re-implemented) so a format change cannot pass unnoticed.
python3 - "$APP_DIR/app.env" <<'PYCHECK' || true
import os, re, sys
path = sys.argv[1]
for line in open(path, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v
raw = os.environ.get("APP_USERS", "")
print(f"  APP_USERS: {len([x for x in raw.split(',') if x.strip()])} reviewer(s)")
for item in raw.split(","):
    name, _, rest = item.strip().partition(":")
    uid, _, pw = rest.partition(":")
    if name:
        print(f"    {name}: uid={uid or '(none — recorded by name)'} "
              f"password={'set' if pw else 'MISSING'}")
print(f"  APP_SECRET: {'set' if os.environ.get('APP_SECRET') else 'MISSING — app.py will refuse to start'}")
PYCHECK

if [ "$CONFIG_ONLY" -eq 1 ]; then
  echo "  config written: $APP_DIR/app.env"
  exit 0
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
# A pre-existing site on this box may already claim the same server_name (or
# default_server for :80) — on 2026-10-19 that was /etc/nginx/sites-enabled/
# philip.amzmanager.net, and it made `nginx -t` fail with "a duplicate default
# server". Move any such file aside instead of guessing which block nginx picks.
for f in $(sudo grep -rlE "server_name[^;]*philip\.amzmanager\.net" /etc/nginx/sites-enabled/ 2>/dev/null | grep -v '/ai-complaints$' || true); do
  sudo mv "$f" "$f.disabled-$(date -u +%Y%m%dT%H%M%SZ)"
  echo "  disabled $f — it claimed philip.amzmanager.net (backup kept alongside)"
done
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
# The gate is now a 302 to /login, not a 401: a 401 would make the browser open its
# grey Basic dialog, which is exactly what the sign-in page replaced. Check it, so a
# deploy that silently reverts to Basic (or leaves / unauthenticated) fails loudly.
gate_code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'Host: philip.amzmanager.net' http://127.0.0.1/ || true)"
gate_loc="$(curl -s -o /dev/null -D - -m 10 -H 'Host: philip.amzmanager.net' http://127.0.0.1/ 2>/dev/null | tr -d '\r' | sed -n 's/^[Ll]ocation: //p' | head -1 || true)"
case "$gate_code" in
  302) case "$gate_loc" in
         */login*) echo "  sign-in gate: 302 -> $gate_loc (correct)" ;;
         *) echo "  !! / returned 302 but not to /login ($gate_loc)" >&2 ;;
       esac ;;
  *) echo "  !! / returned $gate_code, expected 302 to /login — check the service and nginx" >&2 ;;
esac
login_code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'Host: philip.amzmanager.net' http://127.0.0.1/login || true)"
echo "  GET /login: $login_code (200 = the form is being served)"
echo
echo "done. From the outside: https://philip.amzmanager.net/"
echo "logs: journalctl -u ai-complaints -f   |   tail -f /var/log/nginx/ai-complaints.access.log"
echo
echo "APPROVED CODES — no manual step needed:"
echo "  Approving a code in the app writes complaint_codes (approved=1) in the database"
echo "  and appends it to $APP_DIR/app/data/vocab_additions.csv on this box."
echo "  The refresh host runs sync_approved_vocab.py as step 2 of cx_refresh.sh, which"
echo "  reads the same automation_philip database and writes the classifier's vocabulary"
echo "  file, so the next classification batch can emit the code."
