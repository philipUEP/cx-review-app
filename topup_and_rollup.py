#!/usr/bin/env python3
"""
Top up the corrections sheet so it covers the WHOLE review queue.

Why:
  - The sheet was seeded with 500 flagged rows while 2,100+ need review. A queue
    that silently stops at 500 is worse than no queue: work looks finished.
  - Order matters. It used to be date order, so the highest-volume corrections
    (e.g. 371 rows of one code) were scattered down 2,099 rows and had to be typed
    one at a time. Rows are now appended by volume (SKU x complaint count, desc),
    and a code that recurs can be cleared in one line on the 'Bulk apply' tab.
  - Already-decided reviews are not re-queued, so work actually shrinks.

The weekly agreement figure is NOT computed here any more: it was the accuracy of
the rows the model was least sure about, which measures the queue, not the system.
weekly_sample.py draws a real random sample and owns quality_checks.

Run: cx_refresh.sh calls this before rebuilding. Idempotent.
  --dry-run   report what would change
"""
import argparse, json, os, subprocess, sys

import pymysql

import sheet_util as SU

SHEET_ID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".corrections_sheet_id")
HERMES = "/opt/data"
GAPI = os.path.join(HERMES, "skills/productivity/google-workspace/scripts/google_api.py")
GWS = "/opt/data/venvs/gws/bin/python"
TAB = "Corrections"


def g(args):
    """Call the wrapper CLI rather than importing it — the CLI's flags are the stable
    contract; the module's internal function names are not."""
    env = dict(os.environ, HERMES_HOME=HERMES)
    p = subprocess.run([GWS, GAPI] + args, capture_output=True, text=True, env=env, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"google_api {args[0]} failed: {(p.stderr or p.stdout)[:300]}")
    return p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    sid = open(SHEET_ID_FILE).read().strip()
    conn = pymysql.connect(host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
                           password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
                           charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    cur = conn.cursor()

    # ---- 1. undecided flagged rows, highest-volume (sku x code) first ----
    cur.execute("""
        SELECT c.review_id, c.review_date, c.sku, c.product,
               LEFT(COALESCE(c.review_text,''),300) t, c.complaint, c.category,
               COALESCE(v.n, 0) vol
          FROM classifications c
          LEFT JOIN (SELECT sku, complaint, COUNT(*) n FROM classifications
                      WHERE sku IS NOT NULL AND complaint IS NOT NULL
                      GROUP BY sku, complaint) v
                 ON v.sku = c.sku AND v.complaint = c.complaint
         WHERE c.needs_review = 1
           AND NOT EXISTS (SELECT 1 FROM corrections k WHERE k.review_id = c.review_id)
         ORDER BY vol DESC, c.review_date DESC""")
    flagged = cur.fetchall()
    print(f"undecided flagged rows: {len(flagged)}")

    if a.dry_run:
        print("top of the queue (by SKU x code volume):")
        for r in flagged[:8]:
            print(f"   {r['vol']:4d}  {r['sku'] or '-':<18} {r['complaint']}")
        print("DRY RUN — nothing written")
        return

    # ---- 2. keep the dropdowns pointing at the live taxonomy ----
    try:
        svc = SU.service()
        SU.refresh_codes_tab(svc, sid, cur)
        corr_sheet_id = SU.tabs(svc, sid)[TAB]
        SU.validation(svc, sid, corr_sheet_id, 7, 1, 6000, "Codes!A2:A400",
                      "Pick a code from the list (or NOT A COMPLAINT)")
        print("dropdowns refreshed (Codes tab + your_correction)")
    except Exception as e:                                    # noqa: BLE001
        print(f"WARN: could not refresh dropdowns: {e}")

    # ---- 3. append what the sheet does not have yet ----
    try:
        have = {str(row[0]).strip() for row in json.loads(g(["sheets", "get", sid, f"{TAB}!A2:A6000"])) if row}
    except Exception as e:
        print(f"  could not read the sheet ({e}); skipping top-up")
        return
    missing = [r for r in flagged if str(r["review_id"]) not in have]
    print(f"missing from the sheet: {len(missing)}")
    if missing:
        rows = [[str(r["review_id"]), r["review_date"].isoformat() if r["review_date"] else "",
                 r["sku"] or "", r["product"] or "", r["t"] or "", r["complaint"] or "",
                 r["category"] or "", "", "", ""] for r in missing]
        # Chunk the append: the whole queue in one argument blows the OS argv limit
        # ("Argument list too long"), and per-row calls hit the Sheets quota.
        CHUNK = 200
        done = 0
        for i in range(0, len(rows), CHUNK):
            g(["sheets", "append", sid, f"{TAB}!A:J", "--values", json.dumps(rows[i:i + CHUNK])])
            done += len(rows[i:i + CHUNK])
            print(f"  appended {done}/{len(rows)}")


if __name__ == "__main__":
    main()
