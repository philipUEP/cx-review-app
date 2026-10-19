#!/usr/bin/env python3
"""
Bulk correction: "apply this code to all N rows with this AI code".

Without it the highest-volume corrections have to be typed one row at a time
(371 rows for a single code), which is why a 2,100-row queue gets abandoned. Each
line here is a scoped rule, applied to every matching review that no human has
decided yet:

  ai_complaint | new_code | scope        | apply
  Arrived Late | NOT A COMPLAINT | queue  | yes

  * new_code must be in the taxonomy (or the literal NOT A COMPLAINT), so a typo
    cannot invent a category
  * scope 'queue' = only rows flagged needs_review; 'all' = every row with that
    AI code
  * idempotent: already-validated reviews are skipped, so re-running is safe
  * the outcome is written back into the 'done' column, so the sheet shows what
    each line did

Run: cx_refresh.sh, or `python bulk_apply.py --dry-run`.
"""
import argparse
import datetime as dt
import os
import re
import sys

import pymysql

import sheet_util as SU

HERE = os.path.dirname(os.path.abspath(__file__))
SHEET_ID_FILE = os.path.join(HERE, ".corrections_sheet_id")
TAB = "Bulk apply"
HEADER = ["ai_complaint", "new_code", "scope", "apply", "done"]
BLANK_ROWS = 12
NOT_A_COMPLAINT = "NOT A COMPLAINT"


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def db():
    return pymysql.connect(host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
                           password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
                           charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def ensure_tab(svc, sid):
    sid_tab = SU.ensure_tab(svc, sid, TAB, rows=BLANK_ROWS + 1, cols=len(HEADER))
    cur_vals = SU.get_values(svc, sid, f"'{TAB}'!A1:E1")
    if not cur_vals:
        SU.values(svc, sid, f"'{TAB}'!A1",
                  [HEADER] + [["", "", "queue", "", ""] for _ in range(BLANK_ROWS)])
        SU.validation(svc, sid, sid_tab, 3, 1, BLANK_ROWS + 1, "Codes!A2:A400",
                      "Pick the code to apply (or NOT A COMPLAINT)")
    return sid_tab


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reviewed-by", type=int, default=1089)
    a = ap.parse_args()

    if not os.path.exists(SHEET_ID_FILE):
        print("no corrections sheet configured")
        return
    sid = open(SHEET_ID_FILE).read().strip()
    svc = SU.service()
    ensure_tab(svc, sid)

    cur = db().cursor()
    cur.execute("SELECT code, category FROM complaint_codes WHERE approved=1")
    tax = {norm(c): (c, cat) for c, cat in ((r["code"], r["category"]) for r in cur.fetchall())}
    SU.refresh_codes_tab(svc, sid, cur)

    vals = SU.get_values(svc, sid, f"'{TAB}'!A1:E{BLANK_ROWS + 1}")
    done_rows = {}
    n_applied = 0
    for i, row in enumerate(vals[1:], start=2):
        get = lambda k: (row[k] if len(row) > k else "") or ""
        ai, newc, scope, apply_flag = get(0).strip(), get(1).strip(), (get(2) or "queue").strip().lower(), get(3).strip().lower()
        if not ai or not newc or apply_flag not in ("yes", "y", "1", "true"):
            continue

        if norm(newc) == norm(NOT_A_COMPLAINT):
            decision, target = "rejected", None
        else:
            hit = tax.get(norm(newc))
            if not hit:
                done_rows[i] = [f"ERROR: '{newc[:50]}' is not in the taxonomy"]
                continue
            target = hit[0]
            decision = "accepted" if norm(target) == norm(ai) else "corrected"

        sql = ("SELECT review_id FROM v_ai_complaints WHERE ai_complaint=%s AND human_validated=0")
        args = [ai]
        if scope != "all":
            sql += " AND needs_review=1"
        cur.execute(sql, args)
        ids = [r["review_id"] for r in cur.fetchall()]
        if not ids:
            # keep a previous outcome visible instead of replacing it with nothing
            prev = get(4).strip()
            if not prev.lower().startswith("applied"):
                done_rows[i] = ["nothing to do (no undecided reviews with that AI code)"]
            continue
        if a.dry_run:
            done_rows[i] = [f"would apply {decision} -> {target or 'removed'} to {len(ids)} review(s)"]
            print(f"  row {i}: {ai!r} -> {target or 'removed'} ({decision}) on {len(ids)} review(s)")
            continue

        # source='sheet': this is a decision written on the sheet's bulk-lines tab,
        # not one made on screen. Without the stamp it would land as the column
        # default ('app') and the row-level sheet sync would then treat its own
        # bulk line as a competing surface and hold later row edits back.
        cur.executemany("""INSERT INTO corrections (review_id, ai_complaint, new_complaint,
                                                  decision, reviewed_by, note, source)
                           VALUES (%s,%s,%s,%s,%s,%s,'sheet')""",
                        [(rid, ai, target, decision, a.reviewed_by,
                          f"bulk: {ai} -> {target or 'removed'}") for rid in ids])
        n_applied += len(ids)
        done_rows[i] = [f"applied {decision} -> {target or 'removed'} to {len(ids)} review(s) "
                        f"on {dt.date.today().isoformat()}"]
        print(f"  row {i}: {ai!r} -> {target or 'removed'} ({decision}) x {len(ids)}")

    if done_rows and not a.dry_run:
        for i, cell in done_rows.items():
            SU.values(svc, sid, f"'{TAB}'!E{i}", [cell])
    print(f"bulk apply: {len(done_rows)} line(s), {n_applied} correction(s) written"
          f"{' (dry run)' if a.dry_run else ''}")


if __name__ == "__main__":
    main()
