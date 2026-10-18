#!/usr/bin/env python3
"""
AI Complaint Validation - the review app. THIS is the review surface.

Decision (2026-10-18, Philip): in-page correction is approved. The Cloudflare
Pages site (deploy_pages.sh) stays as a READ-ONLY mirror for people who only want
to look; every decision is made here, because this is the only surface that can
write to automation_philip. The Google Sheet is no longer the review queue.

Deliberately dependency-light: stdlib WSGI + pymysql, so it runs on a bare Debian
box with no build step and no package downloads at runtime.

Routes
  /                 dashboard: volumes, resolved-label distribution, coverage
  /reviews          filterable list: product / title / sku, complaint, source,
                    country, parent, month, date range, status; paginated
  /review/<id>      one review: confirm / correct / reject, in place
  /monthly          SKU x month x complaint rollup (the PowerBI grain)
  /codes            proposed codes: approve (joins the vocabulary) or deny
                    (durable: the code is suppressed and its rows remapped)
  /quality          the weekly 45-row random check: record a call per row
  /export.csv       the current filter as CSV (?text=1 adds the review text)
  /healthz          unauthenticated liveness probe for the deploy script

Product identity (product / parent / sku / asin / title) is enriched into
`classifications` by enrich_products.py on the refresh host, straight from
amzmanager.all_products. This app never connects to the source database.

Auth: HTTP Basic (APP_USER / APP_PASS). TLS is terminated by the reverse proxy in
front of the box, so the credentials are never on the wire in clear text.
"""
import base64
import csv
import io
import os
import random
import re
import secrets
import sys
import urllib.parse
from collections import defaultdict
from datetime import date, timedelta
from wsgiref.simple_server import make_server

import pymysql

APP_USER = os.environ.get("APP_USER", "chadia")
APP_PASS = os.environ.get("APP_PASS", "")
REVIEWER_UID = int(os.environ.get("APP_REVIEWER_UID", "1089"))  # Chadia's uid in the legacy users table
SAMPLE_N = int(os.environ.get("APP_SAMPLE_N", "45"))
NOT_A_COMPLAINT = "not a complaint"
PAGE_SIZE = 100

# Default deny mappings, same table as sync_corrections.DENY_MAP. A deny with no
# replacement would silently drop those reviews out of every rollup, so the form
# asks for one and falls back to these for the codes already ruled on.
DENY_MAP = {
    "too deep": "Doesnt Fit",
    "too flat": "Too Soft",
    "sheds - fibers fly off when touching it": "Pilling",
    "not secure": "Not Adjustable",
    "too bright": "Not As Advertised/Misleading",
    "too cold": "Not As Advertised/Misleading",
}

AMZ_DP = "https://www.amazon.com/dp/"
AMZ_SKU_SEARCH = "https://www.amazon.com/s?k="

CSS = """
:root{--bg:#fbfbfd;--card:#fff;--ink:#1a1a1f;--mut:#6b6b76;--line:#e6e6ec;--accent:#2b5cff;--warn:#b45309;--bad:#b42318;--ok:#067647}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--card);border-bottom:1px solid var(--line);padding:14px 28px;display:flex;align-items:center;gap:26px;position:sticky;top:0;z-index:5;flex-wrap:wrap}
header .brand{font-weight:650;letter-spacing:-.01em}
header nav a{color:var(--mut);font-weight:500;margin-right:18px}
header nav a.on{color:var(--ink);font-weight:650}
main{max-width:1240px;margin:0 auto;padding:26px 28px 60px}
h1{font-size:22px;letter-spacing:-.02em;margin:0 0 4px}
h2{font-size:15px;letter-spacing:-.01em;margin:26px 0 10px}
.sub{color:var(--mut);margin:0 0 22px;font-size:13.5px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:26px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi .n{font-size:24px;font-weight:650;letter-spacing:-.02em}
.kpi .l{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.05em;margin-top:2px}
.kpi .d{color:var(--mut);font-size:12px;margin-top:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;color:var(--mut);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;padding:10px 12px;border-bottom:1px solid var(--line);background:#fafafc;white-space:nowrap}
td{padding:10px 12px;border-bottom:1px solid #f1f1f5;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fcfcff}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:600;border:1px solid var(--line);background:#f6f6fa;color:var(--mut);white-space:nowrap}
.pill.new{background:#fff7ed;border-color:#fed7aa;color:var(--warn)}
.pill.ok{background:#ecfdf3;border-color:#abefc6;color:var(--ok)}
.pill.bad{background:#fef3f2;border-color:#fecdca;color:var(--bad)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.prod{font-weight:600}
.title{color:var(--mut);font-size:12.5px;margin-top:2px;max-width:340px}
form.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin-bottom:16px}
label{display:block;font-size:11.5px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}
input,select,textarea{padding:8px 10px;border:1px solid var(--line);border-radius:7px;background:#fff;font:inherit;font-size:13.5px;min-width:130px}
button{padding:8px 14px;border-radius:7px;border:1px solid var(--line);background:#fff;font:inherit;font-weight:600;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{color:var(--bad);border-color:#fecdca}
.review{display:grid;grid-template-columns:1.5fr 1fr;gap:20px;align-items:start}
.text{background:#fafafc;border:1px solid var(--line);border-radius:8px;padding:16px;white-space:pre-wrap;font-size:14.5px;line-height:1.65}
.meta{font-size:12.5px;color:var(--mut)}
.empty{padding:40px;text-align:center;color:var(--mut)}
.flash{padding:11px 14px;border-radius:8px;margin-bottom:16px;font-size:13.5px;background:#ecfdf3;border:1px solid #abefc6;color:var(--ok)}
.flash.err{background:#fef3f2;border-color:#fecdca;color:var(--bad)}
.pager{display:flex;gap:10px;align-items:center;margin-top:14px}
.pager .sp{color:var(--mut);font-size:12.5px}
td.num,th.num{text-align:right}
"""


# ---------------------------------------------------------------- plumbing

def db():
    return pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def page(title, body, active="", msg="", err=False):
    nav = "".join(
        f'<a href="{u}" class="{"on" if active == u else ""}">{n}</a>'
        for u, n in [("/", "Dashboard"), ("/reviews", "Reviews"), ("/monthly", "Monthly"),
                     ("/codes", "New codes"), ("/quality", "Quality")])
    flash = f'<div class="flash{" err" if err else ""}">{esc(msg)}</div>' if msg else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · AI Complaint Validation</title><style>{CSS}</style></head>
<body><header><span class="brand">AI Complaint Validation</span><nav>{nav}</nav></header>
<main>{flash}{body}</main></body></html>"""


def parse_qs(environ):
    return {k: v[0] for k, v in urllib.parse.parse_qs(environ.get("QUERY_STRING", "")).items()}


def _body(environ):
    try:
        n = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        n = 0
    return environ["wsgi.input"].read(n).decode("utf-8") if n else ""


def parse_form(environ):
    return {k: v[0] for k, v in urllib.parse.parse_qs(_body(environ), keep_blank_values=True).items()}


def parse_form_multi(environ):
    """Multi-valued form. The weekly check posts one field per sampled row."""
    return urllib.parse.parse_qs(_body(environ), keep_blank_values=True)


def redirect(start, to, msg="", err=False):
    """302 with a valid empty body.

    WSGI requires an iterable after start_response; returning None raises inside
    the server and the browser sees a 500 on a write that actually succeeded.
    """
    sep = "&" if "?" in to else "?"
    loc = to + (f"{sep}msg={urllib.parse.quote(msg)}" + ("&err=1" if err else "") if msg else "")
    start("302 Found", [("Location", loc), ("Content-Length", "0")])
    return [b""]


def iso_week(d=None):
    d = d or date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_start(d=None):
    d = d or date.today()
    return d - timedelta(days=d.weekday())


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def amz_asin(asin):
    if not asin:
        return "—"
    return (f'<a href="{AMZ_DP}{urllib.parse.quote(asin)}" target="_blank" '
            f'rel="noopener" class="mono">{esc(asin)}</a>')


def amz_sku(sku):
    if not sku:
        return "—"
    return (f'<a href="{AMZ_SKU_SEARCH}{urllib.parse.quote(sku)}" target="_blank" '
            f'rel="noopener" class="mono">{esc(sku)}</a>')


def product_cell(r):
    """Product code + human title. The title is what makes 'seat cushion'
    findable; the code (SC-B) is not a word anybody searches for."""
    t = (r.get("title") or "").strip()
    return (f'<div class="prod">{esc(r.get("product") or "—")}</div>'
            + (f'<div class="title">{esc(t[:70])}{"…" if len(t) > 70 else ""}</div>' if t else ""))


# ---------------------------------------------------------------- filters
# One filter builder for the list, the count, the export and the pager, so a
# filter cannot work on screen and silently not apply to the CSV.

def build_where(q):
    where, args = [], []
    if q.get("q"):
        where.append("(v.review_text LIKE %s OR v.title LIKE %s OR v.sku LIKE %s OR v.product LIKE %s)")
        args += [f'%{q["q"]}%'] * 4
    if q.get("product"):
        where.append("(v.product LIKE %s OR v.title LIKE %s)")
        args += [f'%{q["product"]}%'] * 2
    if q.get("parent"):
        where.append("v.parent=%s"); args.append(q["parent"])
    if q.get("sku"):
        where.append("v.sku LIKE %s"); args.append(f'%{q["sku"]}%')
    if q.get("complaint"):
        where.append("v.final_complaint=%s"); args.append(q["complaint"])
    if q.get("source"):
        where.append("v.source=%s"); args.append(q["source"])
    if q.get("country"):
        where.append("v.country=%s"); args.append(q["country"])
    if q.get("month"):
        where.append("DATE_FORMAT(v.review_date,'%%Y-%%m')=%s"); args.append(q["month"])
    if q.get("from"):
        where.append("v.review_date >= %s"); args.append(q["from"])
    if q.get("to"):
        where.append("v.review_date <= %s"); args.append(q["to"])
    st = q.get("status") or ""
    if st == "new":
        where.append("v.human_validated=0")
    elif st == "decided":
        where.append("v.human_validated=1")
    elif st == "flagged":
        where.append("v.needs_review=1 AND v.human_validated=0")
    elif st == "newcode":
        where.append("v.is_new_code=1")
    return ("WHERE " + " AND ".join(where)) if where else "", args


def qstr(q, **over):
    d = {k: v for k, v in q.items() if v not in (None, "")}
    d.update(over)
    d = {k: v for k, v in d.items() if v not in (None, "")}
    return urllib.parse.urlencode(d)


def options(cur, sql, args=()):
    cur.execute(sql, args)
    return [(r["k"], r["n"]) for r in cur.fetchall()]


def select(name, vals, cur_val, any_label="Any"):
    opts = "".join(
        f'<option value="{esc(v)}" {"selected" if str(cur_val) == str(v) else ""}>{esc(v)} ({n:,})</option>'
        for v, n in vals)
    return f'<select name="{name}"><option value="">{any_label}</option>{opts}</select>'


def approved_codes(cur):
    cur.execute("SELECT code, category FROM complaint_codes WHERE approved=1 ORDER BY category, code")
    return cur.fetchall()


# ---------------------------------------------------------------- dashboard

def p_dashboard(cur, q, msg=""):
    cur.execute("SELECT COUNT(*) c FROM classifications"); total = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE human_validated=0 AND needs_review=1"); nr = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM proposed_codes WHERE status='pending'"); pend = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM corrections"); corr = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE final_complaint IS NULL"); nofinal = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(DISTINCT final_complaint) c FROM v_ai_complaints"); labels = cur.fetchone()["c"]
    cur.execute("SELECT MIN(review_date) a, MAX(review_date) b FROM classifications"); rng = cur.fetchone()
    cur.execute("""SELECT SUM(source='ncx') ncx, SUM(source='fba') fba, COUNT(*) n FROM v_ai_complaints""")
    src = cur.fetchone()

    cur.execute("""SELECT final_complaint complaint, COUNT(*) n FROM v_ai_complaints
                    WHERE final_complaint IS NOT NULL GROUP BY final_complaint ORDER BY n DESC LIMIT 14""")
    top = cur.fetchall()
    mx = max([r["n"] for r in top], default=1)
    bars = "".join(
        f'<tr><td><a href="/reviews?complaint={urllib.parse.quote(r["complaint"])}">{esc(r["complaint"])}</a></td>'
        f'<td class="mono num">{r["n"]:,}</td>'
        f'<td><div style="height:8px;border-radius:4px;background:var(--accent);opacity:.85;width:{int(r["n"]/mx*100)}%"></div></td></tr>'
        for r in top)

    body = f"""<h1>Dashboard</h1>
<p class="sub">Resolved labels across {total:,} reviews, {rng['a']} to {rng['b']}. Every count uses the resolved
complaint: the team's correction where one exists, the AI's label otherwise. {nofinal:,} review(s) are excluded
because they were marked not-a-complaint.</p>
<div class="grid">
  <div class="kpi"><div class="n">{total:,}</div><div class="l">Reviews classified</div>
    <div class="d">{src['ncx'] or 0:,} ncx · {src['fba'] or 0:,} fba</div></div>
  <div class="kpi"><div class="n">{nr:,}</div><div class="l">Need a human look</div>
    <div class="d"><a href="/reviews?status=flagged">unreviewed and flagged</a></div></div>
  <div class="kpi"><div class="n">{corr:,}</div><div class="l">Human decisions</div>
    <div class="d"><a href="/reviews?status=decided">view decided</a></div></div>
  <div class="kpi"><div class="n">{labels}</div><div class="l">Codes in use</div>
    <div class="d">distinct resolved labels</div></div>
  <div class="kpi"><div class="n">{pend}</div><div class="l">New codes to decide</div>
    <div class="d"><a href="/codes">review the queue</a></div></div>
</div>
<h2>Most common complaints</h2>
<div class="card"><table><thead><tr><th>Complaint</th><th class="num">Reviews</th><th style="width:38%"></th></tr></thead>
<tbody>{bars}</tbody></table></div>"""
    return page("Dashboard", body, "/", msg)


# ---------------------------------------------------------------- reviews list

def p_reviews(cur, q, msg=""):
    w, args = build_where(q)
    cur.execute(f"SELECT COUNT(*) c FROM v_ai_complaints v {w}", args)
    n = cur.fetchone()["c"]
    try:
        pg = max(1, int(q.get("page") or 1))
    except ValueError:
        pg = 1
    off = (pg - 1) * PAGE_SIZE
    cur.execute(f"""SELECT v.review_id, v.review_date, v.product, v.parent, v.sku, v.asin, v.title,
                           v.source, v.country, v.ai_complaint, v.final_complaint,
                           v.human_decision, v.human_validated, v.needs_review, v.is_new_code,
                           v.human_complaint,
                           LEFT(COALESCE(v.review_text,''),180) txt
                      FROM v_ai_complaints v {w}
                     ORDER BY v.review_date DESC, v.review_id DESC
                     LIMIT {PAGE_SIZE} OFFSET {off}""", args)
    rows = cur.fetchall()

    trs = []
    for r in rows:
        pill = ""
        if r["human_decision"] == "accepted":
            pill = '<span class="pill ok">confirmed</span>'
        elif r["human_decision"] == "corrected":
            pill = '<span class="pill ok">corrected</span>'
        elif r["human_decision"] == "rejected":
            pill = '<span class="pill bad">not a complaint</span>'
        elif r["is_new_code"]:
            pill = '<span class="pill new">new code</span>'
        elif r["needs_review"]:
            pill = '<span class="pill bad">needs check</span>'
        if r["human_decision"] == "corrected" and r["ai_complaint"]:
            label = f'<b>{esc(r["final_complaint"] or "")}</b> <span class="meta">(was {esc(r["ai_complaint"])})</span>'
        else:
            label = esc(r["final_complaint"] or "—")
        trs.append(f"""<tr>
          <td class="mono">{esc(r["review_date"])}</td>
          <td>{product_cell(r)}</td>
          <td class="mono">{esc(r["parent"] or "—")}</td>
          <td>{amz_sku(r["sku"])}</td>
          <td>{amz_asin(r["asin"])}</td>
          <td>{label}<br>{pill}</td>
          <td class="meta">{esc((r["txt"] or "")[:150])}</td>
          <td><a href="/review/{r['review_id']}">open</a></td></tr>""")

    products = options(cur, "SELECT product k, COUNT(*) n FROM v_ai_complaints WHERE product IS NOT NULL GROUP BY product ORDER BY n DESC")
    parents = options(cur, "SELECT parent k, COUNT(*) n FROM v_ai_complaints WHERE parent IS NOT NULL GROUP BY parent ORDER BY n DESC")
    sources = options(cur, "SELECT source k, COUNT(*) n FROM v_ai_complaints WHERE source IS NOT NULL GROUP BY source ORDER BY n DESC")
    countries = options(cur, "SELECT country k, COUNT(*) n FROM v_ai_complaints WHERE country IS NOT NULL GROUP BY country ORDER BY n DESC")
    complaints = options(cur, """SELECT final_complaint k, COUNT(*) n FROM v_ai_complaints
                                  WHERE final_complaint IS NOT NULL GROUP BY final_complaint ORDER BY n DESC""")

    statuses = "".join(f'<option value="{v}"{" selected" if q.get("status")==v else ""}>{lbl}</option>'
                       for v, lbl in [("new", "Unreviewed"), ("flagged", "Flagged, unreviewed"),
                                      ("decided", "Decided"), ("newcode", "New code")])
    body = f"""<h1>Reviews</h1>
<p class="sub">{n:,} match{'' if n == 1 else ''} · page {pg} of {max(1, (n + PAGE_SIZE - 1)//PAGE_SIZE)}
  · showing {len(rows)} · <a href="/export.csv?{esc(qstr(q))}">export CSV</a>
  · <a href="/export.csv?{esc(qstr(q, text='1'))}">export with text</a></p>
<form class="filters" method="get">
  <div><label>Search text or title</label><input name="q" value="{esc(q.get('q',''))}" placeholder="seat cushion"></div>
  <div><label>Product / title</label><input name="product" value="{esc(q.get('product',''))}" placeholder="RM-B or umbrella"></div>
  <div><label>Parent</label>{select('parent', parents, q.get('parent'))}</div>
  <div><label>SKU</label><input name="sku" value="{esc(q.get('sku',''))}" placeholder="RM-B-IR"></div>
  <div><label>Complaint</label>{select('complaint', complaints, q.get('complaint'))}</div>
  <div><label>Source</label>{select('source', sources, q.get('source'))}</div>
  <div><label>Country</label>{select('country', countries, q.get('country'))}</div>
  <div><label>Status</label><select name="status"><option value="">Any</option>{statuses}</select></div>
  <div><label>From</label><input type="date" name="from" value="{esc(q.get('from',''))}"></div>
  <div><label>To</label><input type="date" name="to" value="{esc(q.get('to',''))}"></div>
  <div><button class="primary">Filter</button></div>
  <div><a href="/reviews">clear</a></div>
</form>
<div class="card"><table><thead><tr><th>Date</th><th>Product</th><th>Parent</th><th>SKU</th><th>Asin</th>
<th>Complaint</th><th>Review</th><th></th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="8" class="empty">Nothing matches those filters.</td></tr>'}</tbody></table></div>
<div class="pager">{f'<a href="/reviews?{esc(qstr(q, page=pg-1))}">← previous</a>' if pg > 1 else ''}
  <span class="sp">page {pg}</span>
  {f'<a href="/reviews?{esc(qstr(q, page=pg+1))}">next →</a>' if off + PAGE_SIZE < n else ''}</div>"""
    return page("Reviews", body, "/reviews", msg)


# ---------------------------------------------------------------- one review

def p_review(cur, rid, q, msg="", err=False):
    cur.execute("SELECT * FROM v_ai_complaints v WHERE v.review_id=%s", (rid,))
    r = cur.fetchone()
    if not r:
        return page("Not found", '<div class="card"><div class="empty">No such review.</div></div>',
                    "/reviews", msg or "No such review.", True)
    cur.execute("SELECT * FROM corrections WHERE review_id=%s ORDER BY id DESC", (rid,))
    prior = cur.fetchall()
    by_cat = defaultdict(list)
    for c in approved_codes(cur):
        by_cat[c["category"] or "Uncategorised"].append(c["code"])
    groups = "".join(
        f'<optgroup label="{esc(cat)}">' + "".join(
            f'<option value="{esc(c)}"{" selected" if c == r["final_complaint"] else ""}>{esc(c)}</option>'
            for c in codes) + "</optgroup>"
        for cat, codes in sorted(by_cat.items()))

    prior_html = "".join(
        f'<div class="meta">· {esc(p["created_at"])} — <b>{esc(p["decision"])}</b>'
        f'{" to " + esc(p["new_complaint"]) if p["new_complaint"] else ""}'
        f'{" — " + esc(p["note"]) if p["note"] else ""}</div>' for p in prior) \
        or '<div class="meta">No decisions yet.</div>'

    body = f"""<h1>Review {esc(rid)}</h1>
<p class="sub">{product_cell(r)} · parent {esc(r["parent"] or "—")} · SKU {amz_sku(r["sku"])}
  · Asin {amz_asin(r["asin"])}<br>{esc(r["review_date"])} · {esc(r["source"] or "")} {esc(r["country"] or "")}
  {("· order " + esc(r["order_id"])) if r["order_id"] else ""}</p>
<div class="review">
  <div>
    <div class="text">{esc(r["review_text"] or "(no text)")}</div>
    <div style="margin-top:14px" class="meta">AI assigned <b>{esc(r["ai_complaint"] or "—")}</b>
      {("· category " + esc(r["ai_category"])) if r["ai_category"] else ""}
      {("· second choice " + esc(r["second_complaint"])) if r["second_complaint"] else ""}</div>
    <div style="margin-top:4px" class="meta">Reporting as <b>{esc(r["final_complaint"] or "—")}</b>
      {("· " + esc(r["final_category"])) if r["final_category"] else ""}</div>
    <div style="margin-top:16px"><b class="meta">History</b>{prior_html}</div>
  </div>
  <div class="card" style="padding:14px">
    <form method="post" action="/review/{rid}">
      <label>Confirm the AI label</label>
      <button class="primary" name="action" value="accept" style="width:100%;margin-bottom:14px">Confirm “{esc(r["ai_complaint"] or "—")}”</button>
      <label>Correct it</label>
      <select name="pick" style="width:100%;margin-bottom:8px"><option value="">choose a code…</option>{groups}</select>
      <input name="note" placeholder="optional note" style="width:100%;margin-bottom:8px">
      <button name="action" value="correct" style="width:100%">Save correction</button>
    </form>
    <form method="post" action="/review/{rid}" style="margin-top:10px">
      <input name="note" placeholder="why (optional)" style="width:100%;margin-bottom:8px">
      <button class="danger" name="action" value="reject" style="width:100%">Not a complaint / remove from reporting</button>
    </form>
  </div>
</div>"""
    return page(f"Review {rid}", body, "/reviews", msg, err)


def do_review(cur, rid, f):
    cur.execute("SELECT complaint FROM classifications WHERE review_id=%s", (rid,))
    row = cur.fetchone()
    if not row:
        # corrections.review_id has no foreign key, so a bad POST would otherwise
        # write a row pointing at nothing.
        return "No such review.", True
    ai = row["complaint"]
    act = f.get("action")
    note = (f.get("note") or "")[:500] or None
    if act == "accept":
        cur.execute("""INSERT INTO corrections (review_id, ai_complaint, new_complaint, decision, reviewed_by, note)
                       VALUES (%s,%s,NULL,'accepted',%s,%s)""", (rid, ai, REVIEWER_UID, note))
        return "Confirmed.", False
    if act == "correct":
        pick = (f.get("pick") or "").strip()
        if not pick:
            return "Pick a code first.", True
        cur.execute("SELECT code FROM complaint_codes WHERE code=%s AND approved=1", (pick,))
        if not cur.fetchone():
            return f"“{pick}” is not an approved code — approve it on the New codes page first.", True
        if pick == ai:
            cur.execute("""INSERT INTO corrections (review_id, ai_complaint, new_complaint, decision, reviewed_by, note)
                           VALUES (%s,%s,NULL,'accepted',%s,%s)""", (rid, ai, REVIEWER_UID, note))
            return "That is the AI label — recorded as confirmed.", False
        cur.execute("""INSERT INTO corrections (review_id, ai_complaint, new_complaint, decision, reviewed_by, note)
                       VALUES (%s,%s,%s,'corrected',%s,%s)""", (rid, ai, pick, REVIEWER_UID, note))
        return f"Corrected to {pick}.", False
    if act == "reject":
        cur.execute("""INSERT INTO corrections (review_id, ai_complaint, new_complaint, decision, reviewed_by, note)
                       VALUES (%s,%s,NULL,'rejected',%s,%s)""", (rid, ai, REVIEWER_UID, note))
        return "Marked as not a complaint; it leaves complaint reporting.", False
    return "Unknown action.", True


# ---------------------------------------------------------------- monthly

def p_monthly(cur, q, msg=""):
    where, args = ["complaint IS NOT NULL"], []
    if q.get("month"):
        where.append("month=%s"); args.append(q["month"])
    if q.get("product"):
        where.append("(product LIKE %s OR title LIKE %s)"); args += [f'%{q["product"]}%'] * 2
    if q.get("parent"):
        where.append("parent=%s"); args.append(q["parent"])
    if q.get("complaint"):
        where.append("complaint=%s"); args.append(q["complaint"])
    w = "WHERE " + " AND ".join(where)
    cur.execute(f"SELECT COUNT(*) c FROM v_ai_complaints_monthly {w}", args)
    n = cur.fetchone()["c"]
    cur.execute(f"""SELECT month, product, parent, sku, complaint, category, n,
                           pct_of_sku_month, n_human_validated, LEFT(COALESCE(title,''),70) title
                      FROM v_ai_complaints_monthly {w}
                     ORDER BY month DESC, n DESC LIMIT 400""", args)
    rows = cur.fetchall()
    trs = "".join(
        f"""<tr><td class="mono">{esc(r["month"])}</td>
        <td>{esc(r["product"] or "—")}<div class="title">{esc(r["title"] or "")}</div></td>
        <td class="mono">{esc(r["parent"] or "—")}</td>
        <td>{amz_sku(r["sku"])}</td>
        <td>{esc(r["complaint"])}</td><td class="meta">{esc(r["category"] or "")}</td>
        <td class="mono num">{r["n"]:,}</td><td class="mono num">{r["pct_of_sku_month"]}%</td>
        <td class="mono num">{int(r["n_human_validated"] or 0):,}</td></tr>""" for r in rows)

    months = options(cur, """SELECT month k, SUM(n) n FROM v_ai_complaints_monthly
                              WHERE month IS NOT NULL GROUP BY month ORDER BY month DESC""")
    parents = options(cur, "SELECT parent k, SUM(n) n FROM v_ai_complaints_monthly WHERE parent IS NOT NULL GROUP BY parent ORDER BY n DESC")
    complaints = options(cur, "SELECT complaint k, SUM(n) n FROM v_ai_complaints_monthly GROUP BY complaint ORDER BY n DESC")
    body = f"""<h1>Monthly</h1>
<p class="sub">The grain Chadia reports on: SKU × month × complaint, with the share of that SKU-month and how
many rows a human validated. {n:,} rows match{'' if n == 1 else ''} (showing {len(rows)}).
The review-grain rows behind any filter are on <a href="/reviews">Reviews</a>.</p>
<form class="filters" method="get">
  <div><label>Month</label>{select('month', months, q.get('month'))}</div>
  <div><label>Product / title</label><input name="product" value="{esc(q.get('product',''))}" placeholder="seat cushion"></div>
  <div><label>Parent</label>{select('parent', parents, q.get('parent'))}</div>
  <div><label>Complaint</label>{select('complaint', complaints, q.get('complaint'))}</div>
  <div><button class="primary">Filter</button></div>
  <div><a href="/monthly">clear</a></div>
</form>
<div class="card"><table><thead><tr><th>Month</th><th>Product</th><th>Parent</th><th>SKU</th>
<th>Complaint</th><th>Category</th><th class="num">Reviews</th><th class="num">% of SKU-month</th>
<th class="num">Human validated</th></tr></thead>
<tbody>{trs or '<tr><td colspan="9" class="empty">Nothing matches those filters.</td></tr>'}</tbody></table></div>"""
    return page("Monthly", body, "/monthly", msg)


# ---------------------------------------------------------------- codes

def p_codes(cur, q, msg=""):
    cur.execute("SELECT * FROM proposed_codes ORDER BY status='pending' DESC, n_rows DESC")
    rows = cur.fetchall()
    approved = approved_codes(cur)
    cats = sorted({c["category"] for c in approved if c["category"]})
    cur.execute("SELECT code, replacement FROM code_denials")
    denials = {r["code"]: r["replacement"] for r in cur.fetchall()}

    trs = []
    for r in rows:
        st = {"pending": '<span class="pill new">pending</span>',
              "approved": '<span class="pill ok">approved</span>',
              "denied": '<span class="pill bad">denied</span>'}[r["status"]]
        if r["status"] == "pending":
            act = f"""<form method="post" action="/codes" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
              <input type="hidden" name="code" value="{esc(r['code'])}">
              <select name="category" title="category for an approved code">
                <option value="">category…</option>
                {''.join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in cats)}</select>
              <button name="action" value="approved" class="primary">Approve</button>
              <select name="replacement" title="deny: the code she means instead">
                <option value="">remap to…</option>
                {''.join(f'<option value="{esc(c["code"])}">{esc(c["code"])}</option>' for c in approved)}</select>
              <button name="action" value="denied" class="danger">Deny</button>
            </form>"""
            if r["code"] in DENY_MAP:
                act += f'<div class="meta">default deny mapping: {esc(DENY_MAP[r["code"]])}</div>'
        else:
            act = st + (f'<div class="meta">remapped to {esc(denials.get(r["code"]))}</div>'
                        if denials.get(r["code"]) else "")
        trs.append(f"""<tr><td><b>{esc(r["code"])}</b></td><td class="mono num">{r["n_rows"]:,}</td>
          <td class="meta">{esc((r["example_review"] or "")[:150])}</td><td>{act}</td></tr>""")

    body = f"""<h1>New complaint codes</h1>
<p class="sub">Codes the classifier produced that the module does not have. <b>Approve</b> adds the code to the
vocabulary with a category — its reviews stop being “new” and the classifier can use it. <b>Deny</b> suppresses
the code and remaps its reviews to the one you would have chosen; a deny without a replacement is refused,
because it would silently drop those reviews out of every rollup.</p>
<div class="card"><table><thead><tr><th>Code</th><th class="num">Reviews</th><th>Example</th><th>Decision</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="4" class="empty">Nothing proposed.</td></tr>'}</tbody></table></div>"""
    return page("New codes", body, "/codes", msg)


def do_code(cur, f):
    code = (f.get("code") or "").strip()
    act = f.get("action") or ""
    if not code or act not in ("approved", "denied"):
        return "Bad request.", True
    cur.execute("SELECT status FROM proposed_codes WHERE code=%s", (code,))
    if not cur.fetchone():
        return f"“{code}” is not in the proposal queue.", True

    if act == "approved":
        cat = (f.get("category") or "").strip()
        cur.execute("SELECT category FROM complaint_codes WHERE code=%s", (code,))
        known = cur.fetchone()
        cat = cat or (known["category"] if known else None)
        if not cat:
            return "An approved code needs a category — pick one so it appears in the rollups.", True
        cur.execute("""UPDATE proposed_codes SET status='approved', decided_at=NOW(), decided_by=%s, note=%s
                        WHERE code=%s""", (REVIEWER_UID, cat, code))
        cur.execute("""INSERT INTO complaint_codes (code, category, source, approved)
                       VALUES (%s,%s,'chadia-approved',1)
                       ON DUPLICATE KEY UPDATE approved=1, source='chadia-approved',
                         category=COALESCE(VALUES(category), complaint_codes.category)""", (code, cat))
        cur.execute("""UPDATE classifications SET category=%s
                        WHERE complaint=%s AND (category IS NULL OR category='')""", (cat, code))
        cur.execute("UPDATE classifications SET is_new_code=0 WHERE complaint=%s", (code,))
        n = cur.rowcount
        return f"Approved “{code}” as {cat}. {max(n, 0):,} review(s) are no longer flagged as a new code.", False

    # deny ---------------------------------------------------------------
    repl = (f.get("replacement") or "").strip() or DENY_MAP.get(norm(code)) or ""
    if not repl:
        return ("A deny needs a replacement code: without one those reviews keep a label you rejected, "
                "or silently leave reporting. Pick “remap to…”.", True)
    cur.execute("SELECT category FROM complaint_codes WHERE code=%s AND approved=1", (repl,))
    tgt = cur.fetchone()
    if not tgt:
        return f"Replacement “{repl}” is not an approved code.", True
    cur.execute("""UPDATE proposed_codes SET status='denied', decided_at=NOW(), decided_by=%s, note=%s
                    WHERE code=%s""", (REVIEWER_UID, f"-> {repl}", code))
    cur.execute("""INSERT INTO complaint_codes (code, category, source, approved)
                   VALUES (%s,NULL,'chadia-denied',0)
                   ON DUPLICATE KEY UPDATE approved=0, source='chadia-denied'""", (code,))
    # code_denials is what sync_corrections.write_denials_json reads on the refresh
    # host, so this mapping also governs FUTURE classifications (cxvocab.canonical).
    cur.execute("""INSERT INTO code_denials (code, replacement, note, decided_by)
                   VALUES (%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE replacement=VALUES(replacement), note=VALUES(note),
                     decided_by=VALUES(decided_by), decided_at=NOW()""",
                (code, repl, (f.get("note") or "")[:500] or None, REVIEWER_UID))
    cur.execute("""UPDATE classifications SET complaint=%s, category=COALESCE(%s, category), is_new_code=0
                    WHERE complaint=%s""", (repl, tgt["category"], code))
    n = cur.rowcount
    return f"Denied “{code}” and remapped {max(n, 0):,} review(s) to “{repl}”.", False


# ---------------------------------------------------------------- quality

def ensure_sample(cur):
    """Draw this week's 45-row sample once, persist it, never re-draw.

    Stratified by resolved category with at least one row per category, seeded on
    the ISO week so the same week always gives the same sample. Persisted because
    the pool moves as corrections land: a replay would swap rows out from under
    the checker mid-week.
    """
    wk = iso_week()
    cur.execute("SELECT COUNT(*) n FROM weekly_samples WHERE sample_week=%s", (wk,))
    if cur.fetchone()["n"]:
        return wk
    cur.execute("""SELECT review_id, COALESCE(final_category,'Uncategorised') cat
                     FROM v_ai_complaints
                    WHERE review_date IS NOT NULL AND final_complaint IS NOT NULL""")
    pool = cur.fetchall()
    if not pool:
        return wk
    by = defaultdict(list)
    for r in pool:
        by[r["cat"]].append(r["review_id"])
    rng = random.Random(wk)
    total = len(pool)
    n = min(SAMPLE_N, total)
    q = {c: max(1, round(n * len(v) / total)) for c, v in by.items()}
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
        items = sorted(by[cat])
        out.extend(rng.sample(items, min(q.get(cat, 0), len(items))))
    rng.shuffle(out)
    cur.executemany("INSERT IGNORE INTO weekly_samples (sample_week, review_id) VALUES (%s,%s)",
                    [(wk, rid) for rid in out[:n]])
    return wk


def sample_stats(rows):
    acc = sum(1 for r in rows if r["your_call"] and norm(r["your_call"]) == norm(r["ai_complaint"]))
    cor = sum(1 for r in rows if r["your_call"] and NOT_A_COMPLAINT not in norm(r["your_call"])
              and norm(r["your_call"]) != norm(r["ai_complaint"]))
    rej = sum(1 for r in rows if r["your_call"] and NOT_A_COMPLAINT in norm(r["your_call"]))
    return acc, cor, rej, acc + cor     # rejected rows leave the denominator


def sample_rows(cur, wk):
    cur.execute("""SELECT s.review_id, s.your_call, s.note, s.decided_at,
                          v.review_date, v.sku, v.product, v.parent, v.asin, v.title,
                          v.ai_complaint, v.final_complaint, v.final_category,
                          LEFT(COALESCE(v.review_text,''),220) txt
                     FROM weekly_samples s
                     JOIN v_ai_complaints v ON v.review_id = s.review_id
                    WHERE s.sample_week=%s
                    ORDER BY s.review_id""", (wk,))
    return cur.fetchall()


def record_sample(cur, wk):
    """Roll the filled calls into quality_checks. Under 5 calls is progress, not a
    measurement: recording 100% on n=1 would put a meaningless point on the trend."""
    rows = sample_rows(cur, wk)
    acc, cor, rej, n = sample_stats(rows)
    if n < 5:
        return None
    pct = round(acc * 100.0 / n, 2)
    cur.execute("""INSERT INTO quality_checks (week_start, sample_n, agreement_pct, checked_by, sample_ref, note)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE sample_n=VALUES(sample_n),
                     agreement_pct=VALUES(agreement_pct), sample_ref=VALUES(sample_ref),
                     note=VALUES(note)""",
                (week_start(), n, pct, REVIEWER_UID, f"weekly-{wk}",
                 f"weekly random sample: {acc} agreed, {cor} corrected, {rej} removed "
                 f"(excluded from the denominator)"))
    return pct


def p_quality(cur, q, msg=""):
    wk = ensure_sample(cur)
    rows = sample_rows(cur, wk)
    acc, cor, rej, n = sample_stats(rows)
    pct = record_sample(cur, wk)
    filled = acc + cor + rej

    cur.execute("""SELECT week_start, sample_n, agreement_pct, note FROM quality_checks
                    WHERE sample_ref LIKE 'weekly-%%' ORDER BY week_start DESC""")
    checks = cur.fetchall()
    cur.execute("SELECT COUNT(*) n, SUM(human_validated) h FROM v_ai_complaints")
    s = cur.fetchone()
    coverage = float(s["h"] or 0) * 100.0 / max(1, s["n"])

    codes = [c["code"] for c in approved_codes(cur)]
    trs = []
    for i, r in enumerate(rows, 1):
        sel = (f'<select name="call_{r["review_id"]}" style="min-width:200px">'
               f'<option value="">—</option>'
               f'<option value="{NOT_A_COMPLAINT}"'
               f'{" selected" if norm(r["your_call"]) == NOT_A_COMPLAINT else ""}>not a complaint</option>'
               + "".join(
                   f'<option value="{esc(c)}"{" selected" if norm(r["your_call"]) == norm(c) else ""}>{esc(c)}</option>'
                   for c in codes)
               + "</select>")
        trs.append(f"""<tr><td class="mono">{i}</td>
          <td class="mono">{esc(r["review_date"])}</td>
          <td>{product_cell(r)}</td>
          <td>{amz_sku(r["sku"])}</td>
          <td class="meta">{esc((r["txt"] or "")[:170])} <a href="/review/{r['review_id']}">full</a></td>
          <td><b>{esc(r["ai_complaint"] or "—")}</b><div class="meta">{esc(r["final_category"] or "")}</div></td>
          <td>{sel}</td></tr>""")

    trend = "".join(
        f'<tr><td class="mono">{esc(c["week_start"])}</td><td class="mono num">{c["sample_n"]}</td>'
        f'<td class="mono num">{c["agreement_pct"]}%</td><td class="meta">{esc(c["note"] or "")}</td></tr>'
        for c in checks)

    if pct is not None:
        head, note = f"{pct}%", f"n={n} of {len(rows)} called"
    elif filled:
        head, note = f"{filled}/{len(rows)}", "called — agreement records at 5+"
    else:
        head, note = f"0/{len(rows)}", f"drawn for {wk} — no calls yet"

    body = f"""<h1>Quality</h1>
<p class="sub">Chadia's two-month weekly check: {SAMPLE_N} reviews drawn at random from the whole classified
corpus, stratified by complaint category so a small but important category is not swamped by FBA returns.
Record the call for each row — the agreement is computed from your calls, there is no percentage to type.
“Not a complaint” leaves the denominator, exactly as in the sheet workflow.</p>
<div class="grid">
  <div class="kpi"><div class="n">{head}</div><div class="l">This week ({esc(wk)})</div><div class="d">{note}</div></div>
  <div class="kpi"><div class="n">{len(checks)}</div><div class="l">Weekly checks logged</div><div class="d">taper record</div></div>
  <div class="kpi"><div class="n">{coverage:.1f}%</div><div class="l">Queue coverage</div>
    <div class="d">not accuracy — share of reviews a human has decided</div></div>
</div>
<h2>This week's sample</h2>
<form method="post" action="/quality">
<input type="hidden" name="week" value="{esc(wk)}">
<div class="card"><table><thead><tr><th>#</th><th>Date</th><th>Product</th><th>SKU</th>
<th>Review</th><th>AI said</th><th>Your call</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="7" class="empty">No sample drawn yet.</td></tr>'}</tbody></table></div>
<div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
  <button class="primary">Save calls</button>
  <span class="meta">{acc} agreed · {cor} corrected · {rej} not a complaint (excluded)</span>
</div></form>
<h2>Weekly trend</h2>
<div class="card"><table><thead><tr><th>Week</th><th class="num">Sample</th><th class="num">Agreement</th><th>Note</th></tr></thead>
<tbody>{trend or '<tr><td colspan="4" class="empty">No weekly check recorded yet.</td></tr>'}</tbody></table></div>"""
    return page("Quality", body, "/quality", msg)


def do_quality(cur, environ):
    f = parse_form_multi(environ)
    wk = (f.get("week") or [iso_week()])[0]
    cur.execute("SELECT review_id FROM weekly_samples WHERE sample_week=%s", (wk,))
    ids = [r["review_id"] for r in cur.fetchall()]
    if not ids:
        return "No sample for that week.", True
    n = 0
    for rid in ids:
        v = (f.get(f"call_{rid}") or [""])[0].strip()
        if not v:
            continue
        cur.execute("""UPDATE weekly_samples SET your_call=%s, decided_by=%s, decided_at=NOW()
                        WHERE sample_week=%s AND review_id=%s""", (v[:120], REVIEWER_UID, wk, rid))
        n += 1
    pct = record_sample(cur, wk)
    if pct is not None:
        return f"Saved {n} call(s); week {wk} agreement {pct}%.", False
    return f"Saved {n} call(s); agreement is recorded once at least 5 rows are called.", False


# ---------------------------------------------------------------- export

EXPORT_COLS = ["review_id", "review_date", "month", "product", "parent", "sku", "asin", "title",
               "source", "country", "ai_complaint", "ai_category", "final_complaint",
               "final_category", "human_decision", "human_complaint", "needs_review", "is_new_code"]


def export_csv(cur, q):
    w, args = build_where(q)
    cols = EXPORT_COLS + (["review_text"] if q.get("text") == "1" else [])
    cur.execute(f"""SELECT v.review_id, v.review_date, DATE_FORMAT(v.review_date,'%%Y-%%m') month,
                           v.product, v.parent, v.sku, v.asin, v.title, v.source, v.country,
                           v.ai_complaint, v.ai_category, v.final_complaint, v.final_category,
                           v.human_decision, v.human_complaint, v.needs_review, v.is_new_code,
                           v.review_text
                      FROM v_ai_complaints v {w}
                     ORDER BY v.review_date DESC, v.review_id DESC""", args)
    buf = io.StringIO()
    wtr = csv.writer(buf)
    wtr.writerow(cols)
    for r in cur.fetchall():
        wtr.writerow([r.get(c) for c in cols])
    return (buf.getvalue().encode("utf-8-sig"), "text/csv",
            f"attachment; filename=ai-complaints-{date.today().isoformat()}.csv")


# ---------------------------------------------------------------- dispatch

def authorised(environ):
    auth = environ.get("HTTP_AUTHORIZATION", "")
    if not auth.startswith("Basic ") or not APP_PASS:
        return False
    try:
        u, _, p = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
    except Exception:                                          # noqa: BLE001
        return False
    return secrets.compare_digest(u, APP_USER) and secrets.compare_digest(p, APP_PASS)


def app(environ, start_response):
    path = urllib.parse.unquote(environ.get("PATH_INFO", "/") or "/")
    method = environ.get("REQUEST_METHOD", "GET").upper()

    # Liveness: no auth, no data. Lets the deploy script tell "process up" from
    # "process up but the database is unreachable".
    if path == "/healthz":
        try:
            c = db(); cur = c.cursor(); cur.execute("SELECT 1"); cur.fetchone(); c.close()
            body, code = b"ok db=ok", "200 OK"
        except Exception as e:                                 # noqa: BLE001
            body, code = f"db error: {e}".encode(), "503 Service Unavailable"
        start_response(code, [("Content-Type", "text/plain; charset=utf-8")])
        return [body]

    if not authorised(environ):
        start_response("401 Unauthorized",
                       [("WWW-Authenticate", 'Basic realm="AI Complaint Validation"'),
                        ("Content-Type", "text/plain; charset=utf-8")])
        return [b"Authentication required"]

    q = parse_qs(environ)
    msg, err = q.get("msg", ""), q.get("err") == "1"
    conn = db()
    try:
        cur = conn.cursor()

        if method == "POST" and path.startswith("/review/"):
            try:
                rid = int(path.rsplit("/", 1)[1])
            except ValueError:
                start_response("404 Not Found", [("Content-Type", "text/plain")])
                return [b"bad id"]
            m, e = do_review(cur, rid, parse_form(environ))
            return redirect(start_response, f"/review/{rid}", m, e)

        if method == "POST" and path == "/codes":
            m, e = do_code(cur, parse_form(environ))
            return redirect(start_response, "/codes", m, e)

        if method == "POST" and path == "/quality":
            m, e = do_quality(cur, environ)
            return redirect(start_response, "/quality", m, e)

        if path == "/export.csv":
            body, ctype, disp = export_csv(cur, q)
            start_response("200 OK", [("Content-Type", ctype), ("Content-Disposition", disp)])
            return [body]

        if path == "/reviews":
            html = p_reviews(cur, q, msg)
        elif path.startswith("/review/"):
            try:
                rid = int(path.rsplit("/", 1)[1])
            except ValueError:
                start_response("404 Not Found", [("Content-Type", "text/plain")])
                return [b"bad id"]
            html = p_review(cur, rid, q, msg, err)
        elif path == "/codes":
            html = p_codes(cur, q, msg)
        elif path == "/quality":
            html = p_quality(cur, q, msg)
        elif path == "/monthly":
            html = p_monthly(cur, q, msg)
        else:
            html = p_dashboard(cur, q, msg)

        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]
    finally:
        conn.close()


if __name__ == "__main__":
    if not APP_PASS:
        sys.exit("APP_PASS not set — refusing to serve without a password")
    port = int(os.environ.get("APP_PORT", "8080"))
    host = os.environ.get("APP_HOST", "127.0.0.1")
    print(f"serving on {host}:{port} as {APP_USER}")
    make_server(host, port, app).serve_forever()
