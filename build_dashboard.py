#!/usr/bin/env python3
"""
Build the review dashboard as a static site, structured like the existing
AmzManager "Complaints validation" screen so it reads as familiar to the team:

  left sidebar (CX group) · title · metric cards · master table with per-column
  filters · expanding detail panel with the complaint checklist grouped by
  category · Previous/Next footer.

Labels: every figure and chart uses the RESOLVED label (final_complaint /
final_category — the team's correction when there is one, the AI's label
otherwise). The AI's own label stays in the payload and the table tooltip as
ai_complaint for audit. Decisions are made in the review app, which writes to
automation_philip; this page only reads.

Read-only by design: a static page cannot write to MySQL, so nothing on it may
look like an input. The complaint list in the detail panel used to filter the
table when clicked — the same gesture that *edits* in the app — and that is gone.

Honesty: the page carries the last successful refresh from the `runs` table and
turns red past 24h. A static mirror that cannot say how old it is invites people
to quote week-old numbers.

The "decide in the review app" button points at CX_APP_URL. When that is unset
the page says the app is not available at this address yet instead of linking to
a URL that may be a placeholder — a dead CTA is worse than no CTA.

Output: site/index.html + site/data/reviews.json
"""
import json, os, re
from collections import Counter
from datetime import date

import pymysql

import refresh_status

SITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site")
DATA = os.path.join(SITE, "data")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _app_url():
    """Where a decision can actually be made, or "" when there is nowhere yet.

    This used to default to https://philip.amzmanager.net, which at the time
    served the nginx placeholder: the mirror handed out a link that looked like
    the review app and was not. Unset now means "say so", never "invent a URL".
    """
    return (os.environ.get("CX_APP_URL") or "").strip().rstrip("/")


def _app_cta(label="Decide in the review app ↗"):
    """The header button, or an explicit note where the button would be."""
    url = _app_url()
    if not url:
        return ('<span class="noapp" title="CX_APP_URL is not set for this build">'
                'the review app is not available at this address yet</span>')
    return (f'<a href="{url}" target="_blank" rel="noopener" style="text-decoration:none">'
            f'<button class="primary">{label}</button></a>')


def _banner(st, run_errors=None):
    """refresh_status returns ok/bad; a fresh-but-FAILED run is neither.

    Green has to mean "the last run finished cleanly". Amber is "the numbers are
    recent, but the latest attempt failed" — the figures are from the last good
    run, which is not the same thing as healthy.
    """
    html, level = refresh_status.banner_html(st, run_errors=run_errors)
    if level == "ok" and st.get("failed"):
        html = html.replace('class="syncbar ok"', 'class="syncbar warn"')
        level = "warn"
    return html, level


def _sheet_url():
    """Link straight to the corrections sheet. The page cannot write, so the one
    action it must offer is a way to get to the place where she can."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".corrections_sheet_id")
    try:
        sid = open(p).read().strip()
    except OSError:
        return "#"
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit" if sid else "#"


def coverage_text(months):
    """Say out loud what the data covers.

    The corpus is 99.9% Jun-Oct 2026 with a handful of stragglers back to 2023.
    A chart axis that starts in 2023 reads as "the data is broken"; a stated
    window reads as "this is what we have".
    """
    if not months:
        return ""
    total = sum(months.values())
    ordered = sorted(months)
    # smallest contiguous run of months holding >= 99.5% of rows
    best = (0, len(ordered) - 1)
    acc, start = 0, 0
    for i, m in enumerate(ordered):
        acc += months[m]
        while acc - months[ordered[start]] >= total * 0.995:
            acc -= months[ordered[start]]
            start += 1
        if acc >= total * 0.995 and (i - start) < (best[1] - best[0]):
            best = (start, i)
    a, b = ordered[best[0]], ordered[best[1]]
    inside = sum(months[m] for m in ordered[best[0]:best[1] + 1])
    outside = total - inside
    fmt = lambda m: f"{MONTHS[int(m[5:7]) - 1]} {m[:4]}"
    txt = f"data covers {fmt(a)} – {fmt(b)}"
    if outside:
        txt += f" ({inside / total:.1%} of rows; {outside:,} older rows outside)"
    return txt


def main():
    conn = pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()

    cur.execute("""SELECT review_id, review_date, sku, parent, product, asin, order_id,
                          country, source, ai_complaint, ai_category,
                          final_complaint, final_category, second_complaint,
                          needs_review, is_new_code, has_prose, product_family,
                          dup_rank, dup_group,
                          LEFT(COALESCE(review_text,''), 400) AS text,
                          human_decision, human_complaint
                   FROM v_ai_complaints
                   ORDER BY review_date DESC, review_id DESC""")
    rows = cur.fetchall()
    print(f"rows: {len(rows)}")

    # Distribution and the detail panel's checklist use the RESOLVED label, so the
    # counts she reads match the number she would compute by hand from the sheet.
    # 'Review Deletion' is a rule verdict on empty or non-complaint text, not a
    # complaint: it stays in the rows (and the CSV) but never in the distribution.
    # It is NOT the same number as dup_rank (repeats of one comment on one order).
    cur.execute("""SELECT final_complaint complaint, final_category category, COUNT(*) n
                   FROM v_ai_complaints WHERE final_complaint IS NOT NULL
                     AND final_complaint <> 'Review Deletion'
                   GROUP BY final_complaint, final_category ORDER BY n DESC""")
    dist = cur.fetchall()

    cur.execute("SELECT COUNT(*) c FROM proposed_codes WHERE status='pending'"); pend = cur.fetchone()["c"]
    # DISTINCT: a revised decision adds a row, and counting rows inflates the
    # number of reviews a human has actually decided.
    cur.execute("SELECT COUNT(DISTINCT review_id) c FROM corrections"); corr = cur.fetchone()["c"]
    cur.execute("SELECT MIN(review_date) a, MAX(review_date) b FROM classifications"); rng = cur.fetchone()
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE human_validated=1"); validated = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE human_decision='rejected'"); removed = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_rule_deletions"); rule_deleted = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE final_complaint IS NULL"); nofinal = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE has_prose=0"); no_prose = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE has_prose=0 AND needs_review=1 AND human_validated=0")
    queue_no_prose = cur.fetchone()["c"]

    months = Counter()
    for r in rows:
        if r["review_date"]:
            months[r["review_date"].strftime("%Y-%m")] += 1

    cats = {}
    for d in dist:
        cats.setdefault(d["category"] or "Uncategorised", []).append({"c": d["complaint"], "n": d["n"]})

    for r in rows:
        r["review_date"] = r["review_date"].isoformat() if r["review_date"] else ""

    os.makedirs(DATA, exist_ok=True)
    payload = {
        "generated": date.today().isoformat(),
        "range": [rng["a"].isoformat() if rng["a"] else "", rng["b"].isoformat() if rng["b"] else ""],
        "coverage": coverage_text(months),
        "kpi": {
            "total": len(rows),
            "ncx": sum(1 for r in rows if (r["source"] or "") == "ncx"),
            "fba": sum(1 for r in rows if (r["source"] or "") == "fba"),
            "needs_review": sum(1 for r in rows if r["needs_review"]),
            "new_code": sum(1 for r in rows if r["is_new_code"]),
            "pending_codes": pend,
            "corrections": corr,
            "validated": validated,
            "removed": removed,
            "rule_deleted": rule_deleted,
            "no_prose": no_prose,
            "queue_no_prose": queue_no_prose,
            "distinct": len(dist),
        },
        "cats": cats,
        "rows": [{
            "id": r["review_id"], "d": r["review_date"],
            "mon": r["review_date"][:7] if r["review_date"] else "",
            "s": r["sku"] or "", "p": r["parent"] or "", "pr": r["product"] or "",
            "fam": r["product_family"] or "",
            "a": r["asin"] or "", "o": r["order_id"] or "", "ct": r["country"] or "",
            "src": r["source"] or "",
            "c": r["ai_complaint"] or "", "cat": r["ai_category"] or "",
            "fc": r["final_complaint"] or "", "fcat": r["final_category"] or "",
            "c2": r["second_complaint"] or "",
            "nr": int(r["needs_review"] or 0), "nc": int(r["is_new_code"] or 0),
            "dec": r["human_decision"] or "", "hc": r["human_complaint"] or "",
            "t": r["text"] or "", "hp": int(r["has_prose"] or 0),
            # F6: the mirror's payload carried order_id but no dup_rank, so a row
            # that repeats a comment on the same order was invisible here.
            "dr": int(r["dup_rank"] or 1), "dg": int(r["dup_group"] or 1),
        } for r in rows],
    }
    path = os.path.join(DATA, "reviews.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"data/reviews.json: {os.path.getsize(path)/1e6:.1f} MB")

    st = refresh_status.status(cur)
    banner, level = _banner(st, run_errors=os.environ.get("CX_REFRESH_ERRORS") or None)
    print(f"refresh: last successful {st['last_ok'] or 'never'}"
          f"{' (STALE)' if not st['ok'] else ''}; banner={level}")
    payload["refresh"] = {"last_ok": st["last_ok"].isoformat(sep=" ") if st["last_ok"] else None,
                          "stale": not st["ok"], "note": st["note"]}

    html = (INDEX
            .replace("__KPI__", json.dumps(payload["kpi"]))
            .replace("__CATS__", json.dumps(cats))
            .replace("__BANNER__", banner)
            .replace("__APPCTA__", _app_cta())
            .replace("__APPJSON__", json.dumps(_app_url()))
            .replace("__SHEET__", _sheet_url())
            .replace("__GEN__", payload["generated"])
            .replace("__COVERAGE__", payload["coverage"])
            .replace("__RANGE__", f'{payload["range"][0]} - {payload["range"][1]}'))
    # A placeholder left in the page is a link to nowhere or a blank KPI; fail the
    # build instead of shipping it.
    left = re.findall(r"__[A-Z][A-Z0-9_]*__", html)
    if left:
        raise SystemExit(f"unsubstituted placeholder(s) in index.html: {sorted(set(left))}")
    with open(os.path.join(SITE, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"index.html: {len(html)} bytes")
    conn.close()


INDEX = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Complaints validation · UEP</title>
<style>
:root{--bg:#f8f9fa;--card:#fff;--ink:#111827;--mut:#6b7280;--line:#e5e7eb;--accent:#2563eb;--warn:#b45309;--bad:#b42318;--ok:#067647;--pink:#fdf2f8;--pinkb:#fbcfe8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}
.wrap{display:flex;min-height:100vh}
aside{width:218px;flex:0 0 218px;background:var(--card);border-right:1px solid var(--line);padding:16px 0;position:sticky;top:0;height:100vh;overflow:auto}
.brand{font-weight:750;font-size:17px;letter-spacing:-.02em;padding:0 18px 16px;border-bottom:1px solid var(--line);margin-bottom:10px}
.brand span{display:block;font-size:10.5px;font-weight:600;color:var(--mut);letter-spacing:.08em;text-transform:uppercase;margin-top:2px}
.grp{padding:10px 18px 4px;font-size:10.5px;font-weight:700;color:#9ca3af;letter-spacing:.08em;text-transform:uppercase}
.nav{display:block;padding:7px 18px;color:#374151;font-size:13.5px;border-left:2px solid transparent}
.nav:hover{background:#f9fafb}
.nav.on{color:var(--accent);font-weight:650;background:#eff6ff;border-left-color:var(--accent)}
.nav.off{color:#b6bcc6;cursor:default}
main{flex:1;min-width:0;padding:20px 24px 60px}
header.top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px}
h1{font-size:21px;margin:0;letter-spacing:-.02em}
.sub{color:var(--mut);font-size:12.5px;margin-top:3px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.legend{font-size:12px;color:var(--mut);line-height:1.7;margin:-4px 0 16px;max-width:980px}
.legend b{color:#374151;font-weight:650}
.cover{font-size:12px;color:#374151;background:#f3f6fb;border:1px solid #dbe6f7;border-radius:7px;padding:7px 11px;margin:0 0 12px;display:inline-block}
@media(max-width:1100px){.cards{grid-template-columns:repeat(3,1fr)}}
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:12px 14px}
.card .n{font-size:23px;font-weight:680;letter-spacing:-.02em;line-height:1.15}
.card .l{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-top:3px}
.card .s{color:var(--mut);font-size:12px;margin-top:4px}
.card.hi{border-color:#bfdbfe;background:#f8fbff}
.panel{background:var(--card);border:1px solid var(--line);border-radius:9px;overflow:hidden}
.tblwrap{overflow:auto;max-height:calc(100vh - 250px);padding-bottom:10px}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:12.5px;white-space:nowrap}
th{position:sticky;top:0;z-index:2;background:#fafafa;text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:700;padding:8px 10px;border-bottom:1px solid var(--line)}
.filterrow th{position:sticky;top:31px;z-index:2;background:#fff;padding:4px 6px;border-bottom:1px solid var(--line);font-weight:400;text-transform:none;letter-spacing:0}
.filterrow input,.filterrow select{width:100%;min-width:62px;padding:4px 6px;border:1px solid var(--line);border-radius:5px;font:inherit;font-size:12px;background:#fff}
.filterrow .dwrap{display:flex;gap:4px}
.filterrow .dwrap input{min-width:74px}
td{padding:8px 10px;border-bottom:1px solid #f3f4f6;vertical-align:top}
tbody tr{cursor:pointer}
tbody tr:hover td{background:#f9fbff}
tbody tr.sel td{background:#eff6ff}
tbody tr.sel td:first-child{box-shadow:inset 2px 0 0 var(--accent)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:#374151}
/* the review column is the one thing a human actually reads — let it wrap */
td.txt{white-space:normal;min-width:340px;max-width:560px;line-height:1.45;color:#374151;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:10.5px;font-weight:700;border:1px solid var(--line);background:#f6f6fa;color:var(--mut)}
/* pink is reserved for things that were REMOVED or need a decision; 'automatic'
   is the healthy default and must not look like an alert. */
.tag.auto{background:#f3f4f6;border-color:#e5e7eb;color:#4b5563}
.tag.blank{background:var(--pink);border-color:var(--pinkb);color:#9d174d}
.tag.rev{background:#fef3f2;border-color:#fecdca;color:var(--bad)}
.tag.new{background:#fff7ed;border-color:#fed7aa;color:var(--warn)}
.tag.ok{background:#ecfdf3;border-color:#abefc6;color:var(--ok)}
.foot{display:flex;align-items:center;gap:10px;padding:10px 12px;border-top:1px solid var(--line);background:#fafafa;font-size:12.5px;color:var(--mut)}
button{padding:6px 13px;border-radius:6px;border:1px solid var(--line);background:#fff;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button:disabled{opacity:.45;cursor:default}
.detail{display:none;margin-top:16px}
.detail.open{display:block}
.dgrid{display:grid;grid-template-columns:250px 1fr 320px;gap:18px;padding:16px}
@media(max-width:1150px){.dgrid{grid-template-columns:1fr}}
.dbox{background:#fff;border:1px solid #e8e8ee;border-radius:8px;padding:12px}
.dbox h3{margin:0 0 10px;font-size:13px;letter-spacing:-.01em}
.kv{display:grid;grid-template-columns:74px 1fr;gap:6px 8px;font-size:12.5px}
.kv .k{color:var(--mut);font-weight:600}
.kv .v{color:#111;word-break:break-word}
.comment{white-space:pre-wrap;font-size:13px;line-height:1.6;margin:0}
.cats{max-height:340px;overflow:auto;padding-right:4px}
.cat{margin-bottom:12px}
.cat h4{margin:0 0 6px;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);font-weight:700}
.cat h4 b{color:#9ca3af;font-weight:600}
/* Reference rows, NOT options: no pointer, no hover, no click. The old list
   filtered the table on click, which is the gesture that edits in the app. */
.opt{display:flex;align-items:center;gap:7px;padding:3px 0;font-size:12.5px;color:#374151;cursor:default}
.opt .n{margin-left:auto;color:#9ca3af;font-size:11.5px}
.opt.on{color:var(--accent);font-weight:650}
.dot{width:11px;height:11px;border-radius:3px;border:1.5px solid #cbd5e1;flex:0 0 auto}
.opt.on .dot{background:var(--accent);border-color:var(--accent)}
.note{font-size:11.5px;color:var(--mut);margin-top:10px;line-height:1.5}
#dq{width:100%;padding:5px 8px;border:1px solid var(--line);border-radius:5px;font:inherit;font-size:12px;margin-bottom:8px}
.badge{display:inline-block;font-size:10.5px;font-weight:700;color:var(--ok);background:#ecfdf3;border:1px solid #abefc6;border-radius:999px;padding:1px 7px;margin-left:6px}
.syncbar{padding:8px 12px;border-radius:7px;font-size:12.5px;margin-bottom:14px;border:1px solid;line-height:1.5}
.syncbar.ok{background:#f0fdf4;border-color:#bbf7d0;color:#166534}
.syncbar.warn{background:#fffbeb;border-color:#fde68a;color:#92400e}
.syncbar.bad{background:#fef2f2;border-color:#fecaca;color:#b42318}
.noapp{display:inline-block;max-width:250px;text-align:right;font-weight:600;color:#92400e;font-size:12px;line-height:1.35}
.chip{display:inline-block;padding:0 6px;border-radius:4px;font-size:10.5px;font-weight:650;background:#eef2ff;border:1px solid #c7d2fe;color:#3730a3;margin:0 3px 2px 0;white-space:nowrap}
.chip.q{background:#f4f4f5;border-color:#e4e4e7;color:#52525b}
td.txt .prose{display:block;margin-top:2px;color:#374151}
.ro{font-size:11px;color:var(--mut);font-weight:650;text-transform:uppercase;letter-spacing:.05em}
.dtext .chip{margin-bottom:4px}
.dtext p{margin:8px 0 0;font-size:13px;line-height:1.6;white-space:pre-wrap}
.dnone{color:var(--mut);font-style:italic;font-size:12.5px;margin-top:6px}
</style></head><body>
<div class="wrap">
<aside>
  <div class="brand">UEP<span>Complaint validation</span></div>
  <a class="nav on" href="./index.html">Reviews</a>
  <a class="nav" href="./codes.html">New codes</a>
  <a class="nav" href="./quality.html">Quality</a>
</aside>
<main>
  <header class="top">
    <div><h1>Complaints validation</h1>
      <div class="sub">Complaint codes → corrected by the team → one validated label per review · generated __GEN__</div></div>
    <div style="display:flex;gap:8px;align-items:center">
      <span class="ro" title="this page cannot write to the database">Read-only</span>
      <button id="csv" class="primary">Export CSV</button>
      <button id="csvtxt" title="same rows plus the review text">Export with text</button>
      __APPCTA__
    </div>
  </header>
  __BANNER__
  <div class="cover">__COVERAGE__</div>
  <div class="cards" id="cards"></div>
  <div class="legend">
    <b>automatic</b> — the AI was confident and used a code you already have; nothing to do &nbsp;·&nbsp;
    <b>needs check</b> — it wasn't sure, so a person should confirm &nbsp;·&nbsp;
    <b>new code</b> — it used a complaint type that isn't in your list yet &nbsp;·&nbsp;
    <b>confirmed / corrected / removed</b> — someone has already decided. Figures and charts use the corrected label where one exists.<span id="ruledel"></span><br>
    <b>code only</b> — the row carries no customer comment (just the return-reason picklist), so there is nothing to check the label against.<span id="noprose"></span>
  </div>
  <div id="charts" style="margin-bottom:16px"></div>

  <div class="panel">
    <div class="tblwrap">
      <table>
        <thead>
          <tr>
            <th>Status</th><th>Date</th><th>Product</th><th>SKU</th><th>Source</th><th>Country</th><th>Complaint</th><th>Review</th>
          </tr>
          <tr class="filterrow">
            <th><select data-f="st"><option value="">All reviews</option><option value="nr">Needs a human check</option><option value="nc">Uses a new code</option><option value="codeonly">No comment (code only)</option><option value="ok">Handled automatically</option><option value="done">Checked by the team</option></select></th>
            <th><div class="dwrap"><input data-f="d" placeholder="YYYY-MM"><select data-f="win"><option value="">all</option><option value="3">3m</option><option value="6">6m</option><option value="12">12m</option></select></div></th>
            <th><input data-f="pr" placeholder="filter"></th>
            <th><input data-f="s" placeholder="filter"></th>
            <th><select data-f="src"><option value="">All</option></select></th>
            <th><select data-f="ct"><option value="">All</option></select></th>
            <th><select data-f="fc"><option value="">All</option></select></th>
            <th><input data-f="q" placeholder="search the review text"></th>
          </tr>
        </thead>
        <tbody id="tb"></tbody>
      </table>
    </div>
    <div class="foot">
      <button id="prev">Previous</button><button id="next">Next</button>
      <span id="pinfo"></span>
      <span style="margin-left:auto">Click a row to open the review</span>
    </div>
  </div>

  <div class="detail" id="detail">
    <div class="panel"><div class="dgrid">
      <div class="dbox"><h3 id="dt">Review</h3><div class="kv" id="dkv"></div></div>
      <div class="dbox"><h3>Customer comment</h3><div class="dtext" id="dtext"></div>
        <div class="note" id="da"></div>
        <div class="note" id="dapp"></div></div>
      <div class="dbox"><h3>Complaint codes <span class="ro">reference</span></h3>
        <input id="dq" placeholder="type to find a complaint…">
        <div class="cats" id="dcats"></div>
        <div class="note">Counts are validated labels (corrections applied); the highlighted row is this
        review's validated complaint. This list is read-only — nothing here changes a decision.</div>
      </div>
    </div></div>
  </div>
</main></div>
<script src="charts.js"></script>
<script>
const KPI=__KPI__, CATS=__CATS__, APP=__APPJSON__;
/* Display-only renames. The payload keeps the stored value so the CSV and any
   pivot still say 'Vague Complaint'; the screen says what it means. Review
   Deletion is a rule verdict on empty or non-complaint text, not a complaint —
   and it is a DIFFERENT thing from the repeat rows (dup_rank), so the two must
   never share the word 'duplicate' in copy. */
const RENAME={"Vague Complaint":"Unspecified"};
const NOISE="Review Deletion";
function nice(c){return RENAME[c]||c||""}
let ROWS=[], VIEW=[], page=0, PER=100, cur=null;

const cardDefs=[
  ["total","Reviews classified","all sources"],
  ["needs_review","Need a human check","the AI wasn't confident"],
  ["validated","Validated by the team","corrections applied to every figure"],
  ["pending_codes","New codes to decide","not in the current list"]
];
document.getElementById("cards").innerHTML=cardDefs.map(([k,l,s])=>
 `<div class="card ${k==='needs_review'?'hi':''}"><div class="n">${(KPI[k]||0).toLocaleString()}</div><div class="l">${l}</div><div class="s">${s}</div></div>`).join("");
if(KPI.no_prose){
  document.getElementById("noprose").innerHTML =
    ` &nbsp;·&nbsp; <b>${KPI.no_prose.toLocaleString()}</b> of ${KPI.total.toLocaleString()} reviews `+
    `(${Math.round(KPI.no_prose*100/KPI.total)}%) are code only, ${KPI.queue_no_prose.toLocaleString()} of them still `+
    `unreviewed — filter them out with the status list to work only what can be checked.`;
}
if(KPI.rule_deleted){
  document.getElementById("ruledel").innerHTML =
    ` &nbsp;·&nbsp; <b>process noise</b> — ${KPI.rule_deleted.toLocaleString()} records removed by the rule as empty or non-complaint text (not complaints; listed in <code>v_rule_deletions</code>)`;
}

function esc(s){return (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}

/* Review text is an Amazon return form: up to six pipe-separated segments, the
   first one or two being picklist values ('Changed Mind', 'My needs changed').
   Show the picklist as chips and the customer's words as text; a row with no
   prose is labelled, not silently rendered as one grey blob. The 25-character
   threshold is the same rule as has_prose in the database. */
function parts(t){
  t=(t==null?"":String(t)).trim();
  if(!t) return {chips:[],prose:[]};
  const segs=t.split("|").map(x=>x.trim()).filter(Boolean);
  if(segs.length===1) return segs[0].length>=8?{chips:[],prose:segs}:{chips:segs,prose:[]};
  const chips=[],prose=[],seen=new Set();
  segs.forEach(x=>{
    if(x.length>=25 && x.indexOf(" ")>=0){
      const k=x.toLowerCase(); if(!seen.has(k)){seen.add(k);prose.push(x)}
    } else chips.push(x);
  });
  return {chips:chips,prose:prose};
}
function chipHtml(chips,limit){
  if(!chips||!chips.length) return "";
  const shown=limit?chips.slice(0,limit):chips;
  let h=shown.map(c=>`<span class="chip">${esc(c)}</span>`).join("");
  if(limit&&chips.length>shown.length) h+=`<span class="chip q">+${chips.length-shown.length}</span>`;
  return h;
}
function txtCell(r){
  const p=parts(r.t);
  const head=chipHtml(p.chips,2);
  if(!p.prose.length)
    return head+`<span class="prose" style="color:#9d174d">code only — nothing to check</span>`;
  const s=p.prose.join(" ");
  return head+`<span class="prose">${esc(s.slice(0,150))}${s.length>150?"…":""}</span>`;
}
function monthNum(m){return m?parseInt(m.slice(0,4),10)*12+parseInt(m.slice(5,7),10):0}
function catsOf(rows){
  // Recompute the category groups from the rows on screen so the detail panel
  // and the charts agree with the table after a filter. Process noise is a rule
  // verdict, so it is not a complaint group.
  const out={};
  rows.forEach(r=>{if(r.fc===NOISE)return;const k=r.fcat||"Uncategorised";const c=r.fc||"(blank)";
    let g=out[k]||(out[k]=[]);let o=g.find(x=>x.c===c);
    if(o)o.n++;else g.push({c:c,n:1});});
  Object.values(out).forEach(g=>g.sort((a,b)=>b.n-a.n));
  const sorted={};Object.keys(out).sort((a,b)=>out[b].reduce((s,x)=>s+x.n,0)-out[a].reduce((s,x)=>s+x.n,0)).forEach(k=>sorted[k]=out[k]);
  return sorted;
}

function chartRows(){
  // The charts are a reporting distribution: process noise out, display rename in.
  // Only the renamed rows are copied — the 22k rows are not cloned per keystroke.
  return VIEW.filter(r=>r.fc!==NOISE)
             .map(r=>RENAME[r.fc]?Object.assign({},r,{fc:nice(r.fc)}):r);
}

fetch("data/reviews.json").then(r=>r.json()).then(d=>{
  ROWS=d.rows; VIEW=ROWS;
  const ct=new Set(), src=new Set();
  ROWS.forEach(r=>{if(r.ct)ct.add(r.ct); if(r.src)src.add(r.src)});
  const ctSel=document.querySelector('[data-f="ct"]'), srcSel=document.querySelector('[data-f="src"]'), fcSel=document.querySelector('[data-f="fc"]');
  if(ctSel) [...ct].sort().forEach(v=>ctSel.insertAdjacentHTML("beforeend",`<option value="${esc(v)}">${esc(v)}</option>`));
  if(srcSel) [...src].sort().forEach(v=>srcSel.insertAdjacentHTML("beforeend",`<option value="${esc(v)}">${esc(v)}</option>`));
  if(fcSel) Object.values(CATS).flat().forEach(o=>fcSel.insertAdjacentHTML("beforeend",`<option value="${esc(o.c)}">${esc(o.c)}</option>`));
  window.__d=d;
  render();
  drawCharts();
});

function drawCharts(){
  // Charts follow the current filter, so "FBA vs NCX" and "this SKU" are one
  // click away instead of a separate dashboard.
  try{
    if(window.renderCharts) window.renderCharts(document.getElementById("charts"),
      {kpi:KPI, cats:catsOf(VIEW), rows:chartRows()});
  }catch(e){console.error("charts failed:",e)}
}

function status(r){
  // A human decision outranks the AI's state: once someone has looked at it, that
  // is the truth, and the row must stop showing as outstanding work.
  if(r.dec==="accepted") return '<span class="tag ok">confirmed</span>';
  if(r.dec==="corrected") return '<span class="tag ok">corrected</span>';
  if(r.dec==="rejected") return '<span class="tag blank">removed</span>';
  if(r.nc) return '<span class="tag new">new code</span>';
  if(r.nr) return '<span class="tag rev">needs check</span>';
  return '<span class="tag auto">automatic</span>';
}

function render(){
  const slice=VIEW.slice(page*PER,(page+1)*PER);
  document.getElementById("tb").innerHTML=slice.map(r=>`<tr data-id="${r.id}" class="${cur===r.id?'sel':''}">
    <td>${status(r)}</td>
    <td class="mono">${esc(r.d)}</td>
    <td>${esc(r.pr)}</td>
    <td>${esc(r.s)}</td>
    <td>${esc(r.src)||"—"}</td>
    <td>${esc(r.ct)||"—"}</td>
    <td>${esc(nice(r.fc))||"—"}${r.hc&&r.hc!==r.c?'<span title="corrected from '+esc(r.c)+'">*</span>':''}</td>
    <td class="txt">${txtCell(r)}</td></tr>`).join("")
    || '<tr><td colspan="8" style="padding:34px;text-align:center;color:#6b7280">Nothing matches those filters.</td></tr>';
  document.getElementById("pinfo").textContent=`${VIEW.length.toLocaleString()} rows · page ${page+1} of ${Math.max(1,Math.ceil(VIEW.length/PER))}`;
  document.getElementById("prev").disabled=page===0;
  document.getElementById("next").disabled=(page+1)*PER>=VIEW.length;
  document.querySelectorAll("#tb tr[data-id]").forEach(tr=>tr.onclick=()=>openRow(tr.dataset.id));
}

function complaintOptions(){
  const box=document.getElementById("dcats");
  const q=(document.getElementById("dq").value||"").toLowerCase();
  const groups=catsOf(VIEW);
  box.innerHTML=Object.entries(groups).map(([cat,opts])=>{
    const shown=opts.filter(o=>!q||o.c.toLowerCase().includes(q)||nice(o.c).toLowerCase().includes(q)).slice(0,20);
    if(!shown.length) return "";
    const total=opts.reduce((a,b)=>a+b.n,0);
    const rows=shown.map(o=>`<div class="opt ${cur&&o.c===cur.fc?'on':''}">
      <span class="dot"></span><span>${esc(nice(o.c))}</span><span class="n">${o.n.toLocaleString()}</span></div>`).join("");
    return `<div class="cat"><h4>${esc(cat)} <b>(${total.toLocaleString()})</b></h4>${rows}</div>`;
  }).join("")||'<div class="note">No complaint matches that search.</div>';
  /* No click handler on purpose. Clicking a complaint here used to filter the
     table, while the same gesture swtiched a label in the app; the list is a
     reference, so it must not respond to a click at all. */
}

function openRow(id){
  const r=ROWS.find(x=>String(x.id)===String(id)); if(!r) return;
  cur=r;
  document.querySelectorAll("#tb tr").forEach(tr=>tr.classList.toggle("sel",tr.dataset.id===String(id)));
  document.getElementById("detail").classList.add("open");
  document.getElementById("dt").innerHTML=`${esc(r.s||r.pr||"Review")} ${status(r)}`
    +(r.hp?"":' <span class="tag blank">code only</span>');
  document.getElementById("dkv").innerHTML=[
    ["ASIN",r.a],["Review ID",r.id],["Date",r.d],["SKU",r.s],["Product",r.pr],["Family",r.fam],["Parent",r.p],
    ["Country",r.ct],["Source",r.src],["Order ID",r.o],["AI code",nice(r.c)],["AI category",r.cat],
    ["Validated",nice(r.fc)],["Validated category",r.fcat],["Second choice",nice(r.c2)]
  ].map(([k,v])=>`<div class="k">${k}</div><div class="v">${esc(v)||"—"}</div>`).join("");
  const p=parts(r.t);
  document.getElementById("dtext").innerHTML =
    (p.chips.length?`<div>${chipHtml(p.chips)}</div>`:"") +
    (p.prose.length?`<p>${esc(p.prose.join(" "))}</p>`:"") +
    (!p.prose.length?`<div class="dnone">${p.chips.length?"No customer comment — the return reason above is the only text on this row.":"No review text on this row."}</div>`:"");
  document.getElementById("dapp").innerHTML = APP
    ? `Read-only here. <a href="${APP}/review/${r.id}" target="_blank" rel="noopener">Open this review in the app to decide it ↗</a>`
    : `Read-only here. The review app is not available at this address yet, so this row cannot be decided from here.`;
  document.getElementById("da").innerHTML = r.hc
    ? `Validated by the team: <b>${esc(nice(r.fc))}</b> <span class="badge">corrected</span> — the AI had said ${esc(nice(r.c))}.`
    : (r.dec==="rejected" ? `Removed by the team: not a complaint. The AI had said ${esc(nice(r.c))}.`
       : (r.dec==="accepted" ? `Confirmed by the team as <b>${esc(nice(r.c))}</b>.`
          : (r.c?`AI assigned: ${esc(nice(r.c))} — not checked by a person yet.`:"No complaint could be assigned.")));
  document.getElementById("dq").value="";
  complaintOptions();
  const on=document.querySelector("#dcats .opt.on"), box=document.getElementById("dcats");
  if(on && box) box.scrollTop = Math.max(0, on.offsetTop - box.offsetTop - 40);
  document.getElementById("detail").scrollIntoView({behavior:"smooth", block:"start"});
}

function apply(){
  // Keep the raw select values: exact-match filters compare against data that is
  // mixed case ('Noisy'), so lowercasing the filter made every complaint filter
  // return nothing. Substring filters lowercase both sides explicitly.
  const raw={}; document.querySelectorAll("[data-f]").forEach(el=>raw[el.dataset.f]=el.value.trim());
  const low=k=>(raw[k]||"").toLowerCase();
  let cut=0;
  if(raw.win){
    const maxm=ROWS.reduce((m,r)=>Math.max(m,monthNum(r.mon)),0);
    cut=maxm-(parseInt(raw.win,10)-1);
  }
  VIEW=ROWS.filter(r=>{
    if(low("pr") && !(r.pr||"").toLowerCase().includes(low("pr"))) return false;
    if(low("p") && !(r.p||"").toLowerCase().includes(low("p"))) return false;
    if(low("s") && !(r.s||"").toLowerCase().includes(low("s"))) return false;
    if(low("a") && !(r.a||"").toLowerCase().includes(low("a"))) return false;
    if(low("id") && !String(r.id).includes(low("id"))) return false;
    if(low("o") && !(r.o||"").toLowerCase().includes(low("o"))) return false;
    if(low("cat") && !(r.cat||"").toLowerCase().includes(low("cat"))) return false;
    if(low("d") && !(r.d||"").toLowerCase().includes(low("d"))) return false;
    if(cut && monthNum(r.mon)<cut) return false;
    if(raw.ct && (r.ct||"")!==raw.ct) return false;
    if(raw.src && (r.src||"")!==raw.src) return false;
    if(raw.fc && (r.fc||"")!==raw.fc) return false;
    if(low("q") && !(r.t||"").toLowerCase().includes(low("q"))) return false;
    if(raw.st==="codeonly" && r.hp) return false;
    if(raw.st==="nr" && !r.nr) return false;
    if(raw.st==="nc" && !r.nc) return false;
    if(raw.st==="ok" && (r.nr||r.nc||r.dec)) return false;
    if(raw.st==="done" && !r.dec) return false;
    return true;
  });
  page=0; render();
  if(cur) complaintOptions();
  drawCharts();
}
document.querySelectorAll("[data-f]").forEach(el=>{
  el.addEventListener(el.tagName==="SELECT"?"change":"input",()=>{clearTimeout(window._t);window._t=setTimeout(apply,180)});
});
document.getElementById("dq").addEventListener("input",()=>{clearTimeout(window._tq);window._tq=setTimeout(complaintOptions,120)});
document.getElementById("prev").onclick=()=>{if(page>0){page--;render()}};
document.getElementById("next").onclick=()=>{if((page+1)*PER<VIEW.length){page++;render()}};

function exportRows(withText){
  // Export what is on screen, at the grain a pivot needs: one month column, the
  // source and the resolved category are all present, so no second join is needed.
  const head=["review_id","review_date","month","product_family","sku","parent","asin","product","source","country",
              "ai_complaint","ai_category","final_complaint","complaint_display","final_category","human_decision","has_prose"];
  if(withText) head.push("review_text");
  const q=v=>'"'+String(v==null?"":v).replace(/"/g,'""')+'"';
  const csv=[head.join(",")].concat(VIEW.map(r=>{
    // final_complaint stays the STORED code; complaint_display carries the
    // readable label. Same two columns as the app's export, so the mirror's file
    // joins to it instead of using a vocabulary of its own.
    const a=[r.id,r.d,r.mon,r.fam,r.s,r.p,r.a,r.pr,r.src,r.ct,r.c,r.cat,r.fc,nice(r.fc),r.fcat,r.dec,r.hp];
    if(withText) a.push(r.t);
    return a.map(q).join(",");
  })).join("\n");
  const url=URL.createObjectURL(new Blob(["\ufeff"+csv],{type:"text/csv;charset=utf-8"}));
  const a=document.createElement("a"); a.href=url;
  a.download="complaints-"+(withText?"with-text-":"")+new Date().toISOString().slice(0,10)+".csv"; a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
document.getElementById("csv").onclick=()=>exportRows(false);
document.getElementById("csvtxt").onclick=()=>exportRows(true);
</script></body></html>"""


if __name__ == "__main__":
    main()
