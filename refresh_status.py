#!/usr/bin/env python3
"""Refresh health: record every cx_refresh.sh run and read back the last one that
worked.

Why the `runs` table: it already exists, it already means "a batch run happened,
here is when it finished and how many errors it had", and recording the refresh
there keeps classification provenance and refresh provenance in one place instead
of adding a table nobody looks at.

A refresh run is a row with run_id 'refresh-<utc timestamp>' and model
'cx_refresh'. n_errors is the number of failed steps, so "last successful sync"
is the newest refresh row with n_errors=0 — a run that reported success while a
step failed cannot masquerade as fresh data, because the script writes the row
only after it knows every step's exit code.

CLI (used by cx_refresh.sh):
  python refresh_status.py record --ok|--failed --note "..." [--started ISO]
  python refresh_status.py show
"""
import argparse
import os
import sys
from datetime import datetime, timezone

import pymysql

SLA_HOURS = 24
PREFIX = "refresh-"


def _connect():
    return pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def record(started, ok, note, n_errors=0, host=""):
    """Insert one refresh run. Idempotent on run_id: re-running the same start
    stamp updates the row instead of stacking duplicates."""
    run_id = PREFIX + started.strftime("%Y%m%dT%H%M%SZ")
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO runs (run_id, model, prompt_version, started_at, finished_at,
                                        n_rows, n_errors, note)
                       VALUES (%s,'cx_refresh','cx-refresh',%s,UTC_TIMESTAMP(),0,%s,%s)
                       ON DUPLICATE KEY UPDATE finished_at=UTC_TIMESTAMP(), n_errors=VALUES(n_errors),
                         note=VALUES(note)""",
                    (run_id, started.strftime("%Y-%m-%d %H:%M:%S"), n_errors,
                     (note or "")[:255]))
        # One row per 30-minute run forever is 17k rows a year nobody reads. Keep
        # a month: enough to answer "when did it last work?" from the table alone.
        cur.execute(f"DELETE FROM runs WHERE run_id LIKE '{PREFIX}%%' "
                    f"AND finished_at < UTC_TIMESTAMP() - INTERVAL 30 DAY")
        return run_id
    finally:
        conn.close()


def status(cur):
    """{'last_ok','last_run','failed','note','age_h','ok'} — one query, no guessing.

    last_run is the newest attempt (successful or not) so a failed refresh is
    visible even when an older success is still inside the window.
    """
    cur.execute(f"""SELECT run_id, finished_at, n_errors, note FROM runs
                     WHERE run_id LIKE '{PREFIX}%%' AND finished_at IS NOT NULL
                     ORDER BY finished_at DESC LIMIT 1""")
    last = cur.fetchone()
    cur.execute(f"""SELECT finished_at FROM runs
                     WHERE run_id LIKE '{PREFIX}%%' AND n_errors=0 AND finished_at IS NOT NULL
                     ORDER BY finished_at DESC LIMIT 1""")
    ok = cur.fetchone()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    last_ok = ok["finished_at"] if ok else None
    age = (now - last_ok).total_seconds() / 3600.0 if last_ok else None
    return {
        "last_ok": last_ok,
        "last_run": last["finished_at"] if last else None,
        "failed": bool(last and (last["n_errors"] or 0) > 0),
        "note": (last["note"] if last else "") or "",
        "age_h": age,
        "ok": bool(age is not None and age <= SLA_HOURS),
    }


def ago(h):
    if h is None:
        return "never"
    if h < 1:
        return f"{int(h * 60)} min ago"
    if h < 48:
        return f"{h:.0f} h ago"
    return f"{h / 24:.0f} days ago"


def banner_html(st, now=None, run_errors=None):
    """A strip a CX manager cannot misread: green inside 24h, red past it.

    run_errors is set by cx_refresh.sh while building after a failed step, so the
    page that gets published says so immediately instead of waiting for the next
    run to notice the failure in the runs table.

    Returns (html, level) with level in {'ok','bad'}.
    """
    if run_errors:
        return (f'<div class="syncbar bad"><b>This build is from a failed refresh.</b> '
                f'{run_errors} — the figures below may be a run behind. See the summary in '
                f'the refresh log.</div>', "bad")
    if st["last_ok"] is None:
        return ('<div class="syncbar bad"><b>No successful refresh recorded.</b> '
                'The data below is whatever the last manual build produced — do not report from it.</div>',
                "bad")
    stamp = st["last_ok"].strftime("%d %b %Y %H:%M UTC")
    if st["ok"]:
        extra = ""
        if st["failed"]:
            extra = (f' <b>Latest attempt failed</b>{": " + st["note"] if st["note"] else ""} '
                     f'— the figures are from {st["last_ok"].strftime("%d %b %H:%M")}.')
        return (f'<div class="syncbar ok">Data refreshed <b>{stamp}</b> '
                f'({ago(st["age_h"])}).{extra}</div>', "ok")
    return (f'<div class="syncbar bad"><b>Data is stale</b> — last successful refresh '
            f'{stamp}, {ago(st["age_h"])}; this page is meant to refresh every 30 minutes '
            f'and is refreshed at most daily. Do not quote these figures until it is fixed.'
            + (f' Latest error: {st["note"]}' if st["failed"] and st["note"] else "")
            + '</div>', "bad")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--started", default=None, help="UTC ISO timestamp of the run start")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--ok", action="store_true")
    g.add_argument("--failed", action="store_true")
    r.add_argument("--note", default="")
    r.add_argument("--errors", type=int, default=0)
    sub.add_parser("show")
    a = ap.parse_args()

    if a.cmd == "record":
        started = (datetime.fromisoformat(a.started.replace("Z", ""))
                   if a.started else datetime.now(timezone.utc).replace(tzinfo=None))
        run_id = record(started, a.ok, a.note, 0 if a.ok else max(1, a.errors))
        print(f"recorded {run_id} ok={a.ok} errors={0 if a.ok else max(1, a.errors)}")
        return 0
    st = status(_connect().cursor())
    print(f"last successful refresh: {st['last_ok'] or 'never'} ({ago(st['age_h'])})")
    print(f"last attempt          : {st['last_run'] or 'never'}"
          f"{' FAILED' if st['failed'] else ''}")
    print(f"note                  : {st['note'] or '—'}")
    print(f"within {SLA_HOURS}h SLA      : {'yes' if st['ok'] else 'NO'}")
    return 0 if st["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
