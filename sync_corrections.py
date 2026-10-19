#!/usr/bin/env python3
"""Read Chadia's edits back from the corrections Sheet and write them to MySQL.

TWO WRITE SURFACES, ONE AUTHORITATIVE ONE (2026-10-19)
  The app is the only review surface: it is where she approves/denies now, and the
  Google sheet was withdrawn. This script still runs because the sheet may hold
  decisions made before the switch, but it can no longer revert a decision made in
  the app:

  * every corrections row carries `source` ('sheet' or 'app'; the column default is
    'app' because app.py is the only other writer)
  * before inserting, the sync compares the sheet's decision time with the
    created_at of the latest stored decision. If that latest decision came from
    another surface and is NEWER, the sheet value is stale by definition — the row
    is skipped, the sheet cell is marked 'held', and the reason is printed
  * the sheet's decision time is, in order: an optional per-row timestamp column
    (`decision_at` / `updated_at` / `timestamp` / `decided_at`) if the tab has one,
    else the file's Drive modifiedTime (the latest possible time any row was
    edited), else --sheet-ts. With no timestamp at all the sync refuses to
    override another surface — "cannot prove the sheet is newer" is not permission.

Corrections tab
  * both `your_correction` and `notes` blank -> no decision yet, skipped silently
  * `your_correction` says 'NOT A COMPLAINT' -> decision 'rejected'
  * `your_correction` equals the AI label (case/space-insensitive) -> 'accepted'
  * `your_correction` differs -> 'corrected' with new_complaint
  * sync_status 'done' is NOT a reason to skip: the row is compared against the
    decision stored in MySQL, and a different value is written as a SUPERSEDING
    row (the sheet says "done", she changed her mind, and the old code skipped it
    silently forever). A different value marks the sheet row 'revised <date>'.
  * insert is idempotent against the LATEST decision for that review, not against
    history: re-running the same sheet writes nothing.

Attribution
  * reviewed_by / reviewed_by_name come from --reviewed-by, else from the Drive
    file's lastModifyingUser (mapped through data/reviewers.json), else from the
    configured default. The run prints which source it used; it never silently
    stamps a uid that did not make the decision.

Code decisions tab
  * decision approve/deny -> proposed_codes.status approved/denied + decided_at

Usage:
  python sync_corrections.py --dry-run
  python sync_corrections.py                 # apply
  python sync_corrections.py --dry-run --sheet-id <id> --reviewed-by 42 --reviewed-by-name "Philip"

Exit codes: 0 ok, 1 >20% of rows errored (aborted), 2 bad setup.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pymysql

HERE = Path(__file__).resolve().parent
GWS_SCRIPTS = "/opt/data/skills/productivity/google-workspace/scripts"
if GWS_SCRIPTS not in sys.path:
    sys.path.insert(0, GWS_SCRIPTS)

STATE_FILE = HERE / ".corrections_sheet_id"
DENIALS_FILE = HERE / "data" / "denied_codes.json"
REVIEWERS_FILE = HERE / "data" / "reviewers.json"
ERROR_RATIO_LIMIT = 0.20
NOT_A_COMPLAINT = "not a complaint"
MAX_CODE_LEN = 120          # corrections.new_complaint / proposed_codes.code
SYNC_DONE = "done"
# The surface this script writes as. app.py's inserts take the column default
# ('app'), so a decision is never mislabelled by omission.
SOURCE = "sheet"
SOURCE_APP = "app"
HOLD_TEXT = "held: newer decision in the app — sheet value not applied"
# Optional per-row timestamps, best first. A tab without any of these falls back
# to the file's modifiedTime, which is an upper bound on every row's edit time.
TS_COLS = ("decision_at", "updated_at", "timestamp", "decided_at")

REQUIRED_CORR = ("review_id", "ai_complaint", "your_correction", "sync_status")

# Codes she denies, and the code she means instead. Deny is not a no-op: the
# affected reviews are relabelled, otherwise the database keeps a label she has
# rejected forever. The first four are the mappings the expert review prescribed;
# the last two were chosen from the reviews' own text ("Not as Expected|Too
# bright", "Not as Expected|... makes me feel too cold") for the only codes her
# sheet leaves genuinely new and unmapped.
DENY_MAP = {
    "too deep": "Doesnt Fit",
    "too flat": "Too Soft",
    "sheds - fibers fly off when touching it": "Pilling",
    "not secure": "Not Adjustable",
    "too bright": "Not As Advertised/Misleading",
    "too cold": "Not As Advertised/Misleading",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def norm(s):
    """Case/space-insensitive comparison key."""
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def cell(v):
    """Sheets value -> stripped string (numbers arrive as int/float)."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def as_review_id(s):
    s = cell(s)
    if re.fullmatch(r"\d+", s):
        return int(s)
    if re.fullmatch(r"\d+\.0", s):
        return int(float(s))
    raise ValueError(f"review_id is not an integer: {s!r}")


def classify_row(your_correction, notes, ai_complaint):
    """Pure decision logic -> (decision, new_complaint) or None for 'no call yet'.

    Precedence: explicit rejection marker wins, then blank, then accept/correct.
    """
    yc, nt = (your_correction or "").strip(), (notes or "").strip()
    if not yc:
        return None                      # nothing actionable; a lone note is not a call
    if NOT_A_COMPLAINT in yc.lower() or NOT_A_COMPLAINT in nt.lower():
        return ("rejected", None)
    if norm(yc) == norm(ai_complaint):
        return ("accepted", None)
    return ("corrected", yc)


# --------------------------------------------------------------------------
# sheets
# --------------------------------------------------------------------------
def service():
    from google_api import build_service
    return build_service("sheets", "v4")


def read_tab(svc, sid, tab, last_col="Z"):
    rows = svc.spreadsheets().values().get(
        spreadsheetId=sid,
        range=f"'{tab}'!A:{last_col}",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute().get("values", [])
    if not rows:
        return [], []
    header = [cell(h).lower() for h in rows[0]]
    return header, rows[1:]


def col(header, name):
    try:
        return header.index(name)
    except ValueError:
        raise SystemExit(f"FATAL: column {name!r} missing from header {header}")


def get(row, idx):
    return cell(row[idx]) if idx is not None and idx < len(row) else ""


def mark_done(svc, sid, tab, rows_to_mark, text=SYNC_DONE):
    """Write sync_status for the given 1-based sheet row numbers, batched."""
    if not rows_to_mark:
        return
    ranges, start, prev = [], None, None
    for r in sorted(rows_to_mark):
        if start is None:
            start = prev = r
        elif r == prev + 1:
            prev = r
        else:
            ranges.append((start, prev))
            start = prev = r
    if start is not None:
        ranges.append((start, prev))

    data = []
    for a, b in ranges:
        data.append({"range": f"'{tab}'!J{a}:J{b}",
                     "values": [[text] for _ in range(a, b + 1)]})
    for i in range(0, len(data), 100):          # keep request bodies modest
        svc.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={
            "valueInputOption": "RAW", "data": data[i:i + 100]}).execute()


def mark_errors(svc, sid, tab, items, col_letter="J"):
    """Write a visible error message into sync_status so a typo cannot vanish.

    A rejected value stays in the sheet with the reason next to it; the row is
    not marked 'done', so it comes back on the next sync and keeps asking.
    """
    if not items:
        return
    data = [{"range": f"'{tab}'!{col_letter}{r}:{col_letter}{r}",
             "values": [[msg[:300]]]} for r, msg in items]
    for i in range(0, len(data), 100):
        svc.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={
            "valueInputOption": "RAW", "data": data[i:i + 100]}).execute()


# --------------------------------------------------------------------------
# db
# --------------------------------------------------------------------------
def as_epoch(s):
    """ISO-ish timestamp -> epoch seconds (UTC), or None when unparseable.

    Accepts 'YYYY-MM-DD HH:MM:SS', 'YYYY-MM-DDTHH:MM:SSZ' and ISO with an offset.
    Google's modifiedTime is UTC with a 'Z'; MySQL datetimes are stored UTC here
    (session time zone SYSTEM == UTC, verified). Comparing epochs rather than
    naive datetimes keeps this correct if either side ever changes zone.
    """
    s = cell(s).replace("/", "-")
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:                     # a bare 'YYYY-MM-DD HH:MM:SS' is UTC here
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def sheet_edited_at(sid):
    """Epoch of the spreadsheet's last modification, or None if Drive can't say."""
    try:
        from google_api import build_service
        info = build_service("drive", "v3").files().get(
            fileId=sid, fields="modifiedTime").execute()
        return as_epoch(info.get("modifiedTime"))
    except Exception as e:                                     # noqa: BLE001
        print(f"  sheet timestamp: Drive lookup failed "
              f"({type(e).__name__}: {str(e)[:120]})")
        return None


def db():
    return pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"],
        user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"],
        database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4",
        autocommit=False,
    )


def latest_decisions(cur, review_ids):
    """review_id -> dict(decision, new, source, epoch) for the CURRENT decision.

    The latest row per review, which is exactly what v_ai_complaints resolves the
    label from. Comparing the sheet against this (rather than against all history)
    is what lets a later sheet edit supersede an earlier decision — and what lets
    the guard see that the newer decision came from the app.

    epoch is UNIX_TIMESTAMP(created_at): absolute, so a session time zone change
    cannot silently make an app decision look older than the sheet.
    """
    if not review_ids:
        return {}
    fmt = ",".join(["%s"] * len(review_ids))
    cur.execute(f"""SELECT c.review_id, c.decision, c.new_complaint,
                           COALESCE(c.source, 'app') src,
                           UNIX_TIMESTAMP(c.created_at) ts
                      FROM corrections c
                      JOIN (SELECT review_id, MAX(id) mid FROM corrections
                             WHERE review_id IN ({fmt}) GROUP BY review_id) t
                        ON t.mid = c.id""", tuple(review_ids))
    return {r[0]: {"decision": r[1], "new": norm(r[2]), "source": r[3],
                   "epoch": float(r[4] or 0)} for r in cur.fetchall()}


def all_decision_keys(cur, review_ids):
    """Every (review_id, decision, norm(new)) ever stored — for reporting only."""
    if not review_ids:
        return set()
    fmt = ",".join(["%s"] * len(review_ids))
    cur.execute(f"SELECT review_id, decision, new_complaint FROM corrections "
                f"WHERE review_id IN ({fmt})", tuple(review_ids))
    return {(r[0], r[1], norm(r[2])) for r in cur.fetchall()}


def known_review_ids(cur, review_ids):
    if not review_ids:
        return set()
    fmt = ",".join(["%s"] * len(review_ids))
    cur.execute(f"SELECT review_id FROM classifications WHERE review_id IN ({fmt})",
                tuple(review_ids))
    return {r[0] for r in cur.fetchall()}


def taxonomy(cur):
    """norm(code) -> (canonical code, category). Only approved codes."""
    cur.execute("SELECT code, category FROM complaint_codes WHERE approved=1")
    return {norm(c): (c, cat) for c, cat in cur.fetchall()}


def decision_codes(cur):
    """norm(code) -> (code, source, approved) for codes SHE has ruled on."""
    cur.execute("""SELECT code, source, approved FROM complaint_codes
                   WHERE source IN ('chadia-approved','chadia-denied')""")
    return {norm(c): (c, s, int(a)) for c, s, a in cur.fetchall()}


def write_denials_json(cur):
    """Persist deny -> replacement so the classifier applies it to FUTURE rows.

    cxvocab.canonical() reads this file, so 'deny' governs new classifications as
    well as the rows already in the database. Written from the DB (single source
    of truth) so a lost file rebuilds itself on the next sync.

    Two sources: the hard-coded DENY_MAP (the mappings the expert review
    prescribed) and `code_denials`, which is where the review app records a deny
    together with the code she chose instead. Without the second source a deny
    made on screen would relabel only the rows already stored and the code would
    come straight back on the next batch.
    """
    cur.execute("SELECT code FROM complaint_codes WHERE approved=0")
    out = {}
    for (code,) in cur.fetchall():
        target = DENY_MAP.get(norm(code))
        if target:
            out[code] = target
    try:
        cur.execute("SELECT code, replacement FROM code_denials")
        for code, replacement in cur.fetchall():
            if replacement:
                out[code] = replacement
    except Exception:                                          # noqa: BLE001
        # The table is created by app_schema.sql. A sync run against an older
        # schema must not fail over it - the DENY_MAP half still applies.
        pass
    DENIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DENIALS_FILE.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return out


# --------------------------------------------------------------------------
# corrections tab
# --------------------------------------------------------------------------
def sync_corrections(svc, sid, tab, conn, dry_run, reviewer, sheet_ts=None,
                     reader=read_tab, mark_done_fn=mark_done, mark_errors_fn=mark_errors):
    """Pull her decisions into MySQL, letting a later edit supersede an earlier one.

    reader / mark_* are injectable so this logic can be tested against the real
    database without touching her sheet; sheet_ts (epoch) exists for the same
    reason and overrides the Drive modifiedTime.

    A decision is only written when the sheet is not stale relative to another
    surface — see the module docstring and `held` below.
    """
    stats = Counter()
    header, rows = reader(svc, sid, tab)
    for name in REQUIRED_CORR:
        col(header, name)
    ix = {n: col(header, n) for n in
          ("review_id", "ai_complaint", "your_correction", "notes", "sync_status")}
    ts_ix = next((col(header, n) for n in TS_COLS if n in header), None)
    if sheet_ts is None:
        sheet_ts = sheet_edited_at(sid)
    if sheet_ts:
        when = dt.datetime.fromtimestamp(sheet_ts, dt.timezone.utc)
        basis = (f"per-row {header[ts_ix]} column when present"
                 if ts_ix is not None else "drive modifiedTime")
        print(f"[{tab}] sheet decision time : {when:%Y-%m-%dT%H:%M:%SZ} ({basis})")
    else:
        print(f"[{tab}] sheet decision time : UNKNOWN — "
              f"will refuse to override a decision from another surface")

    planned, errors, marks = [], [], {}      # marks: sheet row -> new sync_status
    bad_rows = []
    ids = []
    for i, row in enumerate(rows, start=2):          # sheet row number
        try:
            ids.append(as_review_id(get(row, ix["review_id"])))
        except ValueError:
            ids.append(None)

    cur = conn.cursor()
    known = known_review_ids(cur, [x for x in ids if x])
    latest = latest_decisions(cur, [x for x in ids if x])
    ever = all_decision_keys(cur, [x for x in ids if x])
    tax = taxonomy(cur)
    held = []

    for i, row in enumerate(rows, start=2):
        stats["rows"] += 1
        rid = ids[i - 2]
        yc = get(row, ix["your_correction"])
        nt = get(row, ix["notes"])
        status = norm(get(row, ix["sync_status"]))
        ai = get(row, ix["ai_complaint"])

        decision = classify_row(yc, nt, ai)
        if decision is None:
            # Nothing to write. A row marked done keeps its marker; an untouched
            # row stays untouched.
            if yc == "" and nt == "":
                stats["skipped_undecided"] += 1
            else:
                # a note without a replacement code: human still owes a call
                stats["skipped_note_only"] += 1
            continue

        if rid is None:
            errors.append((i, "unparseable review_id"))
            continue
        if rid not in known:
            errors.append((i, f"review_id {rid} not in classifications"))
            continue

        dec, newc = decision
        if dec == "corrected":
            # A typo used to create a brand-new category silently, which breaks
            # the SKU x complaint analysis months later with no trace. Only codes
            # in her taxonomy are accepted; anything else stays visible in the
            # sheet with the reason.
            hit = tax.get(norm(newc))
            if not hit:
                msg = (f"ERROR: '{newc[:60]}' is not in the taxonomy. "
                       f"Use the Code decisions tab to propose a new code.")
                errors.append((i, msg))
                bad_rows.append((i, msg))
                stats["skipped_unknown_code"] += 1
                continue
            newc = hit[0]                    # store her canonical spelling
        if dec == "corrected" and len(newc) > MAX_CODE_LEN:
            errors.append((i, f"correction longer than {MAX_CODE_LEN} chars ({len(newc)})"))
            continue

        key = (dec, norm(newc))
        prev = latest.get(rid)
        if prev and (prev["decision"], prev["new"]) == key:
            # Already the recorded decision. This is the idempotent case and the
            # ONLY case that is skipped -- a row she edited after it was marked
            # done falls through to a superseding insert below.
            stats["skipped_duplicate"] += 1
            if status != SYNC_DONE:
                marks[i] = SYNC_DONE
            continue

        # ---- the guard (P9) -------------------------------------------------
        # A different value is normally a superseding decision. But if the latest
        # stored decision came from ANOTHER surface and is newer than this sheet
        # row, then the sheet is the stale side and writing it would revert the
        # app decision — the exact silent revert this finding is about.
        if prev and prev["source"] != SOURCE:
            row_ts = as_epoch(get(row, ts_ix)) if ts_ix is not None else None
            cut = row_ts if row_ts is not None else sheet_ts
            if cut is None or prev["epoch"] > cut:
                stats["held_newer_other_source"] += 1
                held.append((i, rid, prev, cut))
                marks[i] = HOLD_TEXT
                continue

        planned.append((rid, ai or None, newc, dec, reviewer["uid"],
                        reviewer.get("name"), nt or None, SOURCE))
        if prev:
            stats["revised"] += 1
            marks[i] = f"revised {date.today().isoformat()}"
        else:
            marks[i] = SYNC_DONE
        # Guards duplicates inside this run: our own insert is a sheet decision at
        # "now", which is newer than anything it supersedes.
        latest[rid] = {"decision": dec, "new": norm(newc), "source": SOURCE,
                       "epoch": dt.datetime.now(dt.timezone.utc).timestamp()}

    stats["decisions"] = len(planned)
    by_dec = Counter(p[3] for p in planned)

    print(f"\n[{tab}] rows read        : {stats['rows']}")
    print(f"[{tab}] decisions to write: {stats['decisions']}  "
          f"(accepted={by_dec.get('accepted', 0)}, "
          f"corrected={by_dec.get('corrected', 0)}, "
          f"rejected={by_dec.get('rejected', 0)}; "
          f"superseding earlier decisions={stats['revised']})")
    print(f"[{tab}] skipped: no decision yet={stats['skipped_undecided']}, "
          f"note only={stats['skipped_note_only']}, "
          f"already current={stats['skipped_duplicate']}, "
          f"code not in taxonomy={stats['skipped_unknown_code']}")
    print(f"[{tab}] held back (newer decision on another surface)="
          f"{stats['held_newer_other_source']}")
    for i, rid, prev, cut in held[:10]:
        cut_s = (f"{dt.datetime.fromtimestamp(cut, dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
                 if cut else "no sheet timestamp")
        print(f"    sheet row {i}: review {rid} — kept {prev['source']} decision "
              f"{prev['decision']} ({prev['new'] or '—'}) at "
              f"{dt.datetime.fromtimestamp(prev['epoch'], dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ}, "
              f"newer than the sheet ({cut_s})")
    if len(held) > 10:
        print(f"    ... and {len(held) - 10} more held rows")
    print(f"[{tab}] attribution     : {reviewer['name'] or '—'}"
          f"{' uid ' + str(reviewer['uid']) if reviewer.get('uid') else ' (no uid — recorded by name)'}"
          f" · {reviewer['source']}")
    print(f"[{tab}] errors         : {len(errors)}")

    if dry_run:
        for p in planned[:10]:
            print(f"    would insert review_id={p[0]} decision={p[3]} "
                  f"ai={p[1]!r} new={p[2]!r}")
        if len(planned) > 10:
            print(f"    ... and {len(planned) - 10} more (dry run, nothing written)")
        if ever:
            print(f"[{tab}] {len(ever)} historical decision(s) already stored")

    if planned and not dry_run:
        cur.executemany(
            """INSERT INTO corrections
                   (review_id, ai_complaint, new_complaint, decision,
                    reviewed_by, reviewed_by_name, note, source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", planned)
        conn.commit()
        print(f"[{tab}] wrote {len(planned)} corrections rows (source='{SOURCE}')")

    if marks and not dry_run:
        done_rows = [r for r, t in marks.items() if t == SYNC_DONE]
        rev_rows = [r for r, t in marks.items() if t != SYNC_DONE and t != HOLD_TEXT]
        held_rows = [r for r, t in marks.items() if t == HOLD_TEXT]
        if done_rows:
            mark_done_fn(svc, sid, tab, done_rows)
        if rev_rows:
            mark_done_fn(svc, sid, tab, rev_rows, text=f"revised {date.today().isoformat()}")
        if held_rows:
            # Visible in the sheet itself, so anyone still looking at it sees why
            # their edit did not take effect instead of assuming the sync is broken.
            mark_done_fn(svc, sid, tab, held_rows, text=HOLD_TEXT)
        print(f"[{tab}] marked {len(done_rows)} sheet row(s) sync_status={SYNC_DONE}"
              f"{f', {len(rev_rows)} revised' if rev_rows else ''}"
              f"{f', {len(held_rows)} held' if held_rows else ''}")
    elif marks and dry_run:
        print(f"[{tab}] would mark {len(marks)} sheet row(s) sync_status")
    if bad_rows and not dry_run:
        mark_errors_fn(svc, sid, tab, bad_rows)
        print(f"[{tab}] wrote {len(bad_rows)} error message(s) into sync_status")

    return stats["rows"], len(planned), errors


# --------------------------------------------------------------------------
# code decisions tab
# --------------------------------------------------------------------------
def sync_codes(svc, sid, tab, conn, dry_run, reviewer):
    """Approve/deny -> durable. This used to only stamp proposed_codes.status.

    approve: the code joins the vocabulary (complaint_codes source
             'chadia-approved'), so its rows stop being "new" and it can be
             emitted by the classifier.
    deny:    the code is recorded as approved=0 (so it never comes back as a
             proposal), the affected classifications are relabelled to the code
             she meant, and the mapping is written to data/denied_codes.json so
             the classifier applies it to future rows too.
    """
    stats = Counter()
    header, rows = read_tab(svc, sid, tab)
    ix = {n: col(header, n) for n in ("code", "decision", "note")}

    cur = conn.cursor()
    cur.execute("SELECT code, status FROM proposed_codes")
    current = {r[0]: r[1] for r in cur.fetchall()}
    decided = decision_codes(cur)
    tax = taxonomy(cur)

    planned, errors = [], []
    for i, row in enumerate(rows, start=2):
        stats["rows"] += 1
        code = get(row, ix["code"])
        raw = norm(get(row, ix["decision"]))
        note = get(row, ix["note"])
        if not code:
            stats["skipped_blank"] += 1
            continue
        if not raw:
            stats["skipped_undecided"] += 1
            continue
        # "approve" or "approve: <Category>" — a category is required for the new
        # code to appear in any category rollup, and inventing one is worse than
        # asking. Anything after the colon is used verbatim (title-cased).
        action, _, arg = raw.partition(":")
        action, arg = action.strip(), arg.strip()
        if action not in ("approve", "deny"):
            errors.append((i, f"decision must be approve/deny, got {raw!r}"))
            continue
        if code not in current:
            errors.append((i, f"code {code!r} not in proposed_codes"))
            continue

        target = "approved" if action == "approve" else "denied"
        done = decided.get(norm(code))
        already_durable = bool(done) and done[2] == (1 if target == "approved" else 0)
        if current[code] == target and already_durable:
            stats["skipped_already"] += 1
            continue
        planned.append((target, note or None, reviewer["uid"], code, arg))
        current[code] = target

    print(f"\n[{tab}] rows read      : {stats['rows']}")
    print(f"[{tab}] decisions to write: {len(planned)}  "
          f"(approve={sum(1 for p in planned if p[0] == 'approved')}, "
          f"deny={sum(1 for p in planned if p[0] == 'denied')})")
    print(f"[{tab}] skipped: no decision={stats['skipped_undecided']}, "
          f"blank code={stats['skipped_blank']}, already decided={stats['skipped_already']}")
    print(f"[{tab}] errors       : {len(errors)}")

    if dry_run:
        for p in planned[:10]:
            print(f"    would set code={p[3]!r} status={p[0]} category={p[4]!r} note={p[1]!r}")
        if len(planned) > 10:
            print(f"    ... and {len(planned) - 10} more (dry run, nothing written)")
    elif planned:
        for target, note, by, code, arg in planned:
            cur.execute("""UPDATE proposed_codes
                              SET status=%s, decided_at=NOW(), note=%s, decided_by=%s
                            WHERE code=%s""", (target, note, by, code))
            if target == "approved":
                exact = tax.get(norm(code), (code, None))[0]
                cat = arg.title() if arg else tax.get(norm(code), (code, None))[1]
                cur.execute("""INSERT INTO complaint_codes (code, category, source, approved)
                               VALUES (%s,%s,'chadia-approved',1)
                               ON DUPLICATE KEY UPDATE approved=1,
                                 source='chadia-approved',
                                 category=COALESCE(VALUES(category), complaint_codes.category)""",
                            (exact, cat))
                cur.execute("UPDATE classifications SET is_new_code=0 WHERE complaint=%s", (code,))
                n = cur.rowcount
                if cat:
                    cur.execute("""UPDATE classifications SET category=%s
                                    WHERE complaint=%s AND (category IS NULL OR category='')""",
                                (cat, code))
                print(f"    APPROVED {code!r} -> vocabulary (category {cat or 'NOT SET — needs one'}); "
                      f"{max(n, 0)} review(s) no longer 'new'")
            else:
                mapped = DENY_MAP.get(norm(code)) or (arg.title() if arg else None)
                cur.execute("""INSERT INTO complaint_codes (code, category, source, approved)
                               VALUES (%s,NULL,'chadia-denied',0)
                               ON DUPLICATE KEY UPDATE approved=0, source='chadia-denied'""",
                            (code,))
                n = 0
                if mapped:
                    mcode, mcat = tax.get(norm(mapped), (mapped, None))
                    cur.execute("""UPDATE classifications
                                      SET complaint=%s, category=COALESCE(%s, category),
                                          is_new_code=0
                                    WHERE complaint=%s""", (mcode, mcat, code))
                    n = cur.rowcount
                    print(f"    DENIED   {code!r} -> remapped {n} review(s) to {mcode!r}")
                else:
                    print(f"    DENIED   {code!r} -> NO MAPPING chosen; reviews left "
                          f"untouched and surfaced for manual review")
        conn.commit()
        print(f"[{tab}] wrote {len(planned)} proposed_codes updates + complaint_codes decisions")
        denials = write_denials_json(cur)
        print(f"[{tab}] denied_codes.json: {denials or '{}'}")
        cur.execute("SELECT status, COUNT(*) FROM proposed_codes GROUP BY status")
        print(f"[{tab}] proposed_codes now: {dict(cur.fetchall())}")

    return stats["rows"], len(planned), errors


# --------------------------------------------------------------------------
# attribution
# --------------------------------------------------------------------------
def reviewer_map():
    """email/name (lowercased) -> {'uid', 'name'} from data/reviewers.json and APP_USERS.

    Both sources are optional: an unmapped person is recorded by name with a NULL
    uid, which is true, rather than by a uid that belongs to somebody else.
    """
    out = {}
    try:
        raw = json.loads(REVIEWERS_FILE.read_text())
    except (OSError, ValueError):
        raw = {}
    for email, rec in (raw or {}).items():
        if not isinstance(rec, dict):
            continue
        uid = rec.get("uid")
        out[email.strip().lower()] = {"uid": int(uid) if str(uid).isdigit() else None,
                                      "name": rec.get("name") or email}
        if rec.get("name"):
            out[rec["name"].strip().lower()] = {"uid": out[email.strip().lower()]["uid"],
                                                "name": rec["name"]}
    for item in (os.environ.get("APP_USERS") or "").split(","):
        # name:uid:password — the same list the review app authenticates against.
        name, _, rest = item.strip().partition(":")
        uid, _, pw = rest.partition(":")
        if name and pw:
            out.setdefault(name.lower(), {"uid": int(uid) if uid.strip().isdigit() else None,
                                          "name": name})
    return out


def resolve_reviewer(sid, explicit_uid=None, explicit_name=None, default_uid=None):
    """Who gets credited for a sheet decision, and on what evidence.

    1. --reviewed-by                       (explicit; wins)
    2. the spreadsheet's lastModifyingUser (Drive API; mapped through reviewers.json)
    3. --default-reviewer / CX_SHEET_REVIEWER_UID

    A sheet edit is not attributable per row: Drive reports the file's last
    editor. That is the strongest signal available, so it is used and labelled
    as such in the run output. Nothing here silently substitutes a uid.
    """
    if explicit_uid is not None:
        return {"uid": explicit_uid, "name": explicit_name or f"user #{explicit_uid}",
                "source": "--reviewed-by (explicit)"}
    try:
        from google_api import build_service
        info = build_service("drive", "v3").files().get(
            fileId=sid,
            fields="lastModifyingUser(displayName,emailAddress)").execute()
        user = info.get("lastModifyingUser") or {}
        email = (user.get("emailAddress") or "").strip()
        display = (user.get("displayName") or "").strip()
        hit = None
        if email or display:
            m = reviewer_map()
            hit = m.get(email.lower()) or m.get(display.lower())
        if hit:
            return {"uid": hit["uid"], "name": hit["name"],
                    "source": f"sheet's last editor {email or display} (mapped)"}
        if display or email:
            return {"uid": None, "name": display or email,
                    "source": f"sheet's last editor {email or display} (no uid mapping — recorded by name)"}
    except Exception as e:                                     # noqa: BLE001
        print(f"  attribution: Drive lookup failed ({type(e).__name__}: {str(e)[:120]})")
    if default_uid is not None:
        return {"uid": default_uid, "name": explicit_name or f"user #{default_uid}",
                "source": "configured default (Drive lookup unavailable — verify this is right)"}
    return {"uid": None, "name": None, "source": "unattributed (no Drive user, no default configured)"}


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sheet-id", default=None)
    ap.add_argument("--corrections-tab", default="Corrections")
    ap.add_argument("--codes-tab", default="Code decisions")
    ap.add_argument("--reviewed-by", type=int, default=None,
                    help="uid to record; omit to use the sheet's last editor")
    ap.add_argument("--reviewed-by-name", default=None,
                    help="name recorded next to --reviewed-by")
    ap.add_argument("--default-reviewer", type=int,
                    default=int(os.environ.get("CX_SHEET_REVIEWER_UID") or 0) or None,
                    help="uid used only when Drive cannot say who edited the sheet")
    ap.add_argument("--sheet-ts", default=None,
                    help="ISO timestamp of the sheet's decision time; overrides Drive "
                         "modifiedTime (used by tests, and when Drive is unavailable)")
    args = ap.parse_args()

    sid = args.sheet_id
    if not sid and STATE_FILE.exists():
        sid = STATE_FILE.read_text().strip()
    if not sid:
        print("FATAL: no sheet id (pass --sheet-id or run make_corrections_sheet.py)")
        sys.exit(2)

    print(f"sheet  : {sid}")
    print(f"mode   : {'DRY RUN — nothing will be written' if args.dry_run else 'APPLY'}")
    reviewer = resolve_reviewer(sid, args.reviewed_by, args.reviewed_by_name, args.default_reviewer)
    print(f"credit : {reviewer['name'] or 'unattributed'}"
          f"{' (uid ' + str(reviewer['uid']) + ')' if reviewer['uid'] else ''} · {reviewer['source']}")

    svc = service()
    conn = db()
    try:
        c_rows, c_dec, c_err = sync_corrections(
            svc, sid, args.corrections_tab, conn, args.dry_run, reviewer,
            sheet_ts=as_epoch(args.sheet_ts) if args.sheet_ts else None)
        k_rows, k_dec, k_err = sync_codes(
            svc, sid, args.codes_tab, conn, args.dry_run, reviewer)

        total_rows = c_rows + k_rows
        total_dec = c_dec + k_dec
        errors = c_err + k_err
        print("\n=== summary ===")
        print(f"rows read          : {total_rows} "
              f"(corrections {c_rows}, codes {k_rows})")
        print(f"decisions written  : {total_dec} "
              f"(corrections {c_dec}, codes {k_dec})")
        print(f"errors             : {len(errors)}")
        for row_no, msg in errors[:15]:
            print(f"  row {row_no}: {msg}")
        if len(errors) > 15:
            print(f"  ... and {len(errors) - 15} more")
        if args.dry_run:
            print("dry run: no DB rows and no sheet cells were changed")

        # post-write DB truth
        if not args.dry_run:
            cur = conn.cursor()
            cur.execute("SELECT decision, COUNT(*) FROM corrections GROUP BY decision")
            print("corrections in DB  :", dict(cur.fetchall()) or "(empty)")
            cur.execute("SELECT status, COUNT(*) FROM proposed_codes GROUP BY status")
            print("proposed_codes     :", dict(cur.fetchall()))
            cur.execute("SELECT COUNT(*) FROM corrections WHERE reviewed_by IS NULL")
            print("reviewed_by null   :", cur.fetchone()[0])
            cur.execute("SELECT source, COUNT(*) FROM corrections GROUP BY source")
            print("by source          :", dict(cur.fetchall()) or "(empty)")

        if total_rows and len(errors) / total_rows > ERROR_RATIO_LIMIT:
            print(f"\nABORT: {len(errors)}/{total_rows} rows errored "
                  f"(> {int(ERROR_RATIO_LIMIT * 100)}%) — fix the sheet and re-run",
                  file=sys.stderr)
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
