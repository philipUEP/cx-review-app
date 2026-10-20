#!/usr/bin/env bash
# Deploy whatever is on origin/main. Safe to run every 5 minutes.
#
# The rule: never leave the box in a state where the app is broken and nobody
# knows. Every run either makes no change, deploys a known commit and proves it
# healthy, or puts the previous commit back. It never half-deploys, and it never
# exits non-zero on a routine run (a failed fetch is a network blip, not an alarm).
set -euo pipefail

REPO=/opt/ai-complaints
LOG=/var/log/ai-complaints-autopull.log
exec 9>/var/lock/ai-complaints-autopull.lock
flock -n 9 || exit 0        # another run is mid-deploy; skip quietly

cd "$REPO"

if ! git fetch -q origin main; then
  echo "$(date -Is) fetch failed (network or credentials) — nothing changed" >> "$LOG"
  exit 0
fi

CUR=$(git rev-parse HEAD)
NEW=$(git rev-parse origin/main)
[ "$CUR" = "$NEW" ] && exit 0

echo "$(date -Is) deploying ${CUR:0:7} -> ${NEW:0:7}" >> "$LOG"
git reset -q --hard origin/main

# app.env is untracked, so the reset above never touches the secrets.
if bash "$REPO/deploy_on_box.sh" >> "$LOG" 2>&1 &&
   curl -fsS --max-time 10 http://127.0.0.1:8080/healthz | grep -q 'ok db=ok'; then
  echo "$(date -Is) ok ${NEW:0:7}" >> "$LOG"
else
  echo "$(date -Is) FAILED ${NEW:0:7} — rolling back to ${CUR:0:7}" >> "$LOG"
  git reset -q --hard "$CUR"
  if bash "$REPO/deploy_on_box.sh" >> "$LOG" 2>&1; then
    echo "$(date -Is) rolled back, running ${CUR:0:7}" >> "$LOG"
  else
    echo "$(date -Is) ROLLBACK ALSO FAILED — needs a human" >> "$LOG"
  fi
fi
