#!/usr/bin/env python3
"""Tell Chadia when the AI has proposed complaint codes she must approve or deny.

"Notified if it creates new complaint categories so I can approve or deny" is the
one governance control she named, and she asked to be the one notified — the alert
used to go to Philip and to point at the Google sheet's 'Code decisions' tab, a
surface the project has since withdrawn. A digest that names the wrong person and
the wrong place is worse than no digest: it looks like governance is running.

Now:

  * recipient comes from CX_NOTIFY_TO (comma-separated Slack member ids and/or
    email addresses), with CX_NOTIFY_CC copied. Default: Chadia as the decision
    owner, Philip copied — see cxnotify.recipients()
  * the body names the app URL (/codes) where the decision is actually made, and
    every pending code with its review count and one example review
  * at most once per UTC day, and only when the pending set changed (or a week has
    passed), so a code sitting in the queue does not become daily noise
  * always writes a digest file under /opt/data/cache/complaint-digest/
  * Slack DM first, email via the Workspace CLI as the fallback

Run: cx_refresh.sh (self-gating, idempotent), or by hand.
  --force            send now even if nothing changed
  --dry-run          print the digest, send nothing
  --to <ids/emails>  override the primary recipient for a test; implies test mode
                     (no state update, so the real daily send is not suppressed)
"""
import argparse
import datetime as dt
import json
import os
import sys

import cxnotify

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, ".pending_digest_state.json")
OUTDIR = "/opt/data/cache/complaint-digest"
APP_URL = (os.environ.get("CX_APP_URL") or "https://philip.amzmanager.net").rstrip("/")
SUBJECT = "AI complaint validation — codes awaiting your approve/deny"


def pending():
    import pymysql
    conn = pymysql.connect(host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
                           password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
                           charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()
    cur.execute("""SELECT code, n_rows, LEFT(COALESCE(example_review,''),220) ex
                     FROM proposed_codes WHERE status='pending' ORDER BY n_rows DESC, code""")
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*) c FROM proposed_codes WHERE status='approved'")
    approved = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM complaint_codes WHERE source='chadia-approved' AND category IS NULL")
    no_cat = cur.fetchone()["c"]
    conn.close()
    return rows, approved, no_cat


def digest_text(rows, approved, no_cat):
    total = sum(int(r["n_rows"] or 0) for r in rows)
    n = len(rows)
    lines = [
        f"{n} complaint code{'s' if n != 1 else ''} proposed by the AI "
        f"{'are' if n != 1 else 'is'} waiting for your approve/deny.",
        f"{total} review{'s' if total != 1 else ''} sit{'s' if total == 1 else ''} on "
        f"{'them' if n != 1 else 'it'}.",
        "",
        "Decide here (the sheet is no longer used):",
        f"  {APP_URL}/codes",
        "",
    ]
    for r in rows:
        ex = " ".join((r["ex"] or "").split())
        lines.append(f"• {r['code']} — {int(r['n_rows'] or 0)} review(s)")
        if ex:
            lines.append(f'    e.g. "{ex[:180]}"')
    lines += [
        "",
        f"Approve = the code joins the vocabulary ({approved} approved so far) and its "
        "reviews stop being counted as new.",
        "Deny needs the code you meant instead, so those reviews are relabelled rather "
        "than dropped.",
        "On approve, also pick the code's category — without one its reviews are absent "
        "from every category rollup.",
    ]
    if no_cat:
        lines.append(f"NOTE: {no_cat} approved code(s) still have no category.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--to", default=None,
                    help="comma-separated Slack ids/emails; overrides CX_NOTIFY_TO for a test")
    a = ap.parse_args()

    rows, approved, no_cat = pending()
    if not rows:
        print("no pending codes — nothing to send")
        return

    test_mode = a.to is not None
    to_items = cxnotify.parse_list(a.to) if test_mode else None
    cc_items = [] if test_mode else None

    today = dt.date.today().isoformat()
    codes = sorted(r["code"] for r in rows)
    try:
        state = json.load(open(STATE))
    except (OSError, ValueError):
        state = {}
    last = state.get("last_sent")
    changed = state.get("codes") != codes
    stale = not last or (dt.date.today() - dt.date.fromisoformat(last)).days >= 7
    if not (a.force or test_mode or a.dry_run):
        if last == today:
            print(f"already sent today ({today})")
            return
        if not changed and not stale:
            print("pending set unchanged since last digest — not sending")
            return

    text = digest_text(rows, approved, no_cat)
    os.makedirs(OUTDIR, exist_ok=True)
    path = os.path.join(OUTDIR, f"pending-codes-{today}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(f"digest file: {path}")

    if a.dry_run:
        print("\n" + text)
        return

    to_list = to_items if to_items is not None else cxnotify.recipients()[0]
    cc_list = cc_items if cc_items is not None else cxnotify.recipients()[1]
    if test_mode:
        # A test must never look like the real alert in the log, and must not
        # consume the day's one send.
        print(f"TEST MODE — primary recipient overridden to {to_list}; no state written")
        text = "[TEST — no action needed]\n\n" + text
    print(f"to: {to_list}  cc: {cc_list or '—'}")
    ok, _ = cxnotify.deliver(text, SUBJECT, to_list, cc_list)
    if not ok:
        print("WARN: no notification channel succeeded; the digest file is on disk",
              file=sys.stderr)
        sys.exit(1)

    if not test_mode:
        json.dump({"last_sent": today, "codes": codes}, open(STATE, "w"), indent=2)
        print(f"sent {len(rows)} pending code(s)")


if __name__ == "__main__":
    main()
