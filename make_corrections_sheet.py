#!/usr/bin/env python3
"""Create the 'AI Complaint Validation — Corrections' Google Sheet.

Two tabs, both pre-filled from `automation_philip`:

  Corrections    sample of needs_review=1 reviews (newest 500) waiting for a
                 human call in the `your_correction` / `notes` columns.
  Code decisions proposed_codes awaiting approve/deny.

The sheet is created in Philip's Drive and NOT shared with anyone — sharing is
a separate, explicit decision.  Run with --print-only to preview without
creating anything; --force to create another copy when one already exists.

Usage:
  HERMES_HOME=/opt/data /opt/data/venvs/gws/bin/python make_corrections_sheet.py
"""
import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pymysql

HERE = Path(__file__).resolve().parent
GWS_SCRIPTS = "/opt/data/skills/productivity/google-workspace/scripts"
if GWS_SCRIPTS not in sys.path:
    sys.path.insert(0, GWS_SCRIPTS)

TITLE = "AI Complaint Validation \u2014 Corrections"
SAMPLE_LIMIT = 500
STATE_FILE = HERE / ".corrections_sheet_id"

CORR_HEADER = ["review_id", "date", "sku", "product", "review_text",
               "ai_complaint", "ai_category", "your_correction", "notes",
               "sync_status"]
CODE_HEADER = ["code", "reviews_affected", "example_review", "decision", "note"]

# 0-based column widths in pixels
CORR_WIDTHS = [100, 100, 95, 150, 520, 190, 90, 200, 200, 90]
CODE_WIDTHS = [260, 120, 520, 110, 220]


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------
def db():
    conn = pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"],
        user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"],
        database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    return conn


def cell_date(v):
    if isinstance(v, (date, datetime)):
        return v.strftime("%Y-%m-%d")
    return (v or "").strip() if v else ""


def fetch_reviews(conn, limit):
    with conn.cursor() as cur:
        # Highest-volume (SKU x code) first, so the corrections that matter most are
        # at the top of the sheet instead of buried under date order.
        cur.execute(
            """SELECT c.review_id, c.review_date, c.sku, c.product, c.review_text,
                      c.complaint, c.category, COALESCE(v.n, 0) vol
                 FROM classifications c
                 LEFT JOIN (SELECT sku, complaint, COUNT(*) n FROM classifications
                             WHERE sku IS NOT NULL AND complaint IS NOT NULL
                             GROUP BY sku, complaint) v
                        ON v.sku = c.sku AND v.complaint = c.complaint
                WHERE c.needs_review = 1
                ORDER BY vol DESC, c.review_date DESC, c.review_id DESC
                LIMIT %s""",
            (limit,),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append([
            str(r["review_id"]),
            cell_date(r["review_date"]),
            r["sku"] or "",
            r["product"] or "",
            (r["review_text"] or "").strip(),
            r["complaint"] or "",
            r["category"] or "",
            "",   # your_correction — the human fills this
            "",   # notes
            "",   # sync_status
        ])
    return out


def fetch_codes(conn):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT code, n_rows, example_review
                 FROM proposed_codes
                WHERE status = 'pending'
                ORDER BY n_rows DESC, code ASC"""
        )
        rows = cur.fetchall()
    return [[r["code"], r["n_rows"], (r["example_review"] or "").strip(), "", ""]
            for r in rows]


def fetch_vocabulary(conn):
    """Every approved code — the dropdown source for your_correction."""
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM complaint_codes WHERE approved=1 ORDER BY code")
        return [r["code"] for r in cur.fetchall()]


BULK_HEADER = ["ai_complaint", "new_code", "scope", "apply", "done"]
BULK_ROWS = 12


# --------------------------------------------------------------------------
# Sheets
# --------------------------------------------------------------------------
def service():
    from google_api import build_service
    return build_service("sheets", "v4")


def a1(name, rng):
    return f"'{name}'!{rng}"


def build_requests(sid_corr, sid_code, n_corr, n_code, sid_vocab=None, n_vocab=0, sid_bulk=None):
    """Formatting batchUpdate: widths, freeze, header style, wrap, validation."""
    def sheet_props(ws_id):
        return {"sheetId": ws_id}

    reqs = []

    # column widths
    for ws_id, widths in ((sid_corr, CORR_WIDTHS), (sid_code, CODE_WIDTHS)):
        for i, px in enumerate(widths):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": ws_id, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": px},
                "fields": "pixelSize"}})

    # freeze header row
    for ws_id in (sid_corr, sid_code):
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": ws_id,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}})

    # header style: bold, white on dark, vertically centred
    for ws_id, ncols in ((sid_corr, len(CORR_HEADER)), (sid_code, len(CODE_HEADER))):
        reqs.append({"repeatCell": {
            "range": {"sheetId": ws_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.13, "green": 0.16, "blue": 0.22},
                "textFormat": {"bold": True,
                               "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)"}})

    # body: top-aligned + wrapped, so long review text is readable
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid_corr, "startRowIndex": 1, "endRowIndex": max(n_corr + 1, 2),
                  "startColumnIndex": 0, "endColumnIndex": len(CORR_HEADER)},
        "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP",
                                       "verticalAlignment": "TOP"}},
        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid_code, "startRowIndex": 1, "endRowIndex": max(n_code + 1, 2),
                  "startColumnIndex": 0, "endColumnIndex": len(CODE_HEADER)},
        "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP",
                                       "verticalAlignment": "TOP"}},
        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}})

    # decision column dropdown on Code decisions (column D)
    reqs.append({"setDataValidation": {
        "range": {"sheetId": sid_code, "startRowIndex": 1,
                  "endRowIndex": max(n_code + 1, 2),
                  "startColumnIndex": 3, "endColumnIndex": 4},
        "rule": {"condition": {"type": "ONE_OF_LIST",
                               "values": [{"userEnteredValue": "approve"},
                                          {"userEnteredValue": "deny"}]},
                 "showCustomUi": True, "strict": False,
                 "inputMessage": "approve, 'approve: Category', or deny"}}})

    # your_correction dropdown from the Codes tab (column H). 136 codes is too many
    # for ONE_OF_LIST, and a free-text cell let a typo create a category.
    if sid_vocab is not None and n_vocab:
        reqs.append({"setDataValidation": {
            "range": {"sheetId": sid_corr, "startRowIndex": 1,
                      "endRowIndex": max(n_corr + 1, 2),
                      "startColumnIndex": 7, "endColumnIndex": 8},
            "rule": {"condition": {"type": "ONE_OF_RANGE",
                                   "values": [{"userEnteredValue": "=Codes!A2:A400"}]},
                     "showCustomUi": True, "strict": False,
                     "inputMessage": "Pick a code from the list (or NOT A COMPLAINT)"}}})

    if sid_bulk is not None:
        reqs.append({"setDataValidation": {
            "range": {"sheetId": sid_bulk, "startRowIndex": 1, "endRowIndex": BULK_ROWS + 1,
                      "startColumnIndex": 1, "endColumnIndex": 2},
            "rule": {"condition": {"type": "ONE_OF_RANGE",
                                   "values": [{"userEnteredValue": "=Codes!A2:A400"}]},
                     "showCustomUi": True, "strict": False,
                     "inputMessage": "Code to apply to every matching review"}}})
        reqs.append({"setDataValidation": {
            "range": {"sheetId": sid_bulk, "startRowIndex": 1, "endRowIndex": BULK_ROWS + 1,
                      "startColumnIndex": 2, "endColumnIndex": 3},
            "rule": {"condition": {"type": "ONE_OF_LIST",
                                   "values": [{"userEnteredValue": "queue"},
                                              {"userEnteredValue": "all"}]},
                     "showCustomUi": True, "strict": False,
                     "inputMessage": "queue = flagged rows only, all = every row with that AI code"}}})
    return reqs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=SAMPLE_LIMIT,
                    help="needs_review rows to pre-fill (default 500)")
    ap.add_argument("--print-only", action="store_true",
                    help="show what would be written, create nothing")
    ap.add_argument("--force", action="store_true",
                    help="create a new copy even if a sheet id is already recorded")
    args = ap.parse_args()

    conn = db()
    reviews = fetch_reviews(conn, args.limit)
    codes = fetch_codes(conn)
    vocab = fetch_vocabulary(conn)
    conn.close()
    print(f"needs_review sample : {len(reviews)} rows")
    print(f"pending codes       : {len(codes)} rows")
    print(f"approved vocabulary : {len(vocab)} codes")

    if args.print_only:
        print("\nheader :", CORR_HEADER)
        for r in reviews[:3]:
            print("row    :", [c[:60] for c in r])
        print("\ncode header:", CODE_HEADER)
        for r in codes[:3]:
            print("code row   :", [str(c)[:60] for c in r])
        return

    if STATE_FILE.exists() and not args.force:
        print(f"\nA corrections sheet id is already recorded in {STATE_FILE}:")
        print("  " + STATE_FILE.read_text().strip())
        print("Re-run with --force to create another copy.")
        sys.exit(2)

    svc = service()

    created = svc.spreadsheets().create(body={
        "properties": {"title": TITLE,
                       "locale": "en_US",
                       "timeZone": "Asia/Manila"},
        "sheets": [
            {"properties": {"title": "Corrections",
                            "gridProperties": {"rowCount": len(reviews) + 1,
                                               "columnCount": len(CORR_HEADER)}}},
            {"properties": {"title": "Code decisions",
                            "gridProperties": {"rowCount": len(codes) + 1,
                                               "columnCount": len(CODE_HEADER)}}},
            {"properties": {"title": "Weekly check",
                            "gridProperties": {"rowCount": 46, "columnCount": 10}}},
            {"properties": {"title": "Bulk apply",
                            "gridProperties": {"rowCount": BULK_ROWS + 1,
                                               "columnCount": len(BULK_HEADER)}}},
            {"properties": {"title": "Codes",
                            "gridProperties": {"rowCount": len(vocab) + 2, "columnCount": 1}}},
        ],
    }, fields="spreadsheetId,spreadsheetUrl,sheets.properties").execute()

    sid = created["spreadsheetId"]
    url = created["spreadsheetUrl"]
    ids = {s["properties"]["title"]: s["properties"]["sheetId"]
           for s in created["sheets"]}
    print("\nspreadsheetId :", sid)
    print("sheetId map   :", ids)

    # data first (RAW so skus/ids/text are not reinterpreted by Sheets)
    svc.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={
        "valueInputOption": "RAW",
        "data": [
            {"range": a1("Corrections", "A1"),
             "values": [CORR_HEADER] + reviews},
            {"range": a1("Code decisions", "A1"),
             "values": [CODE_HEADER] + codes},
            {"range": a1("Weekly check", "A1"),
             "values": [["sample_week", "review_id", "date", "sku", "product", "review_text",
                         "ai_complaint", "final_complaint", "your_call", "notes"]]},
            {"range": a1("Bulk apply", "A1"),
             "values": [BULK_HEADER] + [["", "", "queue", "", ""] for _ in range(BULK_ROWS)]},
            {"range": a1("Codes", "A1"),
             "values": [["Code"], ["NOT A COMPLAINT"]] + [[c] for c in vocab]},
        ],
    }).execute()
    print("values written")

    try:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": build_requests(ids["Corrections"],
                                             ids["Code decisions"],
                                             len(reviews), len(codes),
                                             sid_vocab=ids.get("Codes"), n_vocab=len(vocab),
                                             sid_bulk=ids.get("Bulk apply"))}
        ).execute()
        print("formatting + validation applied")
    except Exception as e:                                  # noqa: BLE001
        print(f"WARN: formatting/validation partially skipped: {e}", file=sys.stderr)

    STATE_FILE.write_text(sid + "\n")
    print(f"state written : {STATE_FILE}")
    print(f"URL           : {url}")
    print("\nNOTE: sheet is private to the creating account — not shared.")


if __name__ == "__main__":
    main()
