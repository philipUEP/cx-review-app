#!/usr/bin/env python3
"""Sync approved complaint codes from the database into the classifier's menu.

Why this exists: approving a code in the review app records the decision in
complaint_codes (approved=1, source='chadia-approved') and, if it can, writes
data/vocab_additions.csv beside the app. The classifier is a different process —
in production a different host — and it reads data/vocab_additions.csv, not the
database. Without this step "approved" is true in the database and invisible to
the classifier, so an approved code can never be emitted.

Idempotent: rewrites the file from the approved set plus whatever rows were
already there (a hand-added code is kept, never dropped). Exits non-zero only if
the file cannot be written.
"""
import csv
import os
import sys
from datetime import date

import pymysql

HEADER = ["Complaint Types", "Category"]
TARGET = os.environ.get("CX_VOCAB_ADDITIONS") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "vocab_additions.csv")


def norm(s):
    return (s or "").strip().lower()


def main():
    conn = pymysql.connect(host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
                           password=os.environ["CXAPP_DB_PASSWORD"],
                           database=os.environ["CXAPP_DB_NAME"], charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()
    cur.execute("""SELECT code, category FROM complaint_codes
                    WHERE approved=1 AND source='chadia-approved'
                    ORDER BY category, code""")
    approved = [(r["code"], r["category"]) for r in cur.fetchall()]
    conn.close()

    rows = []
    try:
        with open(TARGET, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.reader(fh) if r and any(c.strip() for c in r)]
    except OSError:
        rows = []

    if rows and norm(rows[0][0]) == norm(HEADER[0]):
        body = rows[1:]
    else:
        body = rows

    have = {norm(r[0]) for r in body}
    added = [c for c, _ in approved if norm(c) not in have]

    # An approved code's category is authoritative: refresh a row whose category
    # changed rather than leaving the file disagreeing with the decision.
    by_code = {norm(c): cat for c, cat in approved}
    out = []
    for r in body:
        k = norm(r[0])
        out.append([r[0], by_code.get(k) or (r[1] if len(r) > 1 else "")])
    out += [[c, cat] for c, cat in approved if norm(c) not in have]

    tmp = f"{TARGET}.{os.getpid()}.tmp"
    os.makedirs(os.path.dirname(os.path.abspath(TARGET)), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(out)
    os.replace(tmp, TARGET)

    print(f"vocab: {len(out):,} codes in {TARGET} "
          f"({len(approved):,} approved in complaint_codes, {len(added)} added, {date.today()})")
    if added:
        print("  added: " + ", ".join(added))
    return 0


if __name__ == "__main__":
    sys.exit(main())
