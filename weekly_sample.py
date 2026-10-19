#!/usr/bin/env python3
"""
The weekly random check Chadia asked for: "a weekly random check for two months,
then taper".

What existed before was an automatic rollup over the rows the model had already
flagged as uncertain (needs_review=1) — i.e. the 11% it was least sure about. That
measures the queue, not the system: it reads low (the hard rows are all in it),
moves with backlog composition, and excludes rejected rows from the denominator,
so cleaning junk can only raise it. It is kept, but labelled "queue accuracy".

This script draws a REAL sample:
  * 45 rows, stratified by category (so a small but important category such as
    High Risk is not swamped by FBA returns)
  * seeded on the ISO week, so the sample is reproducible and does not re-draw on
    every 30-minute refresh
  * written to the 'Weekly check' tab of the corrections sheet, where the checker
    fills your_call / notes
  * agreement is read back from that tab into quality_checks (sample_ref
    'weekly-<ISO week>'), which is what the Quality page's trend line shows

Run: cx_refresh.sh (idempotent), or by hand:
  python weekly_sample.py            # draw if this week has no sample, read back
  python weekly_sample.py --redraw   # replace this week's undecided sample
  python weekly_sample.py --dry-run
"""
import argparse
import datetime as dt
import os
import random
import re
import sys
from collections import defaultdict

import pymysql

import sheet_util as SU

HERE = os.path.dirname(os.path.abspath(__file__))
SHEET_ID_FILE = os.path.join(HERE, ".corrections_sheet_id")
TAB = "Weekly check"
SAMPLE_N = 45
HEADER = ["sample_week", "review_id", "date", "sku", "product", "review_text",
          "ai_complaint", "final_complaint", "your_call", "notes"]
NOT_A_COMPLAINT = "not a complaint"


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def iso_week(d=None):
    d = d or dt.date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_start(d=None):
    d = d or dt.date.today()
    return d - dt.timedelta(days=d.weekday())


def db():
    return pymysql.connect(host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
                           password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
                           charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def pool(cur):
    cur.execute("""SELECT review_id, review_date, sku, product,
                          COALESCE(final_category,'Uncategorised') cat,
                          LEFT(COALESCE(review_text,''),1200) text,
                          COALESCE(ai_complaint,'') ai_complaint,
                          COALESCE(final_complaint,'') final_complaint
                     FROM v_ai_complaints
                    WHERE review_date IS NOT NULL AND final_complaint IS NOT NULL""")
    return cur.fetchall()


def draw(rows, n=SAMPLE_N):
    """Stratified by category, deterministic per ISO week.

    Proportional allocation with at least 1 row per category, so a small but
    important stratum (High Risk, Damage) is never swamped by FBA returns.
    Within a stratum, rows are ordered by review_id and a seeded RNG picks — the
    same week always gives the same sample, a new week gives a different one.
    """
    by = defaultdict(list)
    for r in rows:
        by[r["cat"]].append(r)
    rng = random.Random(iso_week())
    total = len(rows)
    q = {c: max(1, round(n * len(items) / total)) for c, items in by.items()}

    # exact size: shrink the most over-allocated stratum first, then grow the
    # largest stratum that still has unsampled rows.
    while sum(q.values()) > n:
        c = max((c for c in q if q[c] > 1), key=lambda c: q[c] - n * len(by[c]) / total, default=None)
        if c is None:
            break
        q[c] -= 1
    while sum(q.values()) < n:
        c = max((c for c in by if q[c] < len(by[c])), key=lambda c: len(by[c]), default=None)
        if c is None:
            break
        q[c] += 1

    out = []
    for cat in sorted(by):
        items = sorted(by[cat], key=lambda r: r["review_id"])
        k = min(q.get(cat, 0), len(items))
        out.extend(rng.sample(items, k))
    rng.shuffle(out)
    return out


def write_tab(svc, sid, rows, cur):
    SU.refresh_codes_tab(svc, sid, cur)
    sid_tab = SU.ensure_tab(svc, sid, TAB, rows=len(rows) + 1, cols=len(HEADER))
    wk = iso_week()
    matrix = [HEADER] + [[wk,
                          str(r["review_id"]),
                          r["review_date"].isoformat() if r["review_date"] else "",
                          r["sku"] or "", r["product"] or "", r["text"] or "",
                          r["ai_complaint"] or "", r["final_complaint"] or "",
                          "", ""] for r in rows]
    SU.values(svc, sid, f"'{TAB}'!A1", matrix)
    SU.validation(svc, sid, sid_tab, HEADER.index("your_call"), 1, len(rows) + 1,
                  "Codes!A2:A400",
                  "Pick the complaint you would have chosen (or NOT A COMPLAINT)")
    return len(rows)


def read_back(svc, sid, cur):
    """Count the filled rows in this week's tab and store agreement."""
    try:
        vals = SU.get_values(svc, sid, f"'{TAB}'!A1:J{SAMPLE_N + 50}")
    except Exception as e:                                    # noqa: BLE001
        print(f"  could not read {TAB}: {e}")
        return None
    if not vals:
        return None
    head = [norm(h) for h in vals[0]]
    if "your_call" not in head:
        print("  Weekly check tab has no your_call column")
        return None
    i_call = head.index("your_call")
    i_week = head.index("sample_week") if "sample_week" in head else None
    i_ai = head.index("ai_complaint") if "ai_complaint" in head else None
    wk, wk_iso = week_start().isoformat(), iso_week()
    accepted = corrected = rejected = 0
    for row in vals[1:]:
        call = norm(row[i_call]) if len(row) > i_call else ""
        if not call:
            continue
        # the tab holds several weeks; only roll the current one
        row_week = (row[i_week] if i_week is not None and len(row) > i_week else "")
        if row_week and str(row_week).strip() not in (wk_iso, wk):
            continue
        ai = norm(row[i_ai]) if i_ai is not None and len(row) > i_ai else ""
        if NOT_A_COMPLAINT in call:
            rejected += 1
        elif call == ai:
            accepted += 1
        else:
            corrected += 1
    n = accepted + corrected
    if not n:
        print(f"  {TAB}: no filled calls for the week of {wk} yet")
        return None
    if n < 5:
        # One or two calls is progress, not a measurement; recording it would put a
        # 100%-on-n=1 point on the trend line.
        print(f"  {TAB}: {n} call(s) so far for {wk} — recorded once 5+ are filled")
        return None
    pct = round(accepted * 100.0 / n, 2)
    ref = f"weekly-{iso_week()}"
    cur.execute("""INSERT INTO quality_checks (week_start, sample_n, agreement_pct, sample_ref, note)
                   VALUES (%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE sample_n=VALUES(sample_n),
                     agreement_pct=VALUES(agreement_pct), sample_ref=VALUES(sample_ref),
                     note=VALUES(note)""",
                (wk, n, pct, ref,
                 f"weekly random sample: {accepted} agreed, {corrected} corrected, "
                 f"{rejected} removed (excluded from the denominator)"))
    print(f"  {TAB}: week {wk} n={n} agreement={pct}% ({ref})")
    return pct


def filled_calls(svc, sid):
    """How many calls this week's tab already has. Guards against re-drawing over
    work in progress: a refresh that wiped 20 hand-filled rows would end the
    exercise on day one."""
    try:
        vals = SU.get_values(svc, sid, f"'{TAB}'!A1:J{SAMPLE_N + 50}")
    except Exception:                                         # noqa: BLE001
        return 0
    if not vals:
        return 0
    head = [norm(h) for h in vals[0]]
    if "your_call" not in head or "sample_week" not in head:
        return 0
    i_call, i_week = head.index("your_call"), head.index("sample_week")
    wk_iso = iso_week()
    return sum(1 for r in vals[1:]
               if len(r) > i_call and norm(r[i_call])
               and (len(r) <= i_week or str(r[i_week]).strip() in (wk_iso, "")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--redraw", action="store_true", help="replace this week's undecided rows")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    rows = pool(cur)
    print(f"eligible rows: {len(rows)} · ISO week {iso_week()}")

    cur.execute("SELECT agreement_pct FROM quality_checks WHERE week_start=%s AND sample_ref LIKE 'weekly-%%'",
                (week_start(),))
    existing = cur.fetchone()

    svc = sid = None
    if os.path.exists(SHEET_ID_FILE):
        sid = open(SHEET_ID_FILE).read().strip()
        try:
            svc = SU.service()
        except Exception as e:                                # noqa: BLE001
            print(f"  sheets unavailable ({e}); sample will not be written")
    else:
        print("  no corrections sheet configured — dry numbers only")
        svc = None

    if a.dry_run:
        s = draw(rows)
        print(f"would draw {len(s)} rows:")
        for r in s[:10]:
            print(f"   {r['review_id']}  {r['cat']:<22} {r['final_complaint']}")
        if svc and sid:
            read_back(svc, sid, cur)
        return

    if svc and sid:
        filled = filled_calls(svc, sid)
        if a.redraw:
            n = write_tab(svc, sid, draw(rows), cur)
            print(f"  re-drew {n} rows in '{TAB}' (--redraw)")
        elif existing:
            print(f"  this week already has a recorded check ({existing['agreement_pct']}%)")
        elif filled:
            print(f"  '{TAB}' already has {filled} filled call(s) for {iso_week()} — not redrawing")
        else:
            n = write_tab(svc, sid, draw(rows), cur)
            print(f"  wrote {n} rows to '{TAB}'")
        read_back(svc, sid, cur)
    conn.close()


if __name__ == "__main__":
    main()
