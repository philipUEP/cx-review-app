#!/usr/bin/env python3
"""P9 regression test: the sheet must not revert a newer app decision.

Runs the real sync_corrections() against the real automation_philip database with
a fake sheet reader (no Google calls, no sheet writes) and a real corrections row
standing in for a decision made in the app.

  1. app decision at T-10min ('rejected'), sheet row timestamped T-1h saying
     'accepted'  -> must be HELD: zero rows inserted, app decision still latest
  2. same app decision, sheet row timestamped T+1min ('accepted')
     -> must be WRITTEN with source='sheet' (the sheet really is newer)
  3. re-run of case 2 -> idempotent: nothing inserted

Every row this test creates is deleted before it exits, and the cleanup is
verified (corrections count for the test review must be 0 again).

Run: set -a && . /opt/data/.env && set +a && uv run --with pymysql python3 \
       tests/test_sheet_source_guard.py
"""
import datetime as dt
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import sync_corrections as sc           # noqa: E402

HEADER = ["review_id", "ai_complaint", "your_correction", "notes", "sync_status",
          "decision_at"]


def fake_reader(review_id, ai_code, your_correction, ts_iso):
    """One-row sheet tab. ai_complaint is the real code, so a matching correction
    is 'accepted' and any other code is 'corrected' (both exercised below)."""
    def read(svc, sid, tab):
        return HEADER, [[str(review_id), ai_code, your_correction, "", "", ts_iso]]
    return read


def noop(*a, **k):
    return None


def latest(cur, rid):
    cur.execute("""SELECT decision, COALESCE(source,'app'), UNIX_TIMESTAMP(created_at)
                     FROM corrections WHERE review_id=%s ORDER BY id DESC LIMIT 1""",
                (rid,))
    return cur.fetchone()


def run(conn, rid, your_correction, ts_iso, ai_code, label, sheet_ts):
    print(f"\n--- {label} (sheet decision_at={ts_iso})")
    sc.sync_corrections(None, "fake-sheet", "Corrections", conn, False,
                        {"uid": 1089, "name": "test", "source": "test"},
                        sheet_ts=sheet_ts, reader=fake_reader(rid, ai_code, your_correction, ts_iso),
                        mark_done_fn=noop, mark_errors_fn=noop)


def main():
    conn = sc.db()
    cur = conn.cursor()
    cur.execute("""SELECT review_id, complaint FROM classifications
                    WHERE complaint IS NOT NULL
                      AND review_id NOT IN (SELECT review_id FROM corrections)
                    ORDER BY review_id LIMIT 1""")
    rid, ai_code = cur.fetchone()
    print(f"test review_id={rid} ai_complaint={ai_code!r}")

    created = []
    try:
        # --- the app's decision, 10 minutes ago, 'rejected' -------------------
        cur.execute("""INSERT INTO corrections
                         (review_id, ai_complaint, new_complaint, decision,
                          reviewed_by, reviewed_by_name, note, source, created_at)
                       VALUES (%s,%s,NULL,'rejected',42,'P9 test (app)','P9 TEST','app',
                               NOW() - INTERVAL 10 MINUTE)""", (rid, ai_code))
        created.append(cur.lastrowid)
        conn.commit()
        app_row = latest(cur, rid)
        print(f"app decision: {app_row}")

        now = dt.datetime.now(dt.timezone.utc)
        # --- 1. sheet row OLDER than the app decision -------------------------
        old_ts = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run(conn, rid, ai_code, old_ts, ai_code, "1. stale sheet row", sheet_ts=now.timestamp())
        after = latest(cur, rid)
        assert after == app_row, f"WRONG: stale sheet row overwrote the app decision ({after})"
        cur.execute("SELECT COUNT(*) FROM corrections WHERE review_id=%s", (rid,))
        assert cur.fetchone()[0] == 1, "WRONG: a row was inserted from the stale sheet value"
        print("PASS 1: stale sheet row held; app decision intact, no insert")

        # --- 2. sheet row NEWER than the app decision -------------------------
        new_ts = (now + dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run(conn, rid, ai_code, new_ts, ai_code, "2. newer sheet row", sheet_ts=now.timestamp())
        after2 = latest(cur, rid)
        print(f"latest now: {after2}")
        assert after2[0] == "accepted" and after2[1] == "sheet", \
            f"WRONG: newer sheet decision was not written ({after2})"
        cur.execute("SELECT id FROM corrections WHERE review_id=%s ORDER BY id", (rid,))
        created = [r[0] for r in cur.fetchall()]
        print("PASS 2: newer sheet row written with source='sheet'")

        # --- 3. idempotent re-run of the same sheet row -----------------------
        run(conn, rid, ai_code, new_ts, ai_code, "3. same sheet row again", sheet_ts=now.timestamp())
        cur.execute("SELECT COUNT(*) FROM corrections WHERE review_id=%s", (rid,))
        assert cur.fetchone()[0] == len(created), "WRONG: re-run inserted a duplicate row"
        print("PASS 3: re-run was idempotent")

        # --- 4. no sheet timestamp at all -> refuse to override ---------------
        cur.execute("""INSERT INTO corrections
                         (review_id, ai_complaint, new_complaint, decision,
                          reviewed_by, reviewed_by_name, note, source)
                       VALUES (%s,%s,NULL,'rejected',42,'P9 test (app)','P9 TEST','app')""",
                    (rid, ai_code))
        created.append(cur.lastrowid)
        conn.commit()
        before4 = latest(cur, rid)
        print("\n--- 4. sheet timestamp unknown (Drive unreachable, no per-row column)")
        sc.sheet_edited_at = lambda sid: None      # simulate a Drive failure
        sc.sync_corrections(None, "fake-sheet", "Corrections", conn, False,
                            {"uid": 1089, "name": "test", "source": "test"},
                            sheet_ts=None, reader=fake_reader(rid, ai_code, ai_code, ""),
                            mark_done_fn=noop, mark_errors_fn=noop)
        after4 = latest(cur, rid)
        assert after4 == before4, f"WRONG: no timestamp and it overwrote anyway ({after4})"
        print("PASS 4: no sheet timestamp -> nothing overwritten")
        assert sc.as_epoch("") is None and sc.as_epoch("not a date") is None
        print("PASS 4b: as_epoch returns None for missing/garbage timestamps")
    finally:
        for i in created:
            cur.execute("DELETE FROM corrections WHERE id=%s", (i,))
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM corrections WHERE review_id=%s", (rid,))
        left = cur.fetchone()[0]
        print(f"\ncleanup: {len(created)} test row(s) deleted; rows left for review "
              f"{rid}: {left}")
        assert left == 0, "cleanup failed — remove the test corrections rows by hand"
        cur.execute("SELECT COUNT(*) FROM corrections")
        print(f"corrections table total: {cur.fetchone()[0]}")
    conn.close()
    print("\nALL P9 CHECKS PASSED")


if __name__ == "__main__":
    main()
