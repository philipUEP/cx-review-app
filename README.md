# cx-review-app

The review surface for the AI complaint-validation project: a small WSGI app that
lets Chadia confirm, correct or reject the classifier's complaint code **on the
page**, decide proposed complaint codes, and record the weekly 45-row random
check. It is the only surface that writes to the `automation_philip` database.

The Cloudflare Pages site (`uep-ai-complaints.pages.dev`) stays as a read-only
mirror for anyone who only wants to look.

## Repository layout

```
app.py                 the review app — the only surface that writes decisions
deploy_on_box.sh       installs it on ops-automations, nginx on :80 behind the proxy
cx_refresh.sh          the 30-minute chain: classify -> load -> views -> build -> deploy
cxvocab.py             the complaint vocabulary and the model prompt
apply_views.py         all views + assertions on grain and denominators
build_sales.py         joins the read-only replica's finance data for the rate
taxonomy/              Chadia's audit files, vendored so a clone works
tests/                 check_mix_consistency.py, test_sales_rates.py
POWERBI.md             the PowerBI path for the QC Data Tracker
.env.example           every setting, no secrets
```

## Status, honestly

The classifier agrees with Chadia's blind 220-row audit **83.6%** strictly and
**85.5%** on a same-taxonomy-slot basis (margin ±5 points at n=220). 22,228
complaint records are classified, and 99.94% of them carry an orders denominator
from the seller's finance feed.

**No label has been checked by a human yet.** The weekly 45-row sample is the
mechanism for that, and until two of them exist the honest description of this
data is "AI-assigned", not "validated".

## Deploy (two commands, on ops-automations 192.168.1.203)

```bash
sudo git clone https://github.com/philipUEP/cx-review-app.git /opt/ai-complaints
sudo bash /opt/ai-complaints/deploy_on_box.sh
```

The first run asks once for the `automation_philip` MySQL connection (host, user,
password — the account Ab provisioned), generates Chadia's login password, shows
it once, and writes it to `/opt/ai-complaints/app.env` (mode 600). After that:

```bash
sudo git -C /opt/ai-complaints pull && sudo bash /opt/ai-complaints/deploy_on_box.sh
```

That is the whole update path. The script is idempotent: it leaves `app.env`
alone, re-installs the systemd unit and the nginx site, restarts, and fails loudly
if `/healthz` does not come back `ok`.

Lost the login password? `sudo rm /opt/ai-complaints/app.env` and re-run — it
rotates.

## What it needs

* Python 3.9+ with `python3-venv` (`apt-get install -y python3-venv`)
* nginx (installed by the script if missing)
* Network access to `db01-master.amzmanager.net:3306`
* No build step, no Node, no CDN: one HTML response per page, styled inline.

## Topology

```
browser --https--> philip.amzmanager.net (reverse proxy, terminates TLS)
                        |
                        v  plain HTTP :80
                  nginx on ops-automations (default_server)
                        |
                        v  127.0.0.1:8080
                  app.py (stdlib wsgiref, HTTP Basic auth)
                        |
                        v
                  automation_philip @ db01-master  (writes only here)
```

The app never connects to the `amzmanager` source database. Product identity
(`product`, `parent`, `sku`, `asin`, `title`) is enriched into `classifications`
by `enrich_products.py` on the refresh host, straight from
`amzmanager.all_products` via `cx_reviews.product_id`.

## Pages

| Path | What it does |
| --- | --- |
| `/` | volumes, resolved-label distribution, coverage |
| `/reviews` | filter by text/title, product, parent, SKU, complaint, source, country, status, dates; the return-reason tokens render as chips and rows with no customer comment are marked *code only* and filterable; product + title, SKU and ASIN link to Amazon |
| `/review/<id>` | the whole review, AI vs resolved label, confirm / correct / reject, decision history — and you can change a decision you already made (the new row supersedes it, the old one stays in the history) |
| `/monthly` | SKU × month × complaint with share of SKU-month and how many rows a human validated |
| `/codes` | approve a proposed code into the vocabulary (with a category) or deny and remap its rows |
| `/quality` | this week's 45-row sample: record a call per row, agreement is computed, not typed |
| `/export.csv` | the current filter as CSV (`?text=1` adds the review text) |
| `/healthz` | unauthenticated liveness probe (also checks the DB) |

## Decisions this app makes durable

* **A correction never destroys evidence.** Each decision is an INSERT into
  `corrections` (AI label, new label, decision, reviewer id and name, note,
  timestamp); the resolved label is computed in `v_ai_complaints`, so the AI's
  original answer stays visible.
* **A later decision supersedes an earlier one.** `v_ai_complaints` reads the
  latest row per review, so changing your mind is another INSERT, never an
  update. The sheet path behaves the same way: `sync_status='done'` no longer
  means "skip", it means "compare"; a different value is written as a
  superseding row and the cell is marked `revised <date>`.
* **Attribution is real.** Every write records the signed-in username (and its
  uid where one is configured). The sheet path records the spreadsheet's
  `lastModifyingUser` from Drive, mapped through `data/reviewers.json`, and says
  so in its output; an unmapped editor is stored by name with a NULL uid rather
  than being stamped with someone else's.
* **Approve is real.** The code is written to `complaint_codes`
  (`source='chadia-approved'`, with a category), its rows stop being
  `is_new_code`, and the classifier may emit it from then on.
* **Deny needs a replacement.** The code is recorded `approved=0`
  (`source='chadia-denied'`), its existing rows are relabelled to the code she
  meant, and the mapping is written to `code_denials` — which
  `sync_corrections.write_denials_json` folds into `data/denied_codes.json` on the
  refresh host so future classifications are mapped too. A deny without a
  replacement is refused.
* **The weekly check is a sample, not a feeling.** 45 rows, stratified by
  category, seeded on the ISO week and persisted in `weekly_samples`, so a
  refresh cannot swap rows out from under the checker. Under 5 calls is progress,
  not a measurement: nothing is recorded until 5 rows are called.

## Configuration

`/opt/ai-complaints/app.env` (systemd `EnvironmentFile`, mode 600):

```
CXAPP_DB_HOST=db01-master.amzmanager.net
CXAPP_DB_USER=philip
CXAPP_DB_PASSWORD=…
CXAPP_DB_NAME=automation_philip
APP_USER=chadia
APP_PASS=…                  # generated by the deploy script
APP_REVIEWER_UID=1089       # Chadia's uid in the legacy users table
APP_HOST=127.0.0.1
APP_PORT=8080
```

`APP_SAMPLE_N` (default 45) changes the weekly sample size.

**More than one reviewer.** Add `APP_USERS` and it replaces `APP_USER`/`APP_PASS`
entirely — one entry per person, `name:uid:password`, comma-separated:

```
APP_USERS=chadia:1089:…,philip:1001:…
```

Each decision then carries the username and uid of whoever was signed in, and the
header shows who that is. A person with no legacy uid can still be given a login;
their decisions are recorded by name with a NULL uid, which is true, instead of
borrowing 1089.

**Freshness.** Every page carries the last successful refresh from the `runs`
table and turns red past 24 hours. The refresh job (`cx_refresh.sh`, every 30
minutes) writes one `refresh-<utc>` row per run and exits non-zero with a printed
summary if any step failed; it no longer reports success while its work fails.

## Run it locally

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
export CXAPP_DB_HOST=… CXAPP_DB_USER=… CXAPP_DB_PASSWORD=… CXAPP_DB_NAME=automation_philip
APP_USER=chadia APP_PASS=dev APP_PORT=8095 python app.py
curl -u chadia:dev http://127.0.0.1:8095/healthz   # ok db=ok
```

## Quirks worth knowing

* `wsgiref.simple_server` is single-threaded. That is deliberate: a handful of
  internal users, and a single writer per request means no connection pool to
  leak. If it ever needs concurrency, put `waitress` in the venv and change one
  line in `deploy_on_box.sh`.
* The database account `philip` can only see `automation_philip` (verified with
  `SHOW DATABASES`), so the app physically cannot touch the legacy schema.
* Basic auth is safe here only because the reverse proxy terminates TLS. Do not
  expose port 80 of this box to anything but the proxy.
