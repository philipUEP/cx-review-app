#!/usr/bin/env python3
"""Regenerate data/v3_output.csv from the database, and fail when it is stale.

The classifier writes data/v3_output.csv; load_to_db.py loads it into
`classifications`. After a few loads that ran only on the DB side (or after the
corpus grew through a second path), the file on disk drifted: it carried 21,283
rows while `v_ai_complaints` held 22,228. A stale file is not a cosmetic problem
-- the next refresh loads it, and anything that reads the CSV (chadia_eval,
build_spotcheck_v2, resolve_writeback) silently measures a smaller corpus.

This script is the other direction: it writes the CSV *from* the view, so the two
cannot disagree, and it asserts the result instead of trusting it:

  * the DB must have one row per review_id (no duplicated ids);
  * the number of rows written must equal the number of rows in the DB;
  * re-reading the file must give the same count, the same distinct ids and the
    same (review_id, complaint, category) fingerprint.

Any of those failing exits non-zero, so cx_refresh.sh fails the run rather than
publishing a corpus that is quietly 945 rows short.

Usage:
    python3 sync_v3_output.py            # rewrite the CSV, verifying as it goes
    python3 sync_v3_output.py --check    # read-only: fail if the CSV is stale
"""
import argparse
import csv
import hashlib
import os
import sys

import pymysql

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "data", "v3_output.csv")

# The header load_to_db.py reads. Order and spelling matter: `date` and `text`
# are the names it looks up, not review_date / review_text.
#
# dup_rank / dup_group are NOT read by load_to_db — they ride along so the CSV
# discloses the repeats the same way the page and the export do (F6: the file had
# order_id but no dup_rank, so a reader of the CSV could not see a repeat at all).
COLS = ["review_id", "product", "parent", "sku", "asin", "order_id", "country",
        "date", "source", "text", "complaint", "category", "dup_rank", "dup_group"]

SQL = """SELECT review_id, product, parent, sku, asin, order_id, country,
                DATE_FORMAT(review_date, '%Y-%m-%d') AS date, source,
                review_text AS text, complaint, category, dup_rank, dup_group
           FROM v_ai_complaints
          ORDER BY review_id"""


def connect():
    return pymysql.connect(host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
                           password=os.environ["CXAPP_DB_PASSWORD"],
                           database=os.environ["CXAPP_DB_NAME"], charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)


def fingerprint(rows):
    """Hash of (review_id, complaint, category) for every row.

    A count alone cannot tell "the same 22,228 rows" from "22,228 different rows".
    Normalised through strip/None so the same value read from MySQL and from the
    CSV hashes the same.
    """
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: str(r["review_id"])):
        parts = [" ".join(str(r.get(k) or "").split()) for k in ("review_id", "complaint", "category")]
        h.update("\x1f".join(parts).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:16]


def db_rows(cur):
    cur.execute(SQL)
    return cur.fetchall()


def read_csv(path):
    """(row count, distinct-id count, fingerprint) for a CSV on disk."""
    if not os.path.exists(path):
        return 0, 0, ""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    ids = {(r.get("review_id") or "").strip() for r in rows if (r.get("review_id") or "").strip()}
    return len(rows), len(ids), fingerprint(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="read-only: exit 1 if the CSV disagrees with the database")
    ap.add_argument("--path", default=os.environ.get("CX_OUT") or DEFAULT_PATH)
    args = ap.parse_args()
    path = args.path

    conn = connect()
    cur = conn.cursor()
    rows = db_rows(cur)
    conn.close()
    n_db = len(rows)
    ids = {(r["review_id"] or "") for r in rows}
    if not n_db:
        raise SystemExit("v_ai_complaints is empty — refusing to write an empty v3_output.csv")
    if len(ids) != n_db:
        raise SystemExit(f"v_ai_complaints has {n_db} rows for {len(ids)} review ids: "
                         f"a duplicated review would be loaded twice")
    fresh = fingerprint(rows)

    n_old, _, fp_old = read_csv(path)
    if args.check:
        n_file, n_ids, fp_file = read_csv(path)
        if n_file != n_db or fp_file != fresh:
            print(f"STALE: {path} holds {n_file:,} row(s) (fingerprint {fp_file or '—'}), "
                  f"v_ai_complaints holds {n_db:,} ({fresh}). Run sync_v3_output.py.",
                  file=sys.stderr)
            return 1
        print(f"v3_output.csv is current: {n_file:,} rows, fingerprint {fp_file}")
        return 0

    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore", lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in COLS})
        # Verify the FILE, not the intention: re-read what is about to replace the
        # old one, and refuse to swap it in if it does not match the database.
        n_new, n_ids_new, fp_new = read_csv(tmp)
        if (n_new, fp_new) != (n_db, fresh):
            raise SystemExit(f"the file just written disagrees with the database: {n_new:,} rows / "
                             f"{fp_new} vs {n_db:,} / {fresh} — leaving {path} untouched")
        if n_ids_new != n_db:
            raise SystemExit(f"the file just written has {n_new:,} rows for {n_ids_new:,} ids")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    print(f"v3_output.csv: {n_db:,} rows written from v_ai_complaints "
          f"(was {n_old:,}, {n_db - n_old:+,}); fingerprint {fresh}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
