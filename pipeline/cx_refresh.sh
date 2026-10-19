#!/usr/bin/env bash
# Refresh the AI complaint-validation dashboard end to end:
#   0. ops schema (corrections.source, classifications.prompt_version) — additive
#   1. load new classifications, enrich product identity, rebuild the taxonomy
#   2. reporting views, then regenerate POWERBI.md's coverage block from the DB
#   3. pull the retired sheet's leftovers (guarded: never over a newer app decision)
#   4. drift check (agreement + label mix), notify on new codes
#   5. rebuild the pages from the database
#   6. deploy, but ONLY if something actually changed
#
# Without this the dashboard is a snapshot of whenever it was last built by hand:
# her corrections never appear, and new reviews never show up.
#
# Every step is idempotent. Order is load-bearing (see the comments): the taxonomy
# must be rebuilt before anything reads is_new_code, corrections must be pulled
# before the pages are rendered, and the drift check runs after the views exist.
#
# FAILURE IS LOUD (2026-10-18): every step's exit code is kept, all steps still
# run, and the script exits non-zero with a printed summary. It used to end six
# steps with `|| echo`, so a broken loader "completed successfully" every 30
# minutes while the dashboard silently served week-old data. The outcome is also
# written to the `runs` table (run_id refresh-<utc>), which is what the
# "last successful sync" banner on the mirror and in the app reads.
set -uo pipefail
cd /opt/data/ai-complaint-validation || exit 1
set -a; . /opt/data/.env; set +a
export HERMES_HOME=/opt/data
export PATH="/opt/hermes/tools/uv-0.12.3-linux-x64:$PATH"
GWS=/opt/data/venvs/gws/bin/python
# pandas is in the interpreter because load_to_db hashes the live prompt out of
# cxvocab (which imports pandas) to stamp classifications.prompt_hash.
PY="uv run --with pymysql --with openpyxl --with pandas --quiet python3"

STARTED=$(date -u +%Y-%m-%dT%H:%M:%S)
STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
FAILED=()
WARNED=()
echo "[$STAMP] refresh start"

# Run one step, keep its exit code (a `cmd | tail` pipe would report tail's),
# print the tail of its output either way, and remember the failure.
step() {
  local name="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [ -n "$out" ]; then printf '%s\n' "$out" | tail -n "${STEP_TAIL:-6}"; fi
  if [ "$rc" -ne 0 ]; then
    FAILED+=("$name (exit $rc)")
    echo "!! FAILED: $name (exit $rc)"
  fi
  return 0
}

# Same, but a failure is reported without failing the run. Only for the digest:
# a missed notification changes nobody's numbers, and letting it fail the run
# would keep the freshness banner amber over a Slack problem.
warn_step() {
  local name="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if [ -n "$out" ]; then printf '%s\n' "$out" | tail -n "${STEP_TAIL:-6}"; fi
  if [ "$rc" -ne 0 ]; then
    WARNED+=("$name (exit $rc)")
    echo "!! WARNING (does not fail the refresh): $name (exit $rc)"
  fi
  return 0
}

before=$(sha256sum site/index.html site/data/reviews.json site/codes.html site/quality.html 2>/dev/null | sha256sum | cut -d' ' -f1)

# 0. Ops schema. Additive columns only (corrections.source, classifications
#    prompt_version/prompt_hash); exits 0 when already current. Runs first because
#    the loader and the sheet sync both write them, and a missing column would fail
#    both — a schema step that only ever runs once by hand is a step that gets
#    missed on the next box.
echo "-- ops schema"
step "migrate_ops_schema" $PY migrate_ops_schema.py

# 0b. Approved codes -> the classifier's vocabulary FILE. The review app records an
#     approval in complaint_codes; cxvocab reads data/vocab_additions.csv. On a box
#     where those are two hosts, nothing carried the approval across, so an
#     approved code stayed permanently unemittable. This is the copy, run before the
#     load so an approval made since the last tick can be used by this one.
echo "-- syncing approved codes into the classifier vocabulary"
step "sync_approved_vocab" $PY sync_approved_vocab.py

# 1. CSV -> database. The classifier writes its output to v3_output.csv; nothing
#    else moves it into automation_philip. Without this step the dashboard freezes
#    at whatever was loaded last, which looks like "the AI stopped working".
#    It also prints the DB-only row count and the prompt provenance of every row.
echo "-- loading new classifications into the database"
step "load_to_db" $PY load_to_db.py

# Product identity (product / parent / sku / asin / title) comes from
# amzmanager.all_products via cx_reviews.product_id. ORDER IS LOAD-BEARING:
# load_to_db copies sku/product out of v3_output.csv, which still carries the old
# (wrong) enrichment, so this must run after the load and before the views.
# It rewrites only what changed and verifies the asin agreement as it goes.
echo "-- enriching product identity from all_products"
step "enrich_products" $PY enrich_products.py

# Rebuild the taxonomy from her files + her durable approve/deny decisions.
# ORDER IS LOAD-BEARING: load_taxonomy defines is_new_code and the proposal queue,
# so it must run after the load and before anything reads them.
echo "-- re-flagging against Chadia's taxonomy"
step "load_taxonomy" $PY load_taxonomy.py

# The exposure denominator, from the READ-ONLY replica (amzmanager.finance_by_products
# -> products.sku). ORDER IS LOAD-BEARING: apply_views builds v_complaint_rates on top
# of this table and refuses to run without it, so the denominator must be loaded first.
# It writes only to automation_philip, load-then-RENAME, so an interrupted tick leaves
# the previous good table in place rather than a half-loaded one.
echo "-- building the sales denominator from the read-only replica"
step "build_sales" $PY build_sales.py

# Reporting views: final_complaint/final_category, has_prose and the SKU x month
# aggregate, plus the rate view that joins the denominator onto it. Re-created every
# run so a schema change here cannot be half-applied.
echo "-- applying reporting views"
step "apply_views" $PY apply_views.py

# data/v3_output.csv is the loader's input, and it had drifted 945 rows behind the
# database (21,283 vs 22,228: all Sep/Oct, ordinary complaints). It is now written
# back FROM v_ai_complaints -- the direction that cannot go stale -- and the write
# re-reads the file and fails the run if its count, ids or fingerprint disagree
# with the view. Order: after apply_views, because it reads the view.
echo "-- regenerating data/v3_output.csv from the database"
step "sync_v3_output" $PY sync_v3_output.py

# POWERBI.md's coverage numbers are generated from the database, not typed. The doc
# quotes 22,228 reviews to a team outside this repo; the hand-written version still
# said 19,250 three weeks after the corpus grew. Order: after apply_views, because
# the block counts cells in v_ai_complaints_monthly.
echo "-- regenerating the PowerBI coverage block"
step "gen_powerbi_coverage" $PY gen_powerbi_coverage.py

if [ -f .corrections_sheet_id ]; then
  # 1. top the queue up, THEN pull her decisions: topup appends new rows and the
  #    sync reads the decisions, so the reverse order leaves both a week behind.
  echo "-- topping up the sheet"
  step "topup_and_rollup" "$GWS" topup_and_rollup.py

  # Bulk lines ("apply this code to all N rows") before the row-by-row sync, so a
  # row covered by a bulk line is already decided when the sync looks at it.
  echo "-- applying bulk lines"
  step "bulk_apply" "$GWS" bulk_apply.py

  # Attribution: sync_corrections now asks Drive who last edited the sheet and
  # records that person, falling back to the configured default. So no
  # --reviewed-by is passed here — hard-coding 1089 is how every correction came
  # to be stamped as Chadia's even when someone else made it.
  #
  # It also refuses to write a sheet value that is older than a decision already
  # recorded from the app (corrections.source), so a decision remade on screen
  # cannot be reverted by the next tick. Held rows are printed and marked in the
  # sheet's sync_status.
  echo "-- syncing corrections"
  step "sync_corrections" "$GWS" sync_corrections.py

  # Keep the code-decisions tab aligned with the live proposals: a stale tab asks
  # her to decide codes she already defined.
  step "sync_codes_tab" $PY sync_codes_tab.py

  # The weekly random check now lives IN THE APP (/quality): it draws 45 rows,
  # persists them in weekly_samples and computes agreement from the calls made on
  # screen. weekly_sample.py still exists for the sheet workflow, but running it
  # here would write a SECOND, different sample into quality_checks for the same
  # week (same week_start key), so the refresh deliberately does not.
  echo "-- weekly random check: drawn and answered in the app (/quality) — skipped here"
else
  echo "-- no corrections sheet configured, skipping sync"
fi

# 2. Drift check (P10a): agreement >5 pts below the trailing 4-week mean FAILS the
#    run (the numbers are drifting and the banner should say so); a label's share
#    moving >3 pts week-on-week is DMed but not fatal (mix moves are often real —
#    see drift_check.py). Alerts go to Philip only, never Chadia: she owns decisions,
#    not the pipeline. Skips itself when there is nothing measurable yet.
echo "-- drift check (agreement + label mix, complete weeks)"
step "drift_check" $PY drift_check.py

# 3. Notify Chadia (primary, CX_NOTIFY_TO) with Philip copied (CX_NOTIFY_CC) when the
#    AI has proposed codes she must approve or deny, once a day and only when the set
#    changed. The digest names the app (/codes) — the sheet was withdrawn, so pointing
#    at its 'Code decisions' tab sent her to a surface that no longer takes decisions.
#    Never a channel. Non-fatal by design (see warn_step): a Slack outage must not
#    turn the freshness banner amber.
echo "-- pending-code digest"
warn_step "notify_pending_codes" $PY notify_pending_codes.py

NOTE_OK="ok"
[ ${#WARNED[@]} -gt 0 ] && NOTE_OK="ok (warnings: $(printf '%s; ' "${WARNED[@]}"))"
NOTE_FAIL="failed: $(printf '%s; ' "${FAILED[@]:-}")"
NOTE_FAIL="${NOTE_FAIL%; }"

# Written twice: before the build, so the pages that are about to be published
# carry THIS run's freshness (reading a status row that only lands afterwards is
# how a healthy site ends up showing a red banner for half an hour), and again
# after the build/deploy so a failure there is recorded too. record() is
# idempotent on the run_id.
record_outcome() {
  if [ ${#FAILED[@]} -gt 0 ]; then
    $PY refresh_status.py record --started "$STARTED" --failed \
        --errors "${#FAILED[@]}" --note "$NOTE_FAIL" \
      || echo "!! could not record the run in the runs table (banner will read as stale)"
  else
    $PY refresh_status.py record --started "$STARTED" --ok --note "$NOTE_OK" \
      || echo "!! could not record the run in the runs table (banner will read as stale)"
  fi
}

echo "-- recording run for the build"
record_outcome
# Tell the builder that this run already has failures, so the page it publishes
# says so now rather than on the next refresh.
CX_REFRESH_ERRORS="$(printf '%s; ' "${FAILED[@]:-}")"
export CX_REFRESH_ERRORS="${CX_REFRESH_ERRORS%; }"

# 3. database -> pages
echo "-- rebuilding"
step "build_dashboard" $PY build_dashboard.py
step "build_extra_pages" $PY build_extra_pages.py

after=$(sha256sum site/index.html site/data/reviews.json site/codes.html site/quality.html 2>/dev/null | sha256sum | cut -d' ' -f1)

# 4. deploy only on change — an hourly redeploy that ships identical bytes is noise,
#    and it makes "was this deploy a real update?" impossible to answer later.
#    A failed build means the site on disk is not the site we just built, so it is
#    never deployed: stale-but-real beats half-built.
BUILD_OK=1
for f in "${FAILED[@]:-}"; do
  case "$f" in build_*) BUILD_OK=0 ;; esac
done
if [ "$BUILD_OK" -eq 0 ]; then
  echo "-- build failed, deploy skipped"
elif [ "$before" = "$after" ]; then
  echo "-- no change, deploy skipped"
else
  echo "-- changed, deploying"
  step "deploy_pages" ./deploy_pages.sh
fi

# ---------------------------------------------------------------- outcome
# Record again: the build or the deploy may have failed after the first write.
record_outcome

if [ ${#FAILED[@]} -gt 0 ]; then
  echo
  echo "=== REFRESH FAILED — ${#FAILED[@]} step(s) ==="
  printf '  FAILED  %s\n' "${FAILED[@]}"
  [ ${#WARNED[@]} -gt 0 ] && printf '  warning %s\n' "${WARNED[@]}"
  echo "  pages were built from the database as it stands; a failed build was not deployed;"
  echo "  the freshness banner on the mirror and in the app is red for this run and past 24h."
  exit 1
fi

if [ ${#WARNED[@]} -gt 0 ]; then
  echo "=== refresh complete with ${#WARNED[@]} warning(s) ==="
  printf '  warning %s\n' "${WARNED[@]}"
else
  echo "=== refresh complete — every step succeeded ==="
fi
echo "[$STAMP] refresh done"
