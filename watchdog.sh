#!/usr/bin/env bash
# Restart ai-complaints if it stops answering. Runs from cron every 5 minutes.
#
# Why this exists: on 2 Nov the app wedged. systemd reported the unit as
# running (Restart=always only fires when the PROCESS exits, and it never did),
# so nothing restarted it and the site served 504s until a human noticed.
# A wedged process and a dead process look identical from outside; this checks
# the thing that actually matters — whether it answers — not whether a pid exists.
set -uo pipefail

UNIT=ai-complaints
URL=http://127.0.0.1:8080/healthz
LOG=/var/log/ai-complaints-watchdog.log
MAXFAIL=3

fail=0
code=""
for _ in $(seq 1 $MAXFAIL); do
  code=$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 10 "$URL" 2>/dev/null || echo "000")
  if [ "$code" = "200" ]; then
    exit 0                      # healthy: stay silent, the log is for events
  fi
  fail=$((fail + 1))
  sleep 2
done

echo "$(date -Is) healthz failed $fail/$MAXFAIL (last code '$code') — restarting $UNIT" >> "$LOG"
systemctl restart "$UNIT"

sleep 6
code=$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 15 "$URL" 2>/dev/null || echo "000")
if [ "$code" = "200" ]; then
  echo "$(date -Is) restart ok — healthz 200" >> "$LOG"
else
  echo "$(date -Is) STILL DOWN after restart — healthz '$code'. Needs a human." >> "$LOG"
fi
