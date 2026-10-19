#!/usr/bin/env python3
"""
Keep the 'Code decisions' tab aligned with the live proposed_codes table.

The tab was seeded with 27 codes; after re-flagging against Chadia's own taxonomy
only 10 remain. A stale tab is actively harmful: it asks her to decide codes she
already defined, and it desynchronises the approve/deny read-back.
Existing decisions for codes that still exist are preserved.
"""
import json, os, subprocess

PROJ = os.path.dirname(os.path.abspath(__file__))
GAPI = "/opt/data/skills/productivity/google-workspace/scripts/google_api.py"
GWS = "/opt/data/venvs/gws/bin/python"
TAB = "Code decisions"


def g(*args):
    env = dict(os.environ, HERMES_HOME="/opt/data")
    p = subprocess.run([GWS, GAPI] + list(args), capture_output=True, text=True, env=env, timeout=180)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[:300])
    return p.stdout


def main():
    import pymysql
    sid = open(os.path.join(PROJ, ".corrections_sheet_id")).read().strip()
    conn = pymysql.connect(host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
                           password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
                           charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    cur = conn.cursor()
    cur.execute("SELECT code, n_rows, example_review, status FROM proposed_codes ORDER BY n_rows DESC")
    props = cur.fetchall()

    # keep decisions already made in the sheet for codes that survive
    try:
        old = json.loads(g("sheets", "get", sid, f"{TAB}!A2:E200")) or []
    except Exception:
        old = []
    decided = {r[0].strip(): (r[3] if len(r) > 3 else "", r[4] if len(r) > 4 else "")
               for r in old if r and r[0]}

    rows = [["code", "reviews_affected", "example_review", "decision", "note"]]
    for p in props:
        d, n = decided.get(p["code"], ("", ""))
        rows.append([p["code"], str(p["n_rows"]), (p["example_review"] or "")[:300], d, n])
    # pad to clear rows that no longer exist
    while len(rows) < max(30, len(old) + 1):
        rows.append(["", "", "", "", ""])

    g("sheets", "update", sid, f"{TAB}!A1:E{len(rows)}", "--values", json.dumps(rows))
    print(f"{TAB}: {len(props)} codes written (was {len(old)} rows)")


if __name__ == "__main__":
    main()
