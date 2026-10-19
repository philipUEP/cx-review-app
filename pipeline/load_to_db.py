#!/usr/bin/env python3
"""
Load the existing classifications into automation_philip.

The schema was created but empty while the real output sits in data/v3_output.csv.
This makes the database the system of record so the review app has something to
show, without touching the CSV pipeline that produced it.

Sources:
  data/v3_output.csv           21k classifications (complaint, category, sku, date, text)
  data/review_flags.csv        second_complaint + needs_review (routing)

Provenance (P10b), stamped on every write:
  prompt_version  the run's version string
  prompt_hash     sha256(SYSTEM + menu) from cxvocab, first 12 hex — so a version
                  string reused with an edited menu still shows as a difference
  Rows already in the database but absent from the CSV used to be invisible: 945 of
  them (4.2% of the corpus) had no traceable origin. The loader now prints that
  count on every run so the gap cannot grow unnoticed, and backfills version+hash
  only from the row's own run (same run -> same prompt); it never invents a
  provenance for a row that came from a different prompt.

`is_new_code` is NOT written here. The taxonomy owner (load_taxonomy.py) is the
only thing that decides whether a code is new, because it is the only thing that
reads Chadia's files and her approve/deny decisions. This script used to derive
it from data/new_complaint_categories.csv — a file frozen on 2026-10-08 that no
refresh step regenerated — so "new" had two definitions and the stale one could
win. The column is left at its default 0 on insert and untouched on update.

Idempotent: the run row is replaced, classifications upsert on review_id.

WARNING — the CSV's sku/product/parent columns are NOT authoritative. They carry
an older enrichment that joined business_products instead of all_products (see
enrich_products.py), so this load puts wrong SKUs back on ~1,500 rows every time
it runs. That is why cx_refresh.sh runs enrich_products.py immediately after this
step and before the views are created. Do not reorder those two.
"""
import csv, hashlib, os, sys
from datetime import date
import pymysql

RUN_ID = "backlog-2026-10-17"
PROMPT_VERSION = "v3-nocatchall"
MODEL = "deepseek-chat"
CSV_PATH = "data/v3_output.csv"


def prompt_hash():
    """Fingerprint of the live prompt (cxvocab.SYSTEM + menu), or None.

    Read-only import: cxvocab is the prompt's owner and is not modified here.
    None (with a printed warning) beats a made-up hash if the import fails.
    """
    try:
        import cxvocab
        raw = (cxvocab.SYSTEM or "") + "\n" + (cxvocab.MENU_LINES or "")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    except Exception as e:                                     # noqa: BLE001
        print(f"WARN: prompt hash unavailable ({type(e).__name__}: {e}) — "
              f"prompt_version still stamped, hash left NULL", file=sys.stderr)
        return None


def none_if_blank(v):
    v = (v or "").strip()
    return v or None


def load_flags():
    flags = {}
    p = "data/review_flags.csv"
    if not os.path.exists(p):
        return flags
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        flags[str(r["review_id"])] = {
            "second": none_if_blank(r.get("second")),
            "needs_review": int(r.get("needs_review") or 0),
        }
    return flags


def provenance_report(cur, csv_ids):
    """Provenance health: what the 945-row gap is, and who is on which prompt."""
    cur.execute("SELECT review_id FROM classifications")
    db_ids = {r[0] for r in cur.fetchall()}
    db_only = db_ids - csv_ids
    csv_only = csv_ids - db_ids
    print(f"  db-only rows    : {len(db_only)} of {len(db_ids)} "
          f"({100.0 * len(db_only) / max(len(db_ids), 1):.1f}%) are in the database but "
          f"not in {CSV_PATH}")
    print(f"  csv-only rows   : {len(csv_only)} (present in the CSV, absent from the DB)")
    if db_only:
        sample = ", ".join(str(i) for i in sorted(db_only)[:5])
        print(f"                    e.g. review_id {sample}")
    cur.execute("""SELECT COALESCE(prompt_version,'(none)') v, COALESCE(prompt_hash,'—') h,
                          COUNT(*) n
                     FROM classifications GROUP BY v, h ORDER BY n DESC""")
    rows = cur.fetchall()
    print("  prompt provenance:")
    for v, h, n in rows:
        flag = "" if v == PROMPT_VERSION else "  <- differs from current prompt"
        print(f"    {v:<18} hash {h:<12} {n:>6} row(s){flag}")
    cur.execute("""SELECT COUNT(*) FROM classifications
                    WHERE prompt_version IS NULL OR prompt_version<>%s""", (PROMPT_VERSION,))
    n_other = cur.fetchone()[0]
    print(f"  rows not on the current prompt ({PROMPT_VERSION}): {n_other}")
    return len(db_only), n_other


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true",
                    help="print the provenance report only (no load, no writes)")
    args = ap.parse_args()

    conn = pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4")
    cur = conn.cursor()

    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8-sig")))
    print(f"source rows: {len(rows)}")

    if args.report:
        csv_ids = {int(r["review_id"]) for r in rows if (r.get("review_id") or "").strip()}
        print(f"current prompt: {PROMPT_VERSION} hash {prompt_hash() or '(unavailable)'}")
        print("\n=== provenance ===")
        db_only, n_other = provenance_report(cur, csv_ids)
        print(f"PROVENANCE: {db_only} db-only row(s) (in the DB, absent from {CSV_PATH}); "
              f"{n_other} row(s) not on the current prompt ({PROMPT_VERSION})")
        conn.close()
        return

    phash = prompt_hash()
    print(f"prompt: {PROMPT_VERSION} hash {phash or '(unavailable)'}")

    flags = load_flags()
    print(f"flags: {len(flags)}")

    # Categories the taxonomy knows but the CSV left blank (the 10 codes with no
    # category in her file). Read from complaint_codes, which load_taxonomy fills.
    cur.execute("SELECT code, category FROM complaint_codes WHERE category IS NOT NULL")
    # Code strings differ in casing between her CSV ('Difficult to Use') and the
    # classifier's title-cased output ('Difficult To Use'); the MySQL join is
    # case-insensitive, this dict is not, so normalise the key.
    cat_of = {" ".join(c.split()).lower(): cat for c, cat in cur.fetchall()}

    cur.execute("""INSERT INTO runs (run_id, model, prompt_version, max_tokens, menu_size, n_rows, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE n_rows=VALUES(n_rows), finished_at=NOW()""",
                (RUN_ID, MODEL, PROMPT_VERSION, 800, 132, len(rows),
                 "backlog load from v3_output.csv; catch-alls withheld from menu"))
    conn.commit()

    sql = """INSERT INTO classifications
             (review_id, run_id, prompt_version, prompt_hash, complaint, category,
              second_complaint, needs_review,
              review_date, sku, asin, parent, product, review_text,
              order_id, country, source)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON DUPLICATE KEY UPDATE
               run_id=VALUES(run_id),
               prompt_version=VALUES(prompt_version), prompt_hash=VALUES(prompt_hash),
               complaint=VALUES(complaint), category=VALUES(category),
               second_complaint=VALUES(second_complaint), needs_review=VALUES(needs_review),
               review_date=VALUES(review_date),
               sku=VALUES(sku), asin=VALUES(asin), parent=VALUES(parent),
               product=VALUES(product), review_text=VALUES(review_text),
               order_id=VALUES(order_id), country=VALUES(country), source=VALUES(source)"""

    batch, n_ok, n_bad, csv_ids = [], 0, 0, set()
    for i, r in enumerate(rows, 1):
        rid = (r.get("review_id") or "").strip()
        if not rid:
            n_bad += 1
            continue
        d = (r.get("date") or "").strip()
        d = d if len(d) == 10 and d[4] == "-" else None
        comp = none_if_blank(r.get("complaint"))
        cat = none_if_blank(r.get("category")) or (
            cat_of.get(" ".join(comp.split()).lower()) if comp else None)
        f = flags.get(rid, {})
        csv_ids.add(int(rid))
        batch.append((
            int(rid), RUN_ID, PROMPT_VERSION, phash, comp, cat,
            f.get("second"), f.get("needs_review", 0),
            d, none_if_blank(r.get("sku")), none_if_blank(r.get("asin")),
            none_if_blank(r.get("parent")), none_if_blank(r.get("product")),
            (r.get("text") or "")[:20000] or None,
            none_if_blank(r.get("order_id")), none_if_blank(r.get("country")),
            none_if_blank(r.get("source")),
        ))
        if len(batch) >= 1000:
            cur.executemany(sql, batch)
            conn.commit()
            n_ok += len(batch)
            batch = []
            print(f"  {i}/{len(rows)}", flush=True)
    if batch:
        cur.executemany(sql, batch)
        conn.commit()
        n_ok += len(batch)

    # ---- provenance backfill, same run only --------------------------------
    # Rows the CSV no longer carries (the 945-row gap) still belong to this run,
    # so their prompt is this run's prompt: that is a fact from the run row, not a
    # guess. Rows from a different run are left alone and reported as differing.
    if phash:
        cur.execute("""UPDATE classifications c JOIN runs r ON r.run_id=c.run_id
                          SET c.prompt_version=COALESCE(c.prompt_version, r.prompt_version),
                              c.prompt_hash=COALESCE(c.prompt_hash, %s)
                        WHERE c.run_id=%s AND r.prompt_version=%s""",
                    (phash, RUN_ID, PROMPT_VERSION))
        if cur.rowcount:
            print(f"  provenance stamped on {cur.rowcount} pre-existing row(s) of run {RUN_ID}")
        conn.commit()

    # ---- verify what actually landed ----
    print("\n=== loaded ===")
    cur.execute("SELECT COUNT(*) FROM classifications"); print("  classifications :", cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM classifications WHERE needs_review=1"); print("  needs review    :", cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM classifications WHERE complaint IS NULL"); print("  blank complaint :", cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM classifications WHERE review_date IS NULL"); print("  blank date      :", cur.fetchone()[0])
    cur.execute("SELECT MIN(review_date), MAX(review_date) FROM classifications"); print("  date range      :", cur.fetchone())
    cur.execute("SELECT complaint, COUNT(*) FROM classifications GROUP BY complaint ORDER BY 2 DESC LIMIT 5")
    print("  top labels      :", cur.fetchall())
    if n_bad:
        print(f"  skipped bad ids : {n_bad}")

    # ---- NULL category is a data error, not a taxonomy gap ----------------
    # Every code with a category must classify into one, and every code without
    # one must be an approved code that still needs a category chosen. Anything
    # else means rows vanish from every rollup while looking like a bad report.
    cur.execute("""SELECT c.complaint, COUNT(*) n, MAX(cc.category) cat, MAX(cc.source) src
                     FROM classifications c
                     LEFT JOIN complaint_codes cc ON cc.code = c.complaint
                    WHERE c.category IS NULL OR c.category=''
                    GROUP BY c.complaint ORDER BY n DESC""")
    blanks = cur.fetchall()
    if blanks:
        errors = [b for b in blanks
                  if b[2] is not None or b[3] is None
                  or not (b[3] == "chadia-approved" and b[2] is None)]
        awaiting = [b for b in blanks if b not in errors]
        print(f"  NULL category   : {sum(b[1] for b in blanks)} rows "
              f"({sum(b[1] for b in errors)} fixable, "
              f"{sum(b[1] for b in awaiting)} approved code awaiting a category)")
        for code, n, _, _ in blanks[:12]:
            print(f"     {n:5d}  {code}")
        if errors:
            print("FATAL: classified rows have no category and the taxonomy has one to give — "
                  "fix before anything reads a category rollup", file=sys.stderr)
            conn.close()
            sys.exit(1)

    # ---- provenance (P10b/c) ----------------------------------------------
    print("\n=== provenance ===")
    db_only, n_other = provenance_report(cur, csv_ids)
    # Last line on purpose: cx_refresh.sh prints only the tail of each step, so a
    # summary buried above would scroll out of the log and the 4.2% gap would grow
    # exactly as unnoticed as before.
    print(f"PROVENANCE: {db_only} db-only row(s) (in the DB, absent from {CSV_PATH}); "
          f"{n_other} row(s) not on the current prompt ({PROMPT_VERSION})")

    conn.close()


if __name__ == "__main__":
    main()
