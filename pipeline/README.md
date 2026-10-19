# The pipeline

Everything that turns raw reviews into the numbers the app displays. Runs on the
refresh host (not on `ops-automations`), every 30 minutes via `cx_refresh.sh`.

## What it does, in order

```
cx_refresh.sh
  1. migrate_ops_schema.py   add columns introduced since the last run
  2. classify_v2.py          classify new reviews (bounded batch)
  3. load_to_db.py           CSV -> automation_philip.classifications
  4. load_taxonomy.py        rebuild complaint_codes from Chadia's audit files
  5. enrich_products.py      sku / asin / title / parent from amzmanager.all_products
  6. sync_v3_output.py       regenerate the CSV from the view (fails if stale)
  7. build_sales.py          amzmanager.finance_by_products -> sales_by_sku_month
  8. apply_views.py          rebuild the views, assert grain and denominators
  9. topup_and_rollup.py     top up the correction sheet, roll up weekly agreement
 10. sync_corrections.py     sheet decisions -> corrections
 11. weekly_sample.py        draw the 45-row stratified weekly check
 12. drift_check.py          alert if agreement or label mix moves
 13. gen_powerbi_coverage.py rewrite POWERBI.md's coverage block from the DB
 14. build_dashboard.py
 15. build_extra_pages.py    -> site/
 16. deploy_pages.sh         Cloudflare Pages, only if the output changed
```

Any step failing makes the whole run exit non-zero, and the app shows a red
banner. A run that silently half-worked is the failure mode this is designed
against.

## Setup

```bash
python3 -m venv venv && ./venv/bin/pip install -r pipeline/requirements.txt
cp .env.example .env      # fill in the database credentials and the API key
```

The taxonomy CSVs ship in `pipeline/taxonomy/`, so a clone runs without the
shared attachments directory. Override with `CX_TAXONOMY_DIR` if you keep them
elsewhere.

## Run once, by hand

```bash
set -a && . ./.env && set +a
bash pipeline/cx_refresh.sh
```

## Schedule it

```cron
*/30 * * * * cd /path/to/cx-review-app && bash pipeline/cx_refresh.sh >> /var/log/cx-refresh.log 2>&1
```

## The scripts you are most likely to touch

| Script | What it does |
|---|---|
| `cxvocab.py` | the complaint vocabulary and the system prompt. **Changing `SYSTEM` or `MENU_LINES` changes the prompt hash and invalidates the accuracy measurement** — re-run the 220-row benchmark before shipping |
| `classify_v2.py` | one review -> one complaint code. `CX_OUT` picks the output CSV, `--max` bounds the batch, and it resumes by skipping ids already present |
| `load_taxonomy.py` | builds `complaint_codes` from Chadia's audit files + `vocab_additions.csv`. Upserts; never deletes her decisions |
| `build_sales.py` | the exposure denominator. Reads the **read-only replica** only |
| `apply_views.py` | all views, plus assertions that grain is unsplit and no ratio divides by NULL or zero |
| `drift_check.py` | DMs Philip when agreement drops >5 points or a label's share moves >3 points |

## Two rules that are not negotiable

1. **`amzmanager` and the replica are read-only.** The only writable database is
   `automation_philip`. The analytics account holds `SELECT, SHOW VIEW` and that
   is deliberate — Ab: *"Making changes on the replica server will degrade
   replication."*
2. **A month-over-month comparison is only valid within a single feed.** The
   `ncx` and `amazon` review feeds stop partway through the corpus, so an
   all-source comparison moves with harvesting, not with the product. The
   quotable series is `fba`. `v_complaint_rates_by_source` exists for this
   reason; a July "spike" was published once and retracted because of it.
