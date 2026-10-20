#!/usr/bin/env python3
"""
Build the review dashboard as a static site with ONE calm hierarchy:

  header (title · date range · one filter row) · four cards · two panels side by
  side (what people complained about | over time) · one rate strip · the full
  table · one collapsed "About this data" disclosure at the bottom.

The page used to lead with a legend, a toolbar, a 14-bar category chart, a source
donut, the complaint mix and a line chart, then three paragraphs of data-mechanics
prose above the table. A CX director read it as a log file. Nothing was deleted:
the arithmetic, the feed caveat, the picklist-only note, the record-rate caveat
and the validation status moved into the disclosure, closed by default.

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
import csv

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


def _fmt_day(iso):
    """'2026-07-16' -> '16 Jul 2026'. The page speaks dates, not ISO strings."""
    if not iso:
        return ""
    return f"{int(iso[8:10])} {MONTHS[int(iso[5:7]) - 1]} {iso[:4]}"


def _range_text(a, b):
    if not a or not b:
        return "no dated rows"
    return f"{_fmt_day(a)} – {_fmt_day(b)}"


CATEGORIES_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cx_categories.csv")


def canonical_categories(dist, rows, cur):
    """One spelling per category, whatever casing the import path used.

    'Not as Described' and 'Not As Described' reached the database as two strings
    because two writers (the classifier's menu and the corrections import) spell
    the category differently. MySQL's case-insensitive collation hides that in a
    GROUP BY — it returns ONE arbitrary spelling per complaint code — so the page
    showed the same category twice, under two casings, as two separate rows.

    Resolution order: the spelling in data/cx_categories.csv (the taxonomy she
    maintains), then the most common spelling in complaint_codes, then the most
    common spelling in the rows themselves. Every comparison is case-insensitive.

    Returns {lower: canonical} for the assertion at the end of the build.
    """
    counts = {}          # lower -> {spelling: weight}
    csv_names = {}
    try:
        with open(CATEGORIES_CSV, encoding="utf-8-sig") as f:
            for rec in csv.DictReader(f):
                name = (rec.get("name") or "").strip()
                if name:
                    csv_names[name.lower()] = name
    except OSError:
        pass

    cur.execute("""SELECT category, COUNT(*) n FROM complaint_codes
                    WHERE category IS NOT NULL GROUP BY category, HEX(category)""")
    for rec in cur.fetchall():
        name = (rec["category"] or "").strip()
        if name:
            counts.setdefault(name.lower(), {})[name] = counts.setdefault(
                name.lower(), {}).get(name, 0) + 10 * int(rec["n"] or 1)
    for rec in dist:
        name = (rec["category"] or "").strip()
        if name:
            counts.setdefault(name.lower(), {})[name] = counts.setdefault(
                name.lower(), {}).get(name, 0) + int(rec["n"] or 0)
    # The AI's own category rides in the payload too (the detail panel shows it),
    # and it carries the pseudo-categories NEW / NO_TEXT / ERR that final_category
    # never holds — so the vocabulary is the union of both columns.
    for r in rows:
        for col in ("final_category", "ai_category"):
            name = (r[col] or "").strip()
            if name:
                counts.setdefault(name.lower(), {})[name] = counts.setdefault(
                    name.lower(), {}).get(name, 0) + 1

    canon, merged = {}, []
    for key, variants in counts.items():
        if key in csv_names:
            pick = csv_names[key]
        else:
            pick = sorted(variants.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        canon[key] = pick
        for v in variants:
            if v != pick:
                merged.append((v, pick))

    for rec in dist:
        if rec["category"]:
            rec["category"] = canon[rec["category"].strip().lower()]
    for r in rows:
        for col in ("final_category", "ai_category"):
            if r[col]:
                r[col] = canon[r[col].strip().lower()]

    # Hard gate: after canonicalisation a category has exactly ONE spelling. A
    # second spelling reappearing (a new import path, a hand-edited sheet) must
    # fail the build, not quietly split the mix again.
    seen = {}
    for rec in dist:
        if rec["category"]:
            seen.setdefault(rec["category"].strip().lower(), set()).add(rec["category"])
    for r in rows:
        for col in ("final_category", "ai_category"):
            if r[col]:
                seen.setdefault(r[col].strip().lower(), set()).add(r[col])
    split = {k: sorted(v) for k, v in seen.items() if len(v) > 1}
    if split:
        raise SystemExit(f"category casing split after canonicalisation: {split}")
    if merged:
        print("categories: merged " + "; ".join(f"'{a}' -> '{b}'" for a, b in sorted(set(merged))))
    return canon, [a for a, _ in merged]


def main():
    conn = pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()

    cur.execute("""SELECT review_id, review_date, sku, parent, product, asin, title, order_id,
                          country, source, ai_complaint, ai_category,
                          final_complaint, final_category, second_complaint,
                          needs_review, is_new_code, has_prose, product_family,
                          human_validated,
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
    # ORDER BY n DESC alone leaves ties to the engine, so two builds could order
    # equal-count codes differently and the page (and its hash) churned. The
    # category+code tie-break makes a build from the same data byte-identical.
    cur.execute("""SELECT final_complaint complaint, final_category category, COUNT(*) n
                   FROM v_ai_complaints WHERE final_complaint IS NOT NULL
                     AND final_complaint <> 'Review Deletion'
                   GROUP BY final_complaint, final_category
                   ORDER BY n DESC, category, complaint""")
    dist = cur.fetchall()

    # One spelling per category before anything counts or groups it. Without
    # this the mix shows 'Not as Described' twice (see canonical_categories).
    canon_cats, rejected_spellings = canonical_categories(dist, rows, cur)

    cur.execute("SELECT COUNT(*) c FROM proposed_codes WHERE status='pending'"); pend = cur.fetchone()["c"]
    # DISTINCT: a revised decision adds a row, and counting rows inflates the
    # number of reviews a human has actually decided.
    cur.execute("SELECT COUNT(DISTINCT review_id) c FROM corrections"); corr = cur.fetchone()["c"]
    cur.execute("SELECT MIN(review_date) a, MAX(review_date) b FROM classifications"); rng = cur.fetchone()
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE human_validated=1"); validated = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE human_decision='rejected'"); removed = cur.fetchone()["c"]
    # Who decided, and when. The view carries human_decision but not the person
    # behind it; corrections is the only table that has both, and the latest id
    # per review is the row the view reads as current. Empty map today means
    # every row is AI-classified, which is the truth and must render as such.
    cur.execute("""SELECT k.review_id, k.reviewed_by_name, k.created_at
                     FROM corrections k
                     JOIN (SELECT review_id, MAX(id) id FROM corrections GROUP BY review_id) l
                       ON l.id = k.id""")
    decided_by = {r["review_id"]: (r["reviewed_by_name"] or "",
                                   r["created_at"].strftime("%Y-%m-%d") if r["created_at"] else "")
                  for r in cur.fetchall()}
    cur.execute("SELECT COUNT(*) c FROM v_rule_deletions"); rule_deleted = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE final_complaint IS NULL"); nofinal = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE has_prose=0"); no_prose = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE has_prose=0 AND needs_review=1 AND human_validated=0")
    queue_no_prose = cur.fetchone()["c"]

    months = Counter()
    for r in rows:
        if r["review_date"]:
            months[r["review_date"].strftime("%Y-%m")] += 1

    # ---- the mix's inputs -------------------------------------------------
    # The app's family filter is a first-class dimension; the mirror had the
    # value on every row but no control for it. Read the distinct values, never
    # hardcode a list that drifts when the taxonomy is edited.
    cur.execute("""SELECT product_family f, COUNT(*) n FROM v_ai_complaints
                    WHERE product_family IS NOT NULL AND product_family <> ''
                    GROUP BY product_family ORDER BY product_family""")
    families = [r["f"] for r in cur.fetchall()]

    # feed_days is COUNT(DISTINCT review_date) over the WHOLE corpus (unfiltered),
    # exactly as app.feed_days does it: a day the feed carries with no record for
    # this selection is a genuine zero and must be divided by, and a day before
    # the feed existed is not data at all. The client can then compute the same
    # denominator without a second round trip.
    cur.execute("""SELECT DISTINCT review_date d FROM v_ai_complaints
                    WHERE review_date IS NOT NULL ORDER BY d""")
    feed_dates = [r["d"].isoformat() for r in cur.fetchall()]
    cur.execute("""SELECT IFNULL(source,'') s, MIN(review_date) lo, MAX(review_date) hi
                     FROM v_ai_complaints GROUP BY s""")
    source_max = {r["s"]: (r["hi"].isoformat() if r["hi"] else "") for r in cur.fetchall()}

    # Sales for every SKU the corpus knows, in the months the corpus covers: the
    # same rows v_complaint_rates divides by, at the grain the client needs to
    # rebuild the denominator after a family/product filter. The denominator's
    # scope is the PRODUCT filter only — never the complaint predicate, or the
    # rate divides by the orders that produced that one complaint (app R2).
    if months:
        cur.execute("""SELECT s.sku, s.month, s.orders, s.units
                         FROM sales_by_sku_month s
                        WHERE s.sku IN (SELECT DISTINCT sku FROM v_ai_complaints
                                         WHERE sku IS NOT NULL)
                          AND s.month BETWEEN %s AND %s""", (min(months), max(months)))
    else:
        cur.execute("SELECT sku, month, orders, units FROM sales_by_sku_month WHERE 1=0")
    sales = {}
    for r in cur.fetchall():
        sales.setdefault(r["sku"], {})[r["month"]] = [int(r["orders"] or 0), int(r["units"] or 0)]
    cur.execute("""SELECT sku, MAX(product_family) fam, MAX(parent) par, MAX(product) prod
                     FROM v_ai_complaints WHERE sku IS NOT NULL GROUP BY sku""")
    sku_meta = {r["sku"]: [r["fam"] or "", r["par"] or "", r["prod"] or ""]
                for r in cur.fetchall()}

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
            "amazon": sum(1 for r in rows if (r["source"] or "") == "amazon"),
            "needs_review": sum(1 for r in rows if r["needs_review"]),
            "new_code": sum(1 for r in rows if r["is_new_code"]),
            "pending_codes": pend,
            "corrections": corr,
            "validated": validated,
            "removed": removed,
            "human": sum(1 for r in rows
                         if r["human_decision"] in ("accepted", "corrected")),
            "rule_deleted": rule_deleted,
            "no_prose": no_prose,
            "queue_no_prose": queue_no_prose,
            "distinct": len(dist),
        },
        "cats": cats,
        # Everything the client needs to answer "what is the mix, is it moving,
        # and how exposed are we" without a second request.
        "families": families,
        "mix": {"today": date.today().isoformat(), "feed_dates": feed_dates,
                "source_max": source_max, "sales": sales, "sku_meta": sku_meta},
        "rows": [{
            "id": r["review_id"], "d": r["review_date"],
            "mon": r["review_date"][:7] if r["review_date"] else "",
            "s": r["sku"] or "", "p": r["parent"] or "", "pr": r["product"] or "",
            "fam": r["product_family"] or "",
            "a": r["asin"] or "", "o": r["order_id"] or "", "ct": r["country"] or "",
            # The product title: the ASIN cell shows the ASIN, so the title has to
            # travel with the payload to stay one hover away (and in the detail).
            "ti": r["title"] or "",
            "src": r["source"] or "",
            "c": r["ai_complaint"] or "", "cat": r["ai_category"] or "",
            "fc": r["final_complaint"] or "", "fcat": r["final_category"] or "",
            "c2": r["second_complaint"] or "",
            "nr": int(r["needs_review"] or 0), "nc": int(r["is_new_code"] or 0),
            "hv": int(r["human_validated"] or 0),
            "dec": r["human_decision"] or "", "hc": r["human_complaint"] or "",
            # Who decided and when, for the detail panel's sentence. Two short
            # fields per row; empty string when the AI's label is the one shown.
            "dn": decided_by.get(r["review_id"], ("", ""))[0],
            "dd": decided_by.get(r["review_id"], ("", ""))[1],
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
            .replace("__MIX__", json.dumps(payload["mix"], separators=(",", ":")))
            .replace("__FAMOPTIONS__", "".join(
                f'<option value="{f}">{f}</option>' for f in families))
            .replace("__BANNER__", banner)
            .replace("__APPCTA__", _app_cta())
            .replace("__APPJSON__", json.dumps(_app_url()))
            .replace("__SHEET__", _sheet_url())
            .replace("__GEN__", payload["generated"])
            .replace("__COVERAGE__", payload["coverage"])
            .replace("__RANGE__", _range_text(payload["range"][0], payload["range"][1])))
    # A placeholder left in the page is a link to nowhere or a blank KPI; fail the
    # build instead of shipping it.
    left = re.findall(r"__[A-Z][A-Z0-9_]*__", html)
    if left:
        raise SystemExit(f"unsubstituted placeholder(s) in index.html: {sorted(set(left))}")
    # The casing split this page was rebuilt to fix must not return through any
    # other channel — a row's ai_category, a filter option, a tooltip. If a
    # rejected spelling is anywhere in the page, the build fails.
    back = [s for s in dict.fromkeys(rejected_spellings) if s in html]
    if back:
        raise SystemExit(f"non-canonical category spelling in index.html: {back} "
                         f"(canonical: {canon_cats})")
    with open(os.path.join(SITE, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"index.html: {len(html)} bytes")
    conn.close()


INDEX = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Complaint validation · UEP</title>
<style>
:root{--bg:#f7f8fa;--card:#fff;--ink:#111827;--mut:#6b7280;--soft:#9ca3af;--line:#e9ebef;--accent:#2563eb;--warn:#b45309;--bad:#b42318;--ok:#067647}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
.wrap{display:flex;min-height:100vh}
aside{width:208px;flex:0 0 208px;background:var(--card);border-right:1px solid var(--line);padding:24px 0;position:sticky;top:0;height:100vh;overflow:auto}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:16px;letter-spacing:-.02em;padding:0 20px 18px;color:var(--ink)}
.brand img.mark{height:24px;width:auto;display:block;flex:none}
.brand span{display:block;font-size:10px;font-weight:600;color:var(--soft);letter-spacing:.09em;text-transform:uppercase;margin-top:3px}
.nav{display:block;padding:8px 20px;color:#374151;font-size:13.5px;border-left:2px solid transparent}
.nav:hover{background:#fafbfc}
.nav.on{color:var(--accent);font-weight:650;background:#f5f8ff;border-left-color:var(--accent)}
main{flex:1;min-width:0;padding:28px 34px 72px;max-width:1400px}
header.top{margin-bottom:26px}
.titlerow{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}
h1{font-size:22px;margin:0;letter-spacing:-.025em;font-weight:680}
.sub{color:var(--mut);font-size:12.5px;margin-top:4px;max-width:760px}
.acts{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.ro{font-size:10.5px;color:var(--soft);font-weight:650;text-transform:uppercase;letter-spacing:.06em}
button{font:inherit;font-size:12.5px;font-weight:600;padding:6px 12px;border-radius:7px;border:1px solid var(--line);background:#fff;color:#374151;cursor:pointer}
button:hover{border-color:#cfd6e0}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.noapp{font-size:12px;color:var(--warn);max-width:230px;text-align:right;line-height:1.35}
.filters{display:flex;align-items:flex-end;gap:18px;flex-wrap:wrap;margin-top:20px}
.f{display:flex;flex-direction:column;gap:5px;min-width:0}
.f label{font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--soft)}
.f select,.f input{font:inherit;font-size:13px;padding:6px 9px;border:1px solid #dfe3e9;border-radius:7px;background:#fff;color:var(--ink);min-width:0;max-width:100%}
.f select{min-width:178px}
.f input{width:178px}
.f.grow{flex:1 1 190px}
.f.grow input{width:100%}
.f select:focus,.f input:focus{outline:2px solid #dbeafe;border-color:#bfd4f7}
.cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:16px;margin:0 0 28px}
a.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:11px;padding:16px 18px;color:inherit}
a.card:hover{border-color:#c7d6f2}
.card .n{font-size:25px;font-weight:680;letter-spacing:-.02em;line-height:1.1;font-variant-numeric:tabular-nums}
.card .l{font-size:13px;color:#374151;margin-top:6px;font-weight:600}
.card .s{font-size:11.5px;color:var(--soft);margin-top:2px}
.duo{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:24px;align-items:start;margin-bottom:24px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:18px 20px;min-width:0}
.phead{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:14px}
.phead h2{font-size:15px;margin:0;font-weight:680;letter-spacing:-.01em}
.pcount{font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums;white-space:nowrap}
.mrow{display:grid;grid-template-columns:minmax(0,1fr) 58px minmax(44px,34%);align-items:center;gap:12px;padding:7px 0;font:inherit;font-size:13px;border:0;background:none;width:100%;text-align:left;color:inherit}
.mname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:550}
.mnum{text-align:right;font-variant-numeric:tabular-nums;color:#374151}
.mbar{height:8px;background:#f2f4f7;border-radius:4px;overflow:hidden}
.mbar i{display:block;height:100%;background:#7ba3e8;border-radius:4px}
.mrow.muted .mname{color:var(--mut);font-weight:500}
.mrow.muted .mbar i{background:#cdd3dc}
.mrow.more{cursor:pointer;border-radius:8px}
.mrow.more:hover{background:#fafbfd}
.mrow.more .mname{color:var(--accent);font-weight:600}
.marrow{font-size:10px;color:var(--soft);margin-left:5px}
.mixall{margin-top:4px;border-top:1px solid var(--line);padding-top:6px}
.mixall[hidden]{display:none}
.trend svg{display:block;width:100%;height:auto}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:12px;font-size:11.5px;color:#4b5563}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:10px;height:10px;border-radius:2px;background:#7ba3e8;flex:0 0 auto}
.swl{width:18px;height:0;border-top:2px dashed #111827;opacity:.6;flex:0 0 auto}
.cap{font-size:11.5px;color:var(--soft);margin-top:10px;line-height:1.5}
.empty{padding:26px 0;color:var(--soft);font-size:12.5px}
.strip{display:flex;flex-wrap:wrap;align-items:center;gap:4px 9px;background:var(--card);border:1px solid var(--line);border-radius:11px;padding:12px 18px;margin:0 0 24px;font-size:12.5px;color:#374151}
.strip b{font-weight:680;font-variant-numeric:tabular-nums;color:var(--ink)}
.strip .sep{color:#d5d9e0}
.strip .end{margin-left:auto;color:var(--mut);font-size:12px;display:inline-flex;gap:9px;flex-wrap:wrap}
.tblcard{background:var(--card);border:1px solid var(--line);border-radius:11px;overflow:hidden}
.tblwrap{overflow:auto;max-height:calc(100vh - 240px)}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:12.5px;white-space:nowrap}
th{position:sticky;top:0;z-index:2;background:#fbfbfc;text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--soft);font-weight:700;padding:10px 12px;border-bottom:1px solid var(--line)}
.filterrow th{top:32px;background:#fff;padding:5px 8px;border-bottom:1px solid var(--line);text-transform:none;letter-spacing:0;font-weight:400}
.filterrow input,.filterrow select{width:100%;min-width:56px;max-width:100%;padding:4px 6px;border:1px solid #e3e6eb;border-radius:6px;font:inherit;font-size:12px;background:#fff;color:var(--ink)}
td{padding:9px 12px;border-bottom:1px solid #f4f5f7;vertical-align:top}
tbody tr{cursor:pointer}
tbody tr:hover td{background:#fafcff}
tbody tr.sel td{background:#f5f8ff}
tbody tr.sel td:first-child{box-shadow:inset 2px 0 0 var(--accent)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:#4b5563}
td.txt{white-space:normal;min-width:320px;max-width:520px;line-height:1.45;color:#4b5563;cursor:pointer}
/* Three lines, then an ellipsis on a line boundary. The old rule clamped the
   <td> itself with display:-webkit-box, which fought the table layout and cut
   words; the clamp lives on an inner block and the cell expands on click. */
.rvclip{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;white-space:normal}
td.txt.exp .rvclip{display:block;-webkit-line-clamp:unset;overflow:visible}
td.txt .chip{margin-bottom:3px}
a.lnk{color:var(--accent);text-decoration:underline;text-underline-offset:2px;text-decoration-thickness:1px}
a.lnk:hover{text-decoration-thickness:2px}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:10.5px;font-weight:650;border:1px solid #e5e7eb;background:#f7f8fa;color:#6b7280}
.tag.auto{background:#f7f8fa;border-color:#e5e7eb;color:#6b7280}
.tag.rev{background:#f2f7ff;border-color:#d7e3fb;color:#1d4ed8}
.tag.new{background:#f4f4f5;border-color:#e4e4e7;color:#3f3f46}
.tag.ok{background:#f7f8fa;border-color:#e5e7eb;color:#374151}
.tag.blank{background:#f7f8fa;border-color:#e5e7eb;color:#9ca3af}
/* Provenance badge: WHO produced the label being reported, beside the status
   tag that says WHETHER the row has been looked at. Two questions, two chips. */
.tag.prov{background:#f4f4f5;border-color:#e4e4e7;color:#52525b;font-weight:600}
.tag.prov.human{background:#eef3ff;border-color:#c9d9f8;color:#1d4ed8}
.tag.prov.excl{background:#fdf3f2;border-color:#f2d3cf;color:#a4504a}
.foot{display:flex;align-items:center;gap:10px;padding:11px 14px;border-top:1px solid var(--line);background:#fbfbfc;font-size:12.5px;color:var(--mut);flex-wrap:wrap}
.foot .hint{margin-left:auto;color:var(--soft);font-size:12px}
.detail{display:none;margin-top:20px}
.detail.open{display:block}
.dgrid{display:grid;grid-template-columns:260px minmax(0,1fr) 320px;gap:20px;padding:18px}
.dbox{background:#fff;border:1px solid var(--line);border-radius:9px;padding:14px;min-width:0}
.dbox h3{margin:0 0 10px;font-size:13px;letter-spacing:-.01em;font-weight:650}
.kv{display:grid;grid-template-columns:78px minmax(0,1fr);gap:6px 8px;font-size:12.5px}
.kv .k{color:var(--mut);font-weight:600}
.kv .v{color:#111;word-break:break-word}
.comment{white-space:pre-wrap;font-size:13px;line-height:1.6;margin:0}
.cats{max-height:320px;overflow:auto;padding-right:4px}
.cat{margin-bottom:12px}
.cat h4{margin:0 0 6px;font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--soft);font-weight:700}
.cat h4 b{color:var(--soft);font-weight:600}
.opt{display:flex;align-items:center;gap:7px;padding:3px 0;font-size:12.5px;color:#374151;cursor:default}
.opt .n{margin-left:auto;color:var(--soft);font-size:11.5px}
.opt.on{color:var(--accent);font-weight:650}
.dot{width:11px;height:11px;border-radius:3px;border:1.5px solid #cbd5e1;flex:0 0 auto}
.opt.on .dot{background:var(--accent);border-color:var(--accent)}
.note{font-size:11.5px;color:var(--mut);margin-top:10px;line-height:1.5}
#dq{width:100%;padding:5px 8px;border:1px solid var(--line);border-radius:6px;font:inherit;font-size:12px;margin-bottom:8px}
.badge{display:inline-block;font-size:10.5px;font-weight:650;color:#374151;background:#f7f8fa;border:1px solid #e5e7eb;border-radius:999px;padding:1px 7px;margin-left:6px}
.chip{display:inline-block;padding:0 6px;border-radius:4px;font-size:10.5px;font-weight:600;background:#f4f6fa;border:1px solid #e6eaf1;color:#4b5563;margin:0 3px 2px 0;white-space:nowrap}
.chip.q{background:#f7f8fa;border-color:#eceef2;color:#6b7280}
td.txt .prose{display:block;margin-top:2px}
.dtext .chip{margin-bottom:4px}
.dtext p{margin:8px 0 0;font-size:13px;line-height:1.6;white-space:pre-wrap}
.dnone{color:var(--mut);font-style:italic;font-size:12.5px;margin-top:6px}
.detail .ro{font-size:10px}
.syncbar{padding:8px 12px;border-radius:7px;font-size:12.5px;margin:0 0 18px;border:1px solid;line-height:1.5}
.syncbar.ok{background:#f0fdf4;border-color:#bbf7d0;color:#166534}
.syncbar.warn{background:#fffbeb;border-color:#fde68a;color:#92400e}
.syncbar.bad{background:#fef2f2;border-color:#fecaca;color:#b42318}
.about{background:transparent;border-top:1px solid var(--line);margin-top:34px;padding-top:4px}
.about summary{cursor:pointer;font-size:12.5px;font-weight:600;color:var(--mut);padding:10px 0;list-style:none}
.about summary::-webkit-details-marker{display:none}
.about summary:before{content:"▸";display:inline-block;width:15px;color:var(--soft);font-size:10px}
.about[open] summary:before{content:"▾"}
.aboutbody{max-width:820px;font-size:12px;color:var(--mut);line-height:1.65;padding:0 0 8px 15px}
.aboutbody p{margin:0 0 10px}
.aboutbody b{color:#374151;font-weight:650}
@media(max-width:1280px){.cards{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:1080px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:960px){.duo{grid-template-columns:minmax(0,1fr)}.dgrid{grid-template-columns:minmax(0,1fr)}}
@media(max-width:760px){
 .wrap{display:block}
 aside{position:static;width:auto;flex:none;height:auto;display:flex;align-items:center;gap:2px;padding:8px 10px;border-right:0;border-bottom:1px solid var(--line);overflow-x:auto}
 .brand{padding:0 12px 0 4px;font-size:14px;white-space:nowrap}
 .brand span{display:none}
 .nav{padding:6px 10px;border-left:0;border-bottom:2px solid transparent;white-space:nowrap;font-size:13px}
 .nav.on{border-left:0;border-bottom-color:var(--accent)}
 main{padding:18px 16px 48px}
 h1{font-size:19px}
 .cards{gap:12px;margin-bottom:22px}
 a.card{padding:13px 14px}
 .card .n{font-size:22px}
 .panel{padding:16px 15px}
 .strip{padding:10px 14px}
 .strip .end{margin-left:0;width:100%}
 .foot .hint{display:none}
 .filterrow th{top:30px}
 table{white-space:normal}
 td.txt{min-width:0;max-width:190px}
 th,td{padding:8px 8px}
}
@media(max-width:430px){.f.grow{flex:1 1 100%}.f select{min-width:0;width:100%}.f input{width:100%}.f{flex:1 1 140px}}
</style></head><body>
<div class="wrap">
<aside>
  <div class="brand"><img class="mark" src="assets/logo-mark-light.png" alt="UEP" width="84" height="24"><span>Complaint validation</span></div>
  <a class="nav on" href="./index.html">Reviews</a>
  <a class="nav" href="./codes.html">New codes</a>
  <a class="nav" href="./quality.html">Quality</a>
</aside>
<main>
  <header class="top">
    <div class="titlerow">
      <div>
        <h1>Complaint validation</h1>
        <div class="sub">__RANGE__ · __COVERAGE__</div>
      </div>
      <div class="acts">
        <span class="ro" title="this page cannot write to the database">Read-only</span>
        <button id="csv">Export CSV</button>
        <button id="csvtxt" title="same rows plus the review text">Export with text</button>
        __APPCTA__
      </div>
    </div>
    <div class="filters">
      <div class="f"><label for="famsel">Family</label>
        <select id="famsel" data-f="fam"><option value="">All product families</option>__FAMOPTIONS__</select></div>
      <div class="f"><label for="datesel">Date</label>
        <input id="datesel" data-f="d" placeholder="all dates · YYYY-MM" aria-label="Month, YYYY-MM"></div>
      <div class="f"><label for="winsel">Range</label>
        <select id="winsel" data-f="win" aria-label="Recent months"><option value="">all</option><option value="3">last 3 months</option><option value="6">last 6 months</option><option value="12">last 12 months</option></select></div>
      <div class="f"><label for="srcsel">Source</label>
        <select id="srcsel" data-f="src"><option value="">All sources</option></select></div>
      <div class="f grow"><label for="qsel">Search</label>
        <input id="qsel" data-f="q" placeholder="search the review text"></div>
    </div>
  </header>
  __BANNER__
  <div class="cards" id="cards"></div>

  <div class="duo">
    <section class="panel" id="mix" data-n="0" data-sum="0">
      <div class="phead"><h2>What people complained about</h2><span class="pcount" id="mixcount"></span></div>
      <div id="mixbars"></div>
      <div id="mixall" class="mixall" hidden></div>
    </section>
    <section class="panel" id="trendp">
      <div class="phead"><h2>Over time</h2><span class="pcount" id="trendcount"></span></div>
      <div class="trend" id="trend"></div>
      <div class="legend" id="legend"></div>
      <div class="cap" id="trendcap"></div>
    </section>
  </div>

  <div class="strip" id="strip"></div>

  <div class="tblcard" id="table">
    <div class="tblwrap">
      <table>
        <thead>
          <tr><th>Status</th><th>Date</th><th>ASIN</th><th>SKU</th><th>Complaint</th><th>Review</th></tr>
          <tr class="filterrow">
            <th><select data-f="st"><option value="">All reviews</option><option value="nr">Needs a human check</option><option value="nc">Uses a new code</option><option value="codeonly">No comment (code only)</option><option value="ok">Handled automatically</option><option value="done">Checked by the team</option><option value="ai">AI Classified</option><option value="human">Human Classified</option><option value="excluded">Excluded</option></select></th>
            <th></th>
            <th><input data-f="a" placeholder="filter"></th>
            <th><input data-f="s" placeholder="filter"></th>
            <th><select data-f="fc"><option value="">All</option></select></th>
            <th></th>
          </tr>
        </thead>
        <tbody id="tb"></tbody>
      </table>
    </div>
    <div class="foot">
      <button id="prev">Previous</button><button id="next">Next</button>
      <span id="pinfo"></span>
      <span class="hint">Click a row to open the review</span>
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

  <details class="about" id="about">
    <summary>About this data</summary>
    <div class="aboutbody">
      <p><b>Reconciliation.</b> <span id="a-recon"></span></p>
      <p><b>Feed coverage, and why no change arrows.</b> <span id="a-feed"></span></p>
      <p><b>Picklist-only records.</b> <span id="a-pick"></span></p>
      <p><b>What the rate is.</b> <span id="a-rate"></span></p>
      <p><b>Validation status.</b> <span id="a-valid"></span></p>
      <p><b>Label source.</b> <span id="a-src"></span></p>
      <p><b>Process noise.</b> <span id="a-noise"></span></p>
      <p><b>Status labels.</b> <b>automatic</b> — the AI was confident and used a code you already have; nothing to do · <b>needs check</b> — it wasn't sure, so a person should confirm · <b>new code</b> — it used a complaint type that isn't in your list yet · <b>confirmed / corrected / removed</b> — someone has already decided. Figures and the complaint mix use the corrected label where one exists. The second badge on each row is separate from that: <b>AI Classified</b> — nobody has decided it, so the label being reported is the classifier's · <b>Human Classified</b> — a person confirmed or corrected it, and their label is the one being reported · <b>Excluded</b> — a person marked it not a complaint, so it reports no label at all.</p>
    </div>
  </details>
</main></div>
<script>
const KPI=__KPI__, CATS=__CATS__, APP=__APPJSON__, MIX=__MIX__;
/* Display-only renames. The payload keeps the stored value so the CSV and any
   pivot still say 'Vague Complaint'; the screen says what it means. Review
   Deletion is a rule verdict on empty or non-complaint text, not a complaint —
   and it is a DIFFERENT thing from the repeat rows (dup_rank), so the two must
   never share the word 'duplicate' in copy. */
const RENAME={"Vague Complaint":"Unspecified"};
const NOISE="Review Deletion";
const NOCODE="(no complaint assigned)";
const TOPN=6, RATE_FLOOR=100;
function nice(c){return RENAME[c]||c||""}
let ROWS=[], VIEW=[], MIXSCOPE=[], page=0, PER=100, cur=null, mixOpen=false;

/* ---------------------------------------------------------------- cards
   Four numbers, four doors. Each one is a link to the view that answers it. */
const CARDS=[
  {k:"total",        l:"complaint records",         s:"all sources and families",     href:"#table",      st:""},
  {k:"needs_review", l:"need a human check",        s:"the AI wasn't confident",      href:"#table",      st:"nr"},
  {k:"pending_codes",l:"codes awaiting a decision", s:"not in the complaint list yet",href:"./codes.html",st:null},
  {k:"corrections",  l:"corrected by the team",     s:"corrections applied to every figure", href:"#table", st:"done"},
  /* Appended, not inserted: the four above keep their order and their numbers.
     This one answers a different question — whose label is being reported — and
     carries the share of all records it holds underneath. */
  {k:"human",        l:"Human classified",           s:()=>{const h=KPI.human||0,t=KPI.total||1,p=100*h/t;
     return (h?((p<1?p.toFixed(2):p.toFixed(1))+"%"):"0.0%")+" of all records"}, href:"#table", st:"human"}
];
document.getElementById("cards").innerHTML=CARDS.map(c=>
  `<a class="card" href="${c.href}" data-st="${c.st===null?"":c.st}">`+
  `<div class="n">${(KPI[c.k]||0).toLocaleString()}</div>`+
  `<div class="l">${c.l}</div><div class="s">${typeof c.s==="function"?c.s():c.s}</div></a>`).join("");
document.getElementById("cards").addEventListener("click",e=>{
  const a=e.target.closest("a.card"); if(!a) return;
  if(a.getAttribute("href")==="./codes.html") return;   /* real navigation */
  e.preventDefault();
  const sel=document.querySelector('[data-f="st"]');
  if(sel){sel.value=a.dataset.st; apply();}
  document.getElementById("table").scrollIntoView({behavior:"smooth",block:"start"});
});

function esc(s){return (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
function nfmt(n){return (n||0).toLocaleString()}

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
function monthNum(m){return m?parseInt(m.slice(0,4),10)*12+parseInt(m.slice(5,7),10):0}
function catsOf(rows){
  /* Recompute the category groups from the rows on screen so the detail panel
     agrees with the table after a filter. Process noise is a rule verdict, so
     it is not a complaint group. */
  const out={};
  rows.forEach(r=>{if(r.fc===NOISE)return;const k=r.fcat||"Uncategorised";const c=r.fc||"(blank)";
    let g=out[k]||(out[k]=[]);let o=g.find(x=>x.c===c);
    if(o)o.n++;else g.push({c:c,n:1});});
  Object.values(out).forEach(g=>g.sort((a,b)=>b.n-a.n));
  const sorted={};Object.keys(out).sort((a,b)=>out[b].reduce((s,x)=>s+x.n,0)-out[a].reduce((s,x)=>s+x.n,0)).forEach(k=>sorted[k]=out[k]);
  return sorted;
}

/* ------------------------------------------------------------ date helpers */
function mMs(s){const p=String(s).split("-");return Date.UTC(+p[0],+p[1]-1,+p[2])}
function mIso(v){return new Date(v).toISOString().slice(0,10)}
function addDays(s,n){return mIso(mMs(s)+n*86400000)}
function diffDays(a,b){return Math.round((mMs(b)-mMs(a))/86400000)}
function mBounds(m){const y=+m.slice(0,4),o=+m.slice(5,7);
  return [y+"-"+String(o).padStart(2,"0")+"-01", mIso(Date.UTC(y,o,1)-86400000)]}
const FEED=(MIX&&MIX.feed_dates)||[];
const COVER_SLACK=3;
/* Days the WHOLE feed covers in a window: a feed day with no row for this
   selection is a genuine zero and must be divided by, and a day before the feed
   existed is not a zero, it is not data. */
function feedDays(a,b){let n=0;for(let i=0;i<FEED.length;i++){const d=FEED[i];if(d>=a&&d<=b)n++}return n}

function winStats(rows){
  const out={};
  rows.forEach(r=>{
    if(!r.d) return;
    const s=r.src||"(no source)";
    const d=out[s]||(out[s]={n:0,np:0,mn:"",mx:"",codes:{}});
    d.n++; if(!r.hp) d.np++;
    if(!d.mn||r.d<d.mn) d.mn=r.d;
    if(!d.mx||r.d>d.mx) d.mx=r.d;
    const c=r.fc||NOCODE; d.codes[c]=(d.codes[c]||0)+1;
  });
  return out;
}
function mergeCodes(st,only){
  const codes={}; let tot=0, np=0;
  Object.keys(st).forEach(s=>{
    if(only && only.indexOf(s)<0) return;
    tot+=st[s].n; np+=st[s].np;
    const c=st[s].codes;
    Object.keys(c).forEach(k=>{codes[k]=(codes[k]||0)+c[k]});
  });
  return {codes:codes,total:tot,np:np};
}
/* The window the reader asked for. An explicit YYYY-MM month keeps its CALENDAR
   bounds; otherwise the last three months present here. A window never runs past
   today, and the rates never divide by today — a day still in progress holds a
   fraction of its records and would read as a fall. */
function mixWindow(raw,dated){
  if(!dated.length) return null;
  let lo=dated[0].d, hi=dated[0].d;
  dated.forEach(r=>{if(r.d<lo)lo=r.d;if(r.d>hi)hi=r.d});
  const dm=(raw.d||"").trim();
  let f="",t="",basis="",explicit=false;
  if(/^\d{4}-\d{2}$/.test(dm)){
    const b=mBounds(dm); f=b[0]; t=b[1]; basis="month "+dm; explicit=true;
  }else{
    const keys=[...new Set(dated.map(r=>r.mon).filter(Boolean))].sort();
    const k=keys.slice(-3);
    if(k.length){
      f=mBounds(k[0])[0]; t=mBounds(k[k.length-1])[1];
      basis="last "+k.length+" month"+(k.length>1?"s":"")+" with data here ("+k[0]+" to "+k[k.length-1]+")";
    }
  }
  if(!f||!t) return null;
  const today=(MIX&&MIX.today)||mIso(Date.now());
  if(explicit&&t>=today) t=today;
  else if(!explicit&&t>hi) t=hi;
  if(f>t) f=t;
  const yday=addDays(today,-1);
  const rate_t=(yday<t)?yday:t;
  const rate_ok=rate_t>=f;
  let pf="",pt="",span=0;
  if(rate_ok){
    span=diffDays(f,rate_t)+1;
    pt=addDays(f,-1);
    pf=addDays(pt,-(span-1));
  }
  return {f:f,t:t,rate_t:rate_t,rate_ok:rate_ok,pf:pf,pt:pt,span:span,basis:basis,
          explicit:explicit,lo:lo,hi:hi,today:today};
}

function trimMonths(list){
  if(list.length<=3) return {kept:list,omitted:0};
  const total=list.reduce((s,x)=>s+x[1],0);
  let best=null,acc=0,start=0;
  for(let i=0;i<list.length;i++){
    acc+=list[i][1];
    while(start<i&&acc-list[start][1]>=total*0.995){acc-=list[start][1];start++}
    if(acc>=total*0.995&&(!best||(i-start)<(best[1]-best[0]))) best=[start,i];
  }
  if(!best) return {kept:list,omitted:0};
  const kept=list.slice(best[0],best[1]+1);
  return {kept:kept,omitted:total-kept.reduce((s,x)=>s+x[1],0)};
}

/* ------------------------------------------------------------ sales scope
   The denominator's scope is the PRODUCT filter only. The complaint code, the
   source and the country never narrow it: every order in the family could have
   produced a Too Firm complaint, so every one of them is exposure. */
function salesScope(raw){
  const meta=(MIX&&MIX.sku_meta)||{};
  const pr=(raw.pr||"").toLowerCase(), sk=(raw.s||"").toLowerCase(), fam=raw.fam||"";
  return Object.keys(meta).filter(k=>{
    const m=meta[k]||[];
    if(fam && m[0]!==fam) return false;
    if(pr && !String(m[2]||"").toLowerCase().includes(pr)) return false;
    if(sk && !k.toLowerCase().includes(sk)) return false;
    return true;
  });
}
function monthsOrders(raw,months){
  const sales=(MIX&&MIX.sales)||{}, out={};
  months.forEach(m=>out[m]=0);
  salesScope(raw).forEach(k=>{
    const s=sales[k]; if(!s) return;
    months.forEach(m=>{const v=s[m]; if(v) out[m]+=v[0]});
  });
  return out;
}

function rateInfo(raw,months){
  const sales=(MIX&&MIX.sales)||{};
  const scope=salesScope(raw);
  let orders=0,units=0,skuMonths=0,zeroMonths=0;
  scope.forEach(k=>{
    const s=sales[k]; if(!s) return;
    months.forEach(m=>{
      const v=s[m]; if(!v) return;
      skuMonths++; orders+=v[0]; units+=v[1];
      if(!v[0]) zeroMonths++;
    });
  });
  let recs=0,cov=0,nosales=0,zero=0,outside=0;
  VIEW.forEach(r=>{
    /* The rate's numerator is COMPLAINT RECORDS — the same set the mix counts —
       not every row in the table. A record outside the charted months is counted
       and named, never silently divided into a denominator it does not belong to. */
    if(!r.fc||r.fc===NOISE) return;
    if(r.fcat&&["NEW","NO_TEXT","ERR"].indexOf(r.fcat)>=0) return;
    if(months.indexOf(r.mon)<0){ outside++; return; }
    recs++;
    const v=((sales[r.s]||{})[r.mon]);
    if(!v) nosales++;
    else if(!v[0]) zero++;
    else cov++;
  });
  return {orders:orders,units:units,skuMonths:skuMonths,zeroMonths:zeroMonths,
          recs:recs,cov:cov,nosales:nosales,zero:zero,outside:outside,scopeSkus:scope.length};
}

/* ------------------------------------------------------- what people said
   Two rules make this panel honest:

   1. EVERY ROW IS A COUNT, AND THE ROWS ADD UP TO THE TABLE. The six largest
      complaint types are shown; every other code sits under one row, and the
      records with no complaint sit under one more. Nothing is dropped, so the
      mix reconciles to the table footer on any filter.

   2. SHARE IS IN THE HEADLINE, NOT ON EVERY ROW. One 'N records' in the panel
      head; the bars carry the shape, not a percentage per line. */
function mixPanel(raw){
  const box=document.getElementById("mix");
  const n=VIEW.length;
  box.dataset.n=n; box.dataset.sum=n;

  const cnt={};
  VIEW.forEach(r=>{const k=r.fc||NOCODE;cnt[k]=(cnt[k]||0)+1});
  const noise=cnt[NOISE]||0, blank=cnt[NOCODE]||0;
  const unassigned=blank+noise;
  const bars=Object.keys(cnt).filter(k=>k!==NOISE&&k!==NOCODE)
                 .map(k=>[k,cnt[k]]).sort((a,b)=>b[1]-a[1]||(a[0]<b[0]?-1:1));
  const barsTotal=bars.reduce((s,x)=>s+x[1],0);
  box.dataset.complaints=barsTotal;
  box.dataset.unassigned=unassigned;
  box.dataset.codes=bars.length;

  if(!n){
    document.getElementById("mixcount").textContent="";
    document.getElementById("mixbars").innerHTML='<div class="empty">Nothing matches those filters.</div>';
    document.getElementById("mixall").innerHTML="";
    return {n:n,bars:[],barsTotal:0,blank:blank,noise:noise,unassigned:unassigned};
  }

  const top=bars.slice(0,TOPN), rest=bars.slice(TOPN);
  const restN=rest.reduce((s,x)=>s+x[1],0);
  const mx=bars.length?bars[0][1]:1;
  const row=(c,k,cls,title)=>
     '<div class="mrow '+(cls||"")+'" data-code="'+esc(c)+'" data-n="'+k+'"'+(title?' title="'+esc(title)+'"':'')+'>'+
     '<span class="mname">'+esc(nice(c))+'</span>'+
     '<span class="mnum">'+nfmt(k)+'</span>'+
     '<span class="mbar"><i style="width:'+Math.max(2,Math.round(k*100/mx))+'%"></i></span></div>';

  let html=top.map(([c,k])=>row(c,k,"")).join("");
  if(rest.length){
    html+='<button class="mrow more" id="mixmore" aria-expanded="'+(mixOpen?"true":"false")+'" '+
      'data-codes="'+rest.length+'"><span class="mname">everything else ('+rest.length+' code'+
      (rest.length===1?"":"s")+')<span class="marrow">'+(mixOpen?"▾":"▸")+'</span></span>'+
      '<span class="mnum">'+nfmt(restN)+'</span>'+
      '<span class="mbar"><i style="width:'+Math.max(2,Math.round(restN*100/mx))+'%"></i></span></button>';
  }
  if(unassigned){
    html+='<div class="mrow muted" data-code="'+esc(NOCODE)+'" data-n="'+unassigned+'" '+
      'title="'+blank+' with no complaint code, '+noise+' Review Deletion (process noise)"><span class="mname">not assigned</span>'+
      '<span class="mnum">'+nfmt(unassigned)+'</span>'+
      '<span class="mbar"><i style="width:'+Math.max(2,Math.round(unassigned*100/mx))+'%"></i></span></div>';
  }
  document.getElementById("mixbars").innerHTML=html;
  document.getElementById("mixall").innerHTML=rest.map(([c,k])=>row(c,k,"muted",c)).join("");
  document.getElementById("mixall").hidden=!mixOpen;
  document.getElementById("mixcount").textContent=nfmt(n)+" records";

  const more=document.getElementById("mixmore");
  if(more) more.onclick=()=>{
    mixOpen=!mixOpen;
    document.getElementById("mixall").hidden=!mixOpen;
    more.setAttribute("aria-expanded",mixOpen?"true":"false");
    const arrow=more.querySelector(".marrow"); if(arrow) arrow.textContent=mixOpen?"▾":"▸";
  };
  return {n:n,bars:bars,barsTotal:barsTotal,blank:blank,noise:noise,unassigned:unassigned};
}

/* ------------------------------------------------------------------ trend
   Two series, one chart: complaint records per month (bars, left axis) and
   records per 100 orders (dashed line, right axis). The rate's denominator is
   the product scope's own finance feed for that month. */
function dayLabel(s){const p=String(s).split("-");const M=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return (+p[2])+" "+M[+p[1]-1]+" "+p[0]}

function trendPanel(raw){
  const el=document.getElementById("trend"), cap=document.getElementById("trendcap"),
        leg=document.getElementById("legend"), hd=document.getElementById("trendcount");
  const mcount={};
  VIEW.forEach(r=>{if(r.mon&&r.fc&&r.fc!==NOISE)mcount[r.mon]=(mcount[r.mon]||0)+1});
  const allMonths=Object.keys(mcount).sort().map(m=>[m,mcount[m]]);
  const {kept,omitted}=trimMonths(allMonths);
  const months=kept.map(x=>x[0]);
  const ords=monthsOrders(raw,months);
  const rates={};
  months.forEach(m=>{const o=ords[m]||0; rates[m]=o>0?(100*kept.find(x=>x[0]===m)[1]/o):null});

  hd.textContent=months.length?(dayMonth(months[0])+" – "+dayMonth(months[months.length-1])):"";
  leg.innerHTML='<span><i class="sw"></i>complaint records</span>'+
                '<span><i class="swl"></i>records per 100 orders</span>';

  if(!kept.length){
    el.innerHTML='<div class="empty">No dated complaint records in this selection.</div>';
    cap.textContent="";
    return {months:months,kept:kept};
  }

  const W=520,H=196,L=44,R=46,T=14,B=26;
  const iw=W-L-R, ih=H-T-B;
  const mx=Math.max.apply(null,kept.map(p=>p[1]))||1;
  const rv=months.map(m=>rates[m]).filter(v=>v!=null&&isFinite(v));
  const rmax=rv.length?Math.max.apply(null,rv):0;
  const rtop=rmax>0?rmax*1.2:1;
  const step=iw/kept.length, bw=Math.min(44,Math.max(6,step*0.52));
  let g="";
  for(let k=0;k<=4;k++){
    const y=(T+ih*(1-k/4));
    g+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y.toFixed(1)+'" y2="'+y.toFixed(1)+'" stroke="#eef0f3"/>';
    g+='<text x="'+(L-6)+'" y="'+(y+3).toFixed(1)+'" text-anchor="end" font-size="9.5" fill="#9ca3af">'+
       Math.round(mx*k/4).toLocaleString()+'</text>';
    if(rmax>0) g+='<text x="'+(W-R+6)+'" y="'+(y+3).toFixed(1)+'" font-size="9.5" fill="#9ca3af">'+
       (rtop*k/4).toFixed(1)+'</text>';
  }
  const step2=kept.length>8?2:1;
  kept.forEach((p,i)=>{
    const h=Math.max(1,(p[1]/mx)*ih), x=L+i*step+(step-bw)/2, y=T+ih-h;
    const lab=(i%step2)?"":
      '<text x="'+(x+bw/2).toFixed(1)+'" y="'+(H-8)+'" text-anchor="middle" font-size="9.5" fill="#9ca3af">'+
      (monShort(p[0]))+'</text>';
    g+='<rect data-month="'+p[0]+'" data-n="'+p[1]+'" x="'+x.toFixed(1)+'" y="'+y.toFixed(1)+
       '" width="'+bw.toFixed(1)+'" height="'+h.toFixed(1)+'" rx="2" fill="#7ba3e8">'+
       '<title>'+dayMonth(p[0])+': '+p[1].toLocaleString()+' complaint records'+
       (rates[p[0]]!=null?' · '+rates[p[0]].toFixed(2)+' per 100 orders':'')+'</title></rect>'+lab;
  });
  /* the dashed rate line, broken wherever a month has no orders to divide by */
  let seg=[], pts=[];
  months.forEach((m,i)=>{
    const v=rates[m], x=L+i*step+step/2;
    if(v==null||!isFinite(v)){ if(seg.length>1) pts.push(seg); seg=[]; return; }
    seg.push([x, T+ih-ih*(v/rtop)]);
  });
  if(seg.length>1) pts.push(seg);
  pts.forEach(s=>{
    g+='<polyline points="'+s.map(p=>p[0].toFixed(1)+","+p[1].toFixed(1)).join(" ")+
       '" fill="none" stroke="#111827" stroke-width="1.6" stroke-dasharray="4 3" opacity=".55"/>';
  });
  months.forEach((m,i)=>{
    const v=rates[m]; if(v==null||!isFinite(v)) return;
    g+='<circle cx="'+(L+i*step+step/2).toFixed(1)+'" cy="'+(T+ih-ih*(v/rtop)).toFixed(1)+
       '" r="2.4" fill="#111827" opacity=".6"/>';
  });
  el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="Complaint records and records per 100 orders by month" data-months="'+kept.length+'">'+g+'</svg>';

  cap.textContent=coverageNote(months)+(omitted?' · '+omitted.toLocaleString()+' older records outside this range are not drawn':'');
  return {months:months,kept:kept};
}
function dayMonth(m){const M=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return M[+m.slice(5,7)-1]+" "+m.slice(0,4)}
function monShort(m){const M=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return M[+m.slice(5,7)-1]}

/* One line, naming the months whose source coverage differs from the latest
   month shown. No paragraph: the ends of the feeds are the whole story. */
function coverageNote(months){
  if(months.length<2) return "One month shown.";
  const last=months[months.length-1];
  const by={};
  VIEW.forEach(r=>{if(!r.mon||!r.fc||r.fc===NOISE)return;
    const s=r.src||"(no source)"; (by[s]=by[s]||{})[r.mon]=(by[s][r.mon]||0)+1;});
  const ends=(MIX&&MIX.source_max)||{};
  const parts=[];
  Object.keys(by).sort().forEach(s=>{
    const ms=months.filter(m=>by[s][m]);
    if(!ms.length) return;
    if(ms[ms.length-1]!==last){
      const d=ends[s]||"";
      parts.push(s+(d?" ends "+dayLabel(d):" stops after "+dayMonth(ms[ms.length-1])));
    }
  });
  if(!parts.length) return "Every source reports in every month shown.";
  return "Coverage changes: "+parts.join("; ")+".";
}

/* -------------------------------------------------------------- rate strip
   One line. Orders, units, the rate, the validated count, and the source split
   that no longer needs a chart of its own. */
function stripPanel(raw,months){
  const n=VIEW.length, ri=rateInfo(raw,months);
  const bits=['<span><b>'+nfmt(ri.orders)+'</b> orders</span>',
              '<span><b>'+nfmt(ri.units)+'</b> units</span>'];
  let rate=null;
  if(!ri.recs) bits.push('<span>no rate — no complaint records</span>');
  else if(!ri.orders) bits.push('<span>no rate — 0 orders in scope</span>');
  else if(ri.orders<RATE_FLOOR) bits.push('<span>no rate — under '+RATE_FLOOR+' orders</span>');
  else { rate=100*ri.cov/ri.orders; bits.push('<span><b>'+rate.toFixed(2)+'</b> complaints per 100 orders</span>'); }
  bits.push('<span><b>'+nfmt(KPI.validated)+'</b> of <b>'+nfmt(n)+'</b> validated</span>');
  const sep='<span class="sep">·</span>';
  /* The source split belongs to the filter, like every other number on this
     line — a corpus total sitting beside filtered orders reads as a mismatch. */
  const srcCount={};
  VIEW.forEach(r=>{const s=r.src||""; srcCount[s]=(srcCount[s]||0)+1});
  const src=['fba','ncx','amazon'].map(s=>'<span>'+s+' <b>'+nfmt(srcCount[s]||0)+'</b></span>').join("");
  document.getElementById("strip").innerHTML=bits.join(sep)+'<span class="end">'+src+'</span>';
  return ri;
}

/* ------------------------------------------------------------ about this data
   Everything the page used to say in three paragraphs above the table lives
   here, closed by default. Nothing was deleted; it moved. */
function aboutPanel(mix, trend, ri, raw){
  const n=VIEW.length, el=id=>document.getElementById(id);
  const w=mixWinCache;
  el("a-recon").innerHTML=
    '<b>'+nfmt(n)+'</b> rows match these filters · <b>'+nfmt(mix.barsTotal)+
    '</b> carry a real complaint · <b>'+nfmt(mix.blank)+'</b> carry no complaint · <b>'+
    nfmt(mix.noise)+'</b> are process noise (Review Deletion). Those three add to <b>'+
    nfmt(mix.barsTotal+mix.blank+mix.noise)+'</b> — the same count the table footer shows, '+
    'so the mix reconciles to the table on any filter. The mix shows the six largest complaint '+
    'types; every other code is under “everything else”, and the records with no complaint are '+
    'under “not assigned”, which holds the '+nfmt(mix.blank)+' with no code and the '+
    nfmt(mix.noise)+' Review Deletion rows.';
  el("a-noise").innerHTML=KPI.rule_deleted
    ? '<b>'+nfmt(KPI.rule_deleted)+'</b> records were removed by the rule as empty or '+
      'non-complaint text — not complaints, and listed in <code>v_rule_deletions</code>. '+
      'They stay in the table and the CSV; the mix counts them only so the totals reconcile.'
    : 'No rows were removed by the process-noise rule.';
  el("a-pick").innerHTML=
    '<b>'+nfmt(KPI.no_prose)+'</b> of <b>'+nfmt(KPI.total)+'</b> rows carry no customer comment; '+
    'they hold the return-reason picklist only, so no label on them can be checked against what a '+
    'customer wrote ('+nfmt(KPI.queue_no_prose)+' of them are still unreviewed). '+
    'Filter them out with the status list to work only what can be checked.';
  el("a-valid").innerHTML=
    '<b>'+nfmt(KPI.validated)+'</b> of <b>'+nfmt(n)+'</b> records have a decision recorded in this app so far. '+
    'The labels themselves were validated before launch: the CX team has 553,055 human-labelled review×complaint pairs in the live module, and Chadia\u2019s own blind 220-row check of this AI put agreement at 83.6%. '+
    ' Figures and the complaint mix use the corrected label where one exists, and the AI’s own '+
    'label stays on each row for audit.';
  /* Same three numbers as the badges on the rows, so the split always adds to
     the row count on screen: human + AI + excluded = rows. */
  {
    const hv=VIEW.filter(r=>r.dec==="accepted"||r.dec==="corrected").length;
    const xv=VIEW.filter(r=>r.dec==="rejected").length;
    el("a-src").innerHTML='<b>'+nfmt(hv)+'</b> record'+(hv===1?' carries':'s carry')+' a human label, '+
      '<b>'+nfmt(n-hv-xv)+'</b> carry the AI’s'+
      (xv?' and <b>'+nfmt(xv)+'</b> '+(xv===1?'is':'are')+' excluded from reporting':'')+
      '. The badge on each row says which of those is being reported; the status tag beside it '+
      'says whether anyone has looked at the row yet, which is a different question.';
  }
  el("a-rate").innerHTML=rateLong(ri,trend.months);
  el("a-feed").innerHTML=feedLong(w);
}

function feedLong(w){
  let s="";
  if(w){
    if(w.explicit) s+="Period "+w.f+" to "+w.rate_t+". ";
    else s+="Period "+w.f+" to "+w.rate_t+" ("+w.basis+"). ";
    if(w.rate_ok&&w.rate_t<w.t) s+="Today ("+w.t+") is incomplete and is not counted in the rates. ";
  }
  s+=(deltaCtx.show
      ? "Change is compared per feed day over "+deltaCtx.lfl.join(", ")+" only: "+
        nfmt(deltaCtx.curTotal)+" records here against "+nfmt(deltaCtx.priorTotal)+
        " before ("+w.pf+" to "+w.pt+"). "
      : "No month-on-month change is shown for this filter"+
        (deltaCtx.reasons.length?": <b>this comparison is not like-for-like</b> — "+
          esc(deltaCtx.reasons.join("; "))+".":" because no earlier window holds comparable records.")+" ");
  s+="Only a change figure needs the same feeds on both sides, because a feed that starts or stops "+
     "inside a window moves the total without any customer changing their behaviour. "+
     "The counts and shares are unaffected.";
  return s;
}

function rateLong(ri,months){
  if(!ri.recs) return "No complaint records in this filter, so there is no rate to quote.";
  if(!ri.skuMonths) return "No sales data covers this filter ("+nfmt(ri.recs)+
    " complaint records, none in a SKU-month the finance feed covers), so there is no "+
    "complaints-per-100-orders for it. The counts and shares are unaffected — they need no denominator.";
  if(!ri.orders) return nfmt(ri.skuMonths)+" SKU-month"+(ri.skuMonths===1?"":"s")+
    " in scope sold <b>0 orders</b>, so the rate is undefined — it is not 0.00, which would claim "+
    "no complaints per order.";
  let s=nfmt(ri.orders)+" orders and "+nfmt(ri.units)+" units across "+nfmt(ri.skuMonths)+
    " SKU-month"+(ri.skuMonths===1?"":"s")+" — <b>"+(100*ri.cov/ri.orders).toFixed(2)+
    "</b> complaint records per 100 orders"+
    (ri.units?" ("+(1000*ri.cov/ri.units).toFixed(2)+" per 1,000 units)":"")+". ";
  if(ri.orders<RATE_FLOOR) s+="That is under the "+RATE_FLOOR+"-order floor, so the arithmetic exists but no rate is quoted above. ";
  if(ri.nosales||ri.zero){
    s+="Excluded from the numerator: "+(ri.nosales?nfmt(ri.nosales)+" in SKU-months with no sales row":"")+
       (ri.nosales&&ri.zero?" and ":"")+(ri.zero?nfmt(ri.zero)+" in SKU-months that sold 0 orders":"")+
       " — there is no denominator there. ";
  }
  if(ri.outside) s+=nfmt(ri.outside)+" record"+(ri.outside===1?"":"s")+" fall outside "+
    (months[0]||"")+" to "+(months[months.length-1]||"")+" and are not in this rate. ";
  s+="Orders are the seller's own finance feed for the scope, all marketplaces; complaints are "+
     "review records, so this is a record rate, not a return rate.";
  return s;
}

let deltaCtx={show:false,reasons:[],lfl:[],curTotal:0,priorTotal:0}, mixWinCache=null;

/* The change that is NOT printed on the page any more is still computed: the
   disclosure at the bottom has to say why it is withheld, in words. Same rule as
   the app: only feeds present in BOTH windows, neither stopping more than 3 days
   before its window end, and a return-reason-only share that did not move >10pp. */
function deltaContext(raw){
  const out={show:false,reasons:[],lfl:[],curTotal:0,priorTotal:0};
  const dated=VIEW.filter(r=>r.d);
  const cmp=(MIXSCOPE.length?MIXSCOPE:ROWS).filter(r=>r.d);
  const w=mixWindow(raw,dated);
  mixWinCache=w;
  if(!w) return out;
  const curDays=feedDays(w.f,w.rate_t), priDays=feedDays(w.pf,w.pt);
  const cur=winStats(cmp.filter(r=>r.d>=w.f&&r.d<=w.rate_t));
  const pri=winStats(cmp.filter(r=>r.d>=w.pf&&r.d<=w.pt));
  const names=[...new Set(Object.keys(cur).concat(Object.keys(pri)))].sort();
  const lfl=[], reasons=[];
  names.forEach(s=>{
    const cn=(cur[s]||{}).n||0, pn=(pri[s]||{}).n||0;
    if(!cn||!pn){
      if(pn&&!cn){
        const end=((MIX&&MIX.source_max)||{})[s]||"";
        reasons.push("the "+s+" feed ends "+(end||"before this window")+", so this comparison is not like-for-like");
      }else if(cn){
        reasons.push(nfmt(cn)+" record"+(cn===1?"":"s")+" labelled "+s+" appear only in the current window");
      }
      return;
    }
    const gc=cur[s].mx?diffDays(cur[s].mx,w.rate_t):99;
    const gp=pri[s].mx?diffDays(pri[s].mx,w.pt):99;
    if(gc>COVER_SLACK||gp>COVER_SLACK){
      reasons.push("the "+s+" feed stops on "+(cur[s].mx||pri[s].mx||"no date")+" in this window, "+
                   gc+" day"+(gc===1?"":"s")+" before it ends");
      return;
    }
    lfl.push(s);
  });
  const mc=mergeCodes(cur,lfl), mp=mergeCodes(pri,lfl);
  const allOf=st=>Object.keys(st).reduce((a,k)=>({n:a.n+st[k].n,np:a.np+st[k].np}),{n:0,np:0});
  const ca=allOf(cur), pa=allOf(pri);
  const sc=ca.n?100*ca.np/ca.n:0, sp=pa.n?100*pa.np/pa.n:0;
  if(pa.n&&Math.abs(sc-sp)>10){
    reasons.push("the return-reason-only share of records moved from "+sp.toFixed(1)+"% to "+
                 sc.toFixed(1)+"% ("+(sc-sp>=0?"+":"")+(sc-sp).toFixed(1)+"pp)");
  }
  const thin=pa.n>0&&(pa.n<100||pa.n<0.5*ca.n);
  out.lfl=lfl; out.reasons=reasons;
  out.curTotal=mc.total; out.priorTotal=mp.total;
  out.show=pa.n>0&&!thin&&lfl.length>0&&reasons.length===0&&curDays>0&&priDays>0&&w.rate_ok;
  if(thin) out.reasons.push("the preceding period ("+w.pf+" to "+w.pt+") holds only "+
    nfmt(pa.n)+" records against "+nfmt(ca.n)+" here, so the change would be coverage, not trend");
  return out;
}

function updatePanels(raw){
  deltaCtx=deltaContext(raw);
  const mix=mixPanel(raw);
  const trend=trendPanel(raw);
  const ri=stripPanel(raw,trend.months);
  aboutPanel(mix,trend,ri,raw);
}

/* --------------------------------------------------------------- the table */
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

/* Provenance: WHO produced the label being reported. Not the same question as
   the tag beside it, which says whether anyone has looked at the row yet — a
   row can be 'automatic' and AI-classified, or 'confirmed' and human-classified.
   'Excluded' is its own value: those rows report no label at all. */
function provenance(r){
  if(r.dec==="accepted"||r.dec==="corrected") return "Human Classified";
  if(r.dec==="rejected") return "Excluded";
  return "AI Classified";
}
function provTag(r){
  const p=provenance(r);
  const cls=p==="Human Classified"?"human":(p==="Excluded"?"excl":"ai");
  return `<span class="tag prov ${cls}" data-prov="${cls}">${p}</span>`;
}
function dlong(s){
  if(!s) return "";
  const d=new Date(s+"T00:00:00");
  return isNaN(d)?s:d.toLocaleDateString("en-GB",{day:"numeric",month:"short",year:"numeric"});
}

function amzLink(href,text,tip){
  return `<a class="lnk mono" href="${href}" target="_blank" rel="noopener"`+
         ` data-ext="1" onclick="event.stopPropagation()"`+
         (tip?` title="${esc(tip)}"`:"")+`>${esc(text)}</a>`;
}
function asinCell(r){
  /* The column that used to read PRODUCT and show a short product code now
     shows the ASIN, linked. Missing ASIN -> an em dash, never /dp/. */
  if(!r.a) return "—";
  return amzLink("https://www.amazon.com/dp/"+encodeURIComponent(r.a),r.a,r.ti||r.pr);
}
function skuCell(r){
  if(!r.s) return "—";
  return amzLink("https://www.amazon.com/s?k="+encodeURIComponent(r.s),r.s,r.ti||r.pr);
}
function txtCell(r){
  const p=parts(r.t);
  const head=chipHtml(p.chips,2);
  /* Full prose in the DOM; CSS clamps it to three lines and the cell expands on
     click. Slicing the string here is what used to cut words in half. */
  const body=p.prose.length?`<span class="prose">${esc(p.prose.join(" "))}</span>`
    :`<span class="prose" style="color:#6b7280">code only — nothing to check</span>`;
  return `<div class="rvclip">${head}${body}</div>`;
}
function render(){
  const slice=VIEW.slice(page*PER,(page+1)*PER);
  document.getElementById("table").dataset.match=VIEW.length;
  document.getElementById("tb").innerHTML=slice.map(r=>`<tr data-id="${r.id}" class="${cur===r.id?'sel':''}">
    <td>${status(r)} ${provTag(r)}</td>
    <td class="mono">${esc(r.d)}</td>
    <td class="mono">${asinCell(r)}</td>
    <td class="mono">${skuCell(r)}</td>
    <td>${esc(nice(r.fc))||"—"}${r.hc&&r.hc!==r.c?'<span title="corrected from '+esc(r.c)+'">*</span>':''}</td>
    <td class="txt" title="Click to read the full comment" onclick="event.stopPropagation();this.classList.toggle('exp')">${txtCell(r)}</td></tr>`).join("")
    || '<tr><td colspan="6" style="padding:34px;text-align:center;color:#6b7280">Nothing matches those filters.</td></tr>';
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
     table, while the same gesture switched a label in the app; the list is a
     reference, so it must not respond to a click at all. */
}

function openRow(id){
  const r=ROWS.find(x=>String(x.id)===String(id)); if(!r) return;
  cur=r;
  document.querySelectorAll("#tb tr").forEach(tr=>tr.classList.toggle("sel",tr.dataset.id===String(id)));
  document.getElementById("detail").classList.add("open");
  document.getElementById("dt").innerHTML=`${esc(r.s||r.pr||"Review")} ${status(r)} ${provTag(r)}`
    +(r.hp?"":' <span class="tag blank">code only</span>');
  document.getElementById("dkv").innerHTML=[
    ["ASIN",r.a],["Review ID",r.id],["Date",r.d],["SKU",r.s],["Product",r.pr],["Title",r.ti],["Family",r.fam],["Parent",r.p],
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
  /* The sentence that makes the badge auditable: a badge reading 'Human
     Classified' without a name and a date is not a provenance record. */
  const who=esc(r.dn)||"an unattributed user", when=dlong(r.dd)||"an unknown date";
  document.getElementById("da").innerHTML =
    (r.dec==="rejected")
      ? `Excluded by ${who} on ${when} — marked not a complaint, so it is out of complaint reporting. The AI had said ${esc(nice(r.c))}.`
    : (r.dec==="corrected")
      ? `Corrected to <b>${esc(nice(r.fc))}</b> by ${who} on ${when} — the AI had said ${esc(nice(r.c))}.`
    : (r.dec==="accepted")
      ? `Confirmed by ${who} on ${when} as <b>${esc(nice(r.fc)||nice(r.c))}</b> — the human label is the one being reported.`
    : (r.c?`Labelled by the classifier — no human decision yet, so the AI's ${esc(nice(r.c))} is what is reported.`:"");
  document.getElementById("dq").value="";
  complaintOptions();
  const on=document.querySelector("#dcats .opt.on"), box=document.getElementById("dcats");
  if(on && box) box.scrollTop = Math.max(0, on.offsetTop - box.offsetTop - 40);
  document.getElementById("detail").scrollIntoView({behavior:"smooth",block:"start"});
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
  VIEW=ROWS.filter(r=>keepRow(r,raw,low,cut,false));
  /* The comparison window is every other filter with the date/window dropped —
     exactly what the app's _win_filter does — so a month filter still has a
     preceding window to compare against in the disclosure. */
  MIXSCOPE=ROWS.filter(r=>keepRow(r,raw,low,cut,true));
  page=0; render();
  if(cur) complaintOptions();
  updatePanels(raw);
}
/* One predicate for the table, the deep link and the disclosure's two windows,
   so a filter cannot apply on screen and silently not apply to a comparison.
   skipDate drops `d` and `win` — used only for the preceding-window scope. */
function keepRow(r,raw,low,cut,skipDate){
  if(!skipDate){
    if(low("d") && !(r.d||"").toLowerCase().includes(low("d"))) return false;
    if(cut && monthNum(r.mon)<cut) return false;
  }
  if(low("pr") && !(r.pr||"").toLowerCase().includes(low("pr"))) return false;
  if(low("p") && !(r.p||"").toLowerCase().includes(low("p"))) return false;
  if(low("s") && !(r.s||"").toLowerCase().includes(low("s"))) return false;
  if(low("a") && !(r.a||"").toLowerCase().includes(low("a"))) return false;
  if(low("id") && !String(r.id).includes(low("id"))) return false;
  if(low("o") && !(r.o||"").toLowerCase().includes(low("o"))) return false;
  if(low("cat") && !(r.cat||"").toLowerCase().includes(low("cat"))) return false;
  if(raw.ct && (r.ct||"")!==raw.ct) return false;
  if(raw.src && (r.src||"")!==raw.src) return false;
  if(raw.fc && (r.fc||"")!==raw.fc) return false;
  if(low("q") && !(r.t||"").toLowerCase().includes(low("q"))) return false;
  if(raw.fam && (r.fam||"")!==raw.fam) return false;
  if(raw.st==="codeonly" && r.hp) return false;
  if(raw.st==="nr" && !r.nr) return false;
  if(raw.st==="nc" && !r.nc) return false;
  if(raw.st==="ok" && (r.nr||r.nc||r.dec)) return false;
  if(raw.st==="done" && !r.dec) return false;
  /* Provenance values in the same control as the status values: one column,
     one dropdown, so the two can never contradict each other. */
  if(raw.st==="ai" && r.dec) return false;
  if(raw.st==="human" && !(r.dec==="accepted"||r.dec==="corrected")) return false;
  if(raw.st==="excluded" && r.dec!=="rejected") return false;
  return true;
}
document.querySelectorAll("[data-f]").forEach(el=>{
  el.addEventListener(el.tagName==="SELECT"?"change":"input",()=>{clearTimeout(window._t);window._t=setTimeout(apply,180)});
});
document.getElementById("dq").addEventListener("input",()=>{clearTimeout(window._tq);window._tq=setTimeout(complaintOptions,120)});
document.getElementById("prev").onclick=()=>{if(page>0){page--;render()}};
document.getElementById("next").onclick=()=>{if((page+1)*PER<VIEW.length){page++;render()}};

fetch("data/reviews.json").then(r=>r.json()).then(d=>{
  ROWS=d.rows; VIEW=ROWS;
  const ct=new Set(), src=new Set();
  ROWS.forEach(r=>{if(r.ct)ct.add(r.ct); if(r.src)src.add(r.src)});
  const srcSel=document.querySelector('[data-f="src"]'), fcSel=document.querySelector('[data-f="fc"]');
  if(srcSel) [...src].sort().forEach(v=>srcSel.insertAdjacentHTML("beforeend",`<option value="${esc(v)}">${esc(v)}</option>`));
  if(fcSel) Object.values(CATS).flat().forEach(o=>fcSel.insertAdjacentHTML("beforeend",`<option value="${esc(o.c)}">${esc(o.c)}</option>`));
  window.__d=d;
  apply();
});

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
</script></body></html>
"""


if __name__ == "__main__":
    main()
