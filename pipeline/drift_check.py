#!/usr/bin/env python3
"""Alert when the dashboard's numbers stop meaning what they say (P10a).

Two independent lies are possible, and both were previously silent:

  agreement collapse   the weekly random check exists to prove the AI is right; if
                       this week agrees >5 points less than the trailing 4-week mean,
                       the label counts are drifting and nobody was told
  label-mix shift      a complaint's share of a week moving >3 points week-on-week
                       means a code started absorbing (or losing) reviews — usually a
                       prompt/menu change or a taxonomy edit with a side effect

Both are measured on COMPLETE weeks only. The week containing today is partial (a
Monday morning run would compare 35 rows against 1,200 and alert on every label),
so "this week" means the last full ISO week and "last week" the one before it.

Volume guard: label shares are compared only when the week has >= CX_DRIFT_MIN_WEEK_N
rows (default 100) and the label has >= CX_DRIFT_MIN_LABEL_N in either week (default
20). Below that, a single review moves several points and the alert becomes noise
that gets muted — the failure mode this check is supposed to avoid.

No agreement history yet is NOT a breach: it is reported as "not evaluable" (the
weekly sample has to be filled in the app first). The label-share check runs
regardless, so the day-one state still has coverage.

Delivery: a DM to Philip only (CX_DRIFT_NOTIFY_TO, default U0BRQ9PEQ93). Chadia is
never messaged by this check — she owns the decisions, not the pipeline.

Exit: 0 healthy / not evaluable / label-shift-only, 1 agreement collapse (cx_refresh.sh
records the run as failed and the freshness banner goes red). A label shift is DMed but
does not fail the run by default: mix moves are frequently real (seasonality, a product
launch), and a banner that is permanently red for a legitimate mix change is a banner
people stop reading. Set CX_DRIFT_SHARE_FATAL=1 to make it fatal too.

The DM is at most once per UTC day per distinct set of breaches, so a persistent
breach does not spam.

Run: cx_refresh.sh, or by hand.
  --dry-run   print, send nothing, never update the state file
  --force     DM even if already sent today
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import cxnotify

HERE = Path(__file__).resolve().parent
STATE = HERE / ".drift_state.json"
PHILIP_UID = "U0BRQ9PEQ93"

AGREE_DROP_PTS = float(os.environ.get("CX_DRIFT_AGREE_PTS", "5.0"))
SHARE_MOVE_PTS = float(os.environ.get("CX_DRIFT_SHARE_PTS", "3.0"))
MIN_WEEK_N = int(os.environ.get("CX_DRIFT_MIN_WEEK_N", "100"))
MIN_LABEL_N = int(os.environ.get("CX_DRIFT_MIN_LABEL_N", "20"))
SHARE_FATAL = os.environ.get("CX_DRIFT_SHARE_FATAL", "0") != "0"


def week_start(d):
    return d - dt.timedelta(days=d.weekday())


def db():
    import pymysql
    return pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)


def check_agreement(cur):
    """-> (status, detail dict). Compares the latest measured week to the 4 before it."""
    cur.execute("""SELECT week_start, sample_n, agreement_pct
                     FROM quality_checks
                    WHERE agreement_pct IS NOT NULL
                    ORDER BY week_start""")
    rows = cur.fetchall()
    if not rows:
        cur.execute("""SELECT COUNT(*) n, SUM(your_call IS NOT NULL) called
                         FROM weekly_samples""")
        s = cur.fetchone()
        return "no_data", {
            "why": f"no weekly check recorded yet ({int(s['called'] or 0)} of "
                   f"{int(s['n'] or 0)} drawn rows answered)"}
    last = rows[-1]
    prior = rows[-5:-1]
    if len(prior) < 2:
        return "no_data", {
            "why": f"only {len(rows)} measured week(s); a trailing mean needs 2+ "
                   f"(latest {last['week_start']} = {float(last['agreement_pct']):.2f}%)"}
    mean = sum(float(r["agreement_pct"]) for r in prior) / len(prior)
    now = float(last["agreement_pct"])
    detail = {"week": str(last["week_start"]), "now": now, "mean": mean,
              "n": int(last["sample_n"]), "weeks": [str(r["week_start"]) for r in prior],
              "drop": mean - now}
    return ("breach" if now < mean - AGREE_DROP_PTS else "ok"), detail


def label_shares(cur, wk_from, wk_to):
    """{week: {label: count}}, totals included, over final (validated) labels."""
    cur.execute("""SELECT DATE_SUB(review_date, INTERVAL WEEKDAY(review_date) DAY) wk,
                          COALESCE(final_complaint,'(none)') label, COUNT(*) n
                     FROM v_ai_complaints
                    WHERE review_date >= %s AND review_date < %s
                    GROUP BY wk, label""", (wk_from, wk_to))
    out = {}
    for r in cur.fetchall():
        out.setdefault(str(r["wk"]), {})[r["label"]] = int(r["n"])
    return out


def check_shares(cur, last_wk, prev_wk):
    sh = label_shares(cur, prev_wk, last_wk + dt.timedelta(days=7))
    a, b = sh.get(str(prev_wk), {}), sh.get(str(last_wk), {})
    ta, tb = sum(a.values()), sum(b.values())
    if tb < MIN_WEEK_N or ta < MIN_WEEK_N:
        return "no_data", {"why": f"week volume {prev_wk}={ta}, {last_wk}={tb}; "
                                  f"needs {MIN_WEEK_N} in both to compare shares"}
    moves = []
    for label in set(a) | set(b):
        na, nb = a.get(label, 0), b.get(label, 0)
        if max(na, nb) < MIN_LABEL_N:
            continue
        sa, sb = 100.0 * na / ta, 100.0 * nb / tb
        moves.append({"label": label, "prev": sa, "now": sb, "delta": sb - sa,
                      "n_prev": na, "n_now": nb})
    moves.sort(key=lambda m: -abs(m["delta"]))
    breaches = [m for m in moves if abs(m["delta"]) > SHARE_MOVE_PTS]
    return ("breach" if breaches else "ok"), {"prev_week": str(prev_wk), "week": str(last_wk),
                                             "totals": (ta, tb), "moves": moves,
                                             "breaches": breaches}


def fmt(detail_lines):
    return "\n".join(detail_lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    to = cxnotify.parse_list(os.environ.get("CX_DRIFT_NOTIFY_TO") or PHILIP_UID)

    today = dt.date.today()
    this_wk = week_start(today)
    last_wk = this_wk - dt.timedelta(days=7)
    prev_wk = last_wk - dt.timedelta(days=7)

    conn = db()
    cur = conn.cursor()
    agree_status, agree = check_agreement(cur)
    share_status, shares = check_shares(cur, last_wk, prev_wk)
    conn.close()

    print(f"drift check — complete weeks: {prev_wk} vs {last_wk} "
          f"(current week {this_wk} is partial and excluded)")

    problems, lines = [], []
    agreement_broken = False
    if agree_status == "ok":
        print(f"  agreement: {agree['now']:.2f}% (n={agree['n']}) vs trailing "
              f"{len(agree['weeks'])}-week mean {agree['mean']:.2f}% "
              f"— {abs(agree['drop']):.2f} pts below the mean, limit {AGREE_DROP_PTS:.1f}")
    elif agree_status == "breach":
        agreement_broken = True
        msg = (f"agreement {agree['now']:.2f}% for week {agree['week']} is "
               f"{agree['drop']:.1f} pts below the trailing {len(agree['weeks'])}-week "
               f"mean {agree['mean']:.2f}% (n={agree['n']}; limit {AGREE_DROP_PTS:.1f})")
        print(f"  AGREEMENT DROP: {msg}")
        problems.append(msg)
        lines.append(f"• {msg}")
    else:
        print(f"  agreement: not evaluable — {agree['why']}")

    if share_status == "ok":
        top = shares["moves"][:3]
        print(f"  label shares: none moved more than {SHARE_MOVE_PTS:.1f} pts "
              f"(largest: " + ", ".join(
                  f"{m['label']} {m['delta']:+.1f}" for m in top) + ")")
    elif share_status == "breach":
        for m in shares["breaches"]:
            msg = (f"{m['label']}: {m['prev']:.1f}% -> {m['now']:.1f}% of the week "
                   f"({m['delta']:+.1f} pts; n {m['n_prev']} -> {m['n_now']}, "
                   f"weeks {shares['prev_week']} -> {shares['week']})")
            print(f"  LABEL SHIFT: {msg}")
            problems.append(msg)
            lines.append(f"• {msg}")
    else:
        print(f"  label shares: not evaluable — {shares['why']}")

    if not problems:
        if a.dry_run:
            print("dry run: nothing to send")
        return 0

    text = (f"Dashboard drift check — {len(problems)} breach(es) on complete-week data.\n"
            f"Weeks compared: {shares.get('prev_week', prev_wk)} vs "
            f"{shares.get('week', last_wk)}\n\n"
            + "\n".join(lines)
            + "\n\nWhat this means: the label counts are moving for reasons other than "
              "the underlying reviews. Check the latest prompt/menu change "
              "(classifications.prompt_version) and any taxonomy edit before quoting "
              "these numbers.\n"
              "Not a decision request — no action needed from Chadia.")
    sig = json.dumps(sorted(problems))
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    same_day = state.get("last_sent") == today.isoformat() and state.get("sig") == sig

    if a.dry_run:
        print("\n" + text)
        return 1 if (agree_status == "breach" or
                     (share_status == "breach" and SHARE_FATAL)) else 0

    if same_day and not a.force:
        print("  (already alerted today for this exact set of breaches)")
    else:
        ok, _ = cxnotify.deliver(text,
                                 "AI complaint validation — drift alert "
                                 f"({today.isoformat()})",
                                 to_items=to, cc_items=[])
        if not ok:
            print("WARN: drift alert could not be delivered", file=sys.stderr)
        else:
            STATE.write_text(json.dumps({"last_sent": today.isoformat(), "sig": sig},
                                        indent=2))

    if agree_status == "breach":
        return 1
    if share_status == "breach" and SHARE_FATAL:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
