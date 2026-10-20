#!/usr/bin/env bash
# Build + deploy the AI complaint dashboard to Cloudflare Pages, then gate it
# behind Cloudflare Access (Google SSO, UEP domain).
#
# The site is built from automation_philip and contains customer review text, so
# it must never be publicly readable: Access is applied as part of deploying, not
# afterwards. The wildcard destination matters — every <hash>.project.pages.dev
# alias serves the same site, so an app bound only to the production hostname
# leaves each alias as a working bypass.
set -uo pipefail

ENVF=/opt/data/.env
A=7e26155cd322d5d5c48f3de19bac611f
PROJECT=uep-ai-complaints
DOMAIN="$PROJECT.pages.dev"
ROOT=/opt/data/ai-complaint-validation
SITE="$ROOT/site"
STAGE="$ROOT/.cf-deploy"
WRANGLER=/opt/data/cloudflare-tools/node_modules/.bin/wrangler
LOG="$ROOT/.cf_deploy.log"

TOKEN="$(grep -E '^CLOUDFLARE_API_TOKEN=' "$ENVF" | tail -1 | cut -d= -f2-)"
[ -n "$TOKEN" ] || { echo "no CLOUDFLARE_API_TOKEN in $ENVF"; exit 1; }
[ -f "$SITE/index.html" ] || { echo "site/index.html missing — run build_dashboard.py"; exit 1; }
[ -f "$SITE/data/reviews.json" ] || { echo "site/data/reviews.json missing"; exit 1; }
[ -x "$WRANGLER" ] || { echo "wrangler not found at $WRANGLER"; exit 1; }

echo "== staging"
rm -rf "$STAGE"; mkdir -p "$STAGE/data"
cp "$SITE/index.html" "$STAGE/index.html"
cp "$SITE/data/reviews.json" "$STAGE/data/reviews.json"
# charts.js is loaded by index.html; codes/quality are separate pages linked from
# the sidebar. Missing them ships a page that 404s its script and its nav links.
for f in charts.js codes.html quality.html; do
  [ -f "$SITE/$f" ] && cp "$SITE/$f" "$STAGE/$f" || echo "  note: $f not present, skipping"
done
# The brand mark is referenced RELATIVELY from every page's sidebar
# (assets/logo-mark-light.png), so the directory has to travel with them or the
# published header ships a broken image.
if [ -d "$SITE/assets" ]; then
  mkdir -p "$STAGE/assets"
  cp "$SITE"/assets/*.png "$STAGE/assets/"
  echo "  assets: $(ls "$STAGE/assets" | tr '\n' ' ')"
fi

echo "== deploying"
export CLOUDFLARE_API_TOKEN="$TOKEN" CLOUDFLARE_ACCOUNT_ID="$A"

# A Pages project must exist before `pages deploy`; creating it is idempotent —
# the call fails harmlessly when it is already there.
"$WRANGLER" pages project create "$PROJECT" --production-branch=main >> "$LOG" 2>&1 || true

if ! "$WRANGLER" pages deploy "$STAGE" --project-name="$PROJECT" --branch=main --commit-dirty=true >> "$LOG" 2>&1; then
  echo "deploy failed:"; tail -20 "$LOG"; exit 1
fi
tail -3 "$LOG"
echo "deployed: https://$DOMAIN/"

echo "== access"
H=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json")
app_id="$(curl -s "${H[@]}" "https://api.cloudflare.com/client/v4/accounts/$A/access/apps" \
  | python3 -c "
import sys,json
for a in (json.load(sys.stdin).get('result') or []):
    if a.get('domain')=='$DOMAIN': print(a['id']); break
")"

if [ -z "$app_id" ]; then
  echo "access: creating app"
  resp="$(curl -s "${H[@]}" -X POST "https://api.cloudflare.com/client/v4/accounts/$A/access/apps" -d "{
    \"name\":\"UEP AI Complaint Validation\",
    \"domain\":\"$DOMAIN\",
    \"type\":\"self_hosted\",
    \"session_duration\":\"24h\",
    \"app_launcher_visible\":true,
    \"destinations\":[{\"type\":\"public\",\"uri\":\"$DOMAIN\"},{\"type\":\"public\",\"uri\":\"*.$DOMAIN\"}]
  }")"
  app_id="$(printf '%s' "$resp" | python3 -c "import sys,json;print((json.load(sys.stdin).get('result') or {}).get('id',''))")"
  [ -n "$app_id" ] || { echo "access: FAILED"; printf '%s' "$resp" | head -c 400; echo; exit 1; }
  echo "access: app $app_id"
else
  echo "access: app exists ($app_id)"
fi

echo "access: ensuring wildcard destination"
curl -s "${H[@]}" -X PUT "https://api.cloudflare.com/client/v4/accounts/$A/access/apps/$app_id" -d "{
  \"name\":\"UEP AI Complaint Validation\",
  \"domain\":\"$DOMAIN\",
  \"type\":\"self_hosted\",
  \"session_duration\":\"24h\",
  \"destinations\":[{\"type\":\"public\",\"uri\":\"$DOMAIN\"},{\"type\":\"public\",\"uri\":\"*.$DOMAIN\"}]
}" | python3 -c "import sys,json;d=json.load(sys.stdin);print('access: destinations', [x.get('uri') for x in ((d.get('result') or {}).get('destinations') or [])] if d.get('success') else 'FAILED '+json.dumps(d.get('errors'))[:200])"

# Access is scoped to the people who need it, NOT to the whole email domain.
# site/data/reviews.json carries review text, order ids, ASINs and SKUs for every
# row in the corpus; "anyone with an @upperechelonproducts.com Google account"
# is a much larger set than the two people who work this queue.
ACCESS_EMAILS="${ACCESS_EMAILS:-chadia@upperechelonproducts.com philip@upperechelonproducts.com}"
POL_BODY="$(ACCESS_EMAILS="$ACCESS_EMAILS" python3 - <<'PY'
import json, os, re
raw = os.environ["ACCESS_EMAILS"]
emails = [e for e in re.split(r"[,\s]+", raw) if e]
print(json.dumps({"name": "UEP staff (Google SSO)", "decision": "allow",
                  "include": [{"email": {"email": e}} for e in emails],
                  "require": [], "exclude": []}))
PY
)"
echo "access: policy target = $(printf '%s' "$POL_BODY" | python3 -c 'import sys,json;print(", ".join(x["email"]["email"] for x in json.load(sys.stdin)["include"]))')"
pol_id="$(curl -s "${H[@]}" "https://api.cloudflare.com/client/v4/accounts/$A/access/apps/$app_id/policies" \
  | python3 -c "
import sys,json
for p in (json.load(sys.stdin).get('result') or []):
    if p.get('name')=='UEP staff (Google SSO)': print(p['id']); break
")"
if [ -n "$pol_id" ]; then
  echo "access: narrowing the existing policy ($pol_id)"
  out="$(curl -s "${H[@]}" -X PUT "https://api.cloudflare.com/client/v4/accounts/$A/access/apps/$app_id/policies/$pol_id" -d "$POL_BODY")"
else
  echo "access: creating the policy"
  out="$(curl -s "${H[@]}" -X POST "https://api.cloudflare.com/client/v4/accounts/$A/access/apps/$app_id/policies" -d "$POL_BODY")"
fi
printf '%s' "$out" | python3 -c "
import sys,json
d=json.load(sys.stdin)
r=d.get('result') or {}
print('access: policy', 'OK' if d.get('success') else 'FAILED '+json.dumps(d.get('errors'))[:300])
if d.get('success'):
    inc=r.get('include') or []
    who=[x.get('email',{}).get('email') or x.get('email_domain',{}).get('domain') or json.dumps(x) for x in inc]
    print('access: scope is now', who)
    if any(x.get('email_domain') for x in inc):
        print('access: !! still domain-wide')
"
# Show EVERY policy so a second, wider one cannot sit quietly beside this one.
curl -s "${H[@]}" "https://api.cloudflare.com/client/v4/accounts/$A/access/apps/$app_id/policies" \
  | python3 -c "
import sys,json
d=json.load(sys.stdin)
for p in (d.get('result') or []):
    inc=[x.get('email',{}).get('email') or x.get('email_domain',{}).get('domain') or json.dumps(x) for x in (p.get('include') or [])]
    print('access: policy %-28s %-5s %s' % (p.get('name'), p.get('decision'), inc))
"

echo
echo "verify the gate (Access takes a few seconds to propagate — poll, never sample once):"
code=""
for i in $(seq 1 12); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/")"
  [ "$code" = "302" ] && break
  sleep 5
done
echo "  https://$DOMAIN/ -> $code"
if [ "$code" != "302" ]; then
  echo "  !! STILL PUBLIC after 60s — the Access app is not enforcing. Do not share this URL."
  exit 1
fi
alias_code="$(curl -s -o /dev/null -w '%{http_code}' "https://$(tail -1 "$LOG" | grep -oE '[a-f0-9]+\.'"$DOMAIN" | head -1)")"
echo "  per-deploy alias -> ${alias_code:-n/a} (the wildcard destination must also gate it)"
echo
echo "open: https://$DOMAIN/"
