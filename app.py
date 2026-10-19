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
  /reviews          filterable list: product family / product / title / sku, complaint,
                    source, country, parent, month, date range, status, checkability;
                    paginated — with the complaint MIX above the table (share per
                    complaint, a month-by-month line, and the change against the
                    preceding equal-length period, all from the same WHERE clause)
  /review/<id>      one review: confirm / correct / reject, in place - and revise a
                    decision that was already made (the new row supersedes, the old
                    one stays as history)
  /monthly          SKU x month x complaint rollup (the PowerBI grain), paginated
  /monthly.csv      the same rollup, same filters, every row
  /codes            proposed codes: approve (joins the vocabulary AND the classifier's
                    vocabulary file) or deny (durable; the replacement is required and
                    pre-selected when one was prescribed)
  /quality          the weekly 45-row random check: record a call per row
  /export.csv       the current filter as CSV (?text=1 adds the review text)
  /healthz          unauthenticated liveness probe for the deploy script

Product identity (product / parent / sku / asin / title) is enriched into
`classifications` by enrich_products.py on the refresh host, straight from
amzmanager.all_products. This app never connects to the source database.

Auth: HTTP Basic. APP_USERS="user:uid:password,..." is the real list, so every
decision is stamped with the person who made it; APP_USER/APP_PASS/APP_REVIEWER_UID
is the single-user form the first deployment used. TLS is terminated by the
reverse proxy in front of the box, so the credentials are never on the wire in
clear text.

This file is deployed on its own (deploy_on_box.sh installs exactly app.py into
/opt/ai-complaints/app), so it stays a single file: dependency-light, stdlib WSGI
+ pymysql, no imports from the rest of the project.
"""
import base64
import csv
import io
import math
import os
import random
import re
import secrets
import sys
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from wsgiref.simple_server import make_server

import pymysql


def _load_users():
    """username -> {pw, uid, name}.

    APP_USERS is authoritative: 'name:uid:password' comma-separated. The single
    APP_USER/APP_PASS/APP_REVIEWER_UID pair is the fallback that the existing box
    configuration already carries. A username with no uid still gets recorded by
    name in corrections.reviewed_by_name — the app never invents a uid.
    """
    raw = os.environ.get("APP_USERS", "").strip()
    users = {}
    if raw:
        for item in raw.split(","):
            if not item.strip():
                continue
            name, _, rest = item.strip().partition(":")
            uid, _, pw = rest.partition(":")
            if name and pw:
                users[name] = {"pw": pw, "uid": int(uid) if uid.strip().isdigit() else None,
                               "name": name}
    if not users:
        uid = os.environ.get("APP_REVIEWER_UID", "").strip()
        users[os.environ.get("APP_USER", "chadia")] = {
            "pw": os.environ.get("APP_PASS", ""),
            "uid": int(uid) if uid.isdigit() else None,
            "name": os.environ.get("APP_USER", "chadia"),
        }
    return users


USERS = _load_users()
APP_PASS = next((u["pw"] for u in USERS.values() if u["pw"]), "")
# Kept for the one-user configuration: the uid every write is stamped with when
# the signed-in account has none of its own.
DEFAULT_UID = next((u["uid"] for u in USERS.values() if u["uid"]), None)
SAMPLE_N = int(os.environ.get("APP_SAMPLE_N", "45"))
NOT_A_COMPLAINT = "not a complaint"
PAGE_SIZE = 100
SLA_HOURS = 24


# Deny mappings prescribed by the expert review, same table as
# sync_corrections.DENY_MAP. A deny must name a replacement — without one those
# reviews would silently drop out of every rollup — so the codes page PRE-SELECTS
# the mapped target and names it on the button. It is never applied invisibly:
# a deny posted without a replacement is refused, mapped code or not.
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
/* return-reason picklist values are chips; the customer's own words are prose.
   Mixing the two in one grey block is what made 10k rows look like noise. */
.chip{display:inline-block;padding:1px 8px;border-radius:5px;font-size:11.5px;font-weight:600;
  background:#eef2ff;border:1px solid #c7d2fe;color:#3730a3;margin:0 4px 3px 0;white-space:nowrap}
.chip.q{background:#f4f4f5;border-color:#e4e4e7;color:#52525b}
.txtbody{white-space:pre-wrap;font-size:14.5px;line-height:1.65}
.txtnone{color:var(--mut);font-size:13px;font-style:italic}
.txtwrap{display:flex;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid #f1f1f5}
.stale{display:inline-block;padding:2px 7px;border-radius:5px;background:#fef2f2;border:1px solid #fecaca;color:var(--bad);font-size:11px;font-weight:700}
.syncbar{margin:0 0 18px;padding:9px 13px;border-radius:8px;font-size:12.5px;border:1px solid;line-height:1.5}
.syncbar.ok{background:#f0fdf4;border-color:#bbf7d0;color:#166534}
.syncbar.warn{background:#fffbeb;border-color:#fde68a;color:#92400e}
.syncbar.bad{background:#fef2f2;border-color:#fecaca;color:#b42318}
/* the complaint mix: the answer to "what were people complaining about in this
   date range", above the row table so it is the first thing read. */
.mix{border:1px solid var(--line);border-radius:10px;background:var(--card);padding:14px 16px;margin-bottom:18px}
.mixhead{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:4px}
.mixhead h2{margin:0;font-size:15px}
.mixhead .r{margin-left:auto;color:var(--mut);font-size:12px}
.mixgrid{display:grid;grid-template-columns:1.35fr 1fr;gap:22px;align-items:start;margin-top:10px}
@media(max-width:1000px){.mixgrid{grid-template-columns:1fr}}
.mixtbl{width:100%;border-collapse:collapse;font-size:13px}
.mixtbl td{padding:4px 0;border:none;vertical-align:middle}
.mixtbl td.c{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:250px}
.mixtbl td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;padding-right:8px}
.mixtbl td.sh{text-align:right;color:var(--mut);font-variant-numeric:tabular-nums;white-space:nowrap;padding-right:8px}
.mixbar{height:9px;border-radius:4px;background:var(--accent);opacity:.85;min-width:1px}
.mixbar.blank{background:#9aa3af}
.mixbar.noise{background:#c2410c;opacity:.5}
.dl{font-size:11.5px;font-weight:650;white-space:nowrap;padding-left:6px}
.dl.up{color:var(--bad)}.dl.down{color:var(--ok)}.dl.flat{color:var(--mut)}.dl.new{color:var(--warn)}
.mixnote{color:var(--mut);font-size:12px;margin:10px 0 0;line-height:1.5}
.mixnote b{color:var(--ink)}
.txtog{margin:6px 0 0;font-size:12px;color:var(--mut)}
.txtog a{color:var(--mut);text-decoration:none;border-bottom:1px dotted currentColor;margin:0 1px}
.txtog a:hover{color:var(--ink)}
.txtog a.on{color:var(--ink);font-weight:650;border-bottom:1px solid var(--ink)}
.mixpanel .cap{color:var(--mut);font-size:12px;margin-top:6px}
.who{color:var(--mut);font-size:12px;margin-left:auto}
.decision{background:#fafafc;border:1px solid var(--line);border-radius:8px;padding:11px 13px;margin-bottom:12px;font-size:13px}
.decision b{font-weight:650}
"""


# ---------------------------------------------------------------- plumbing

def db():
    return pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def page(title, body, active="", msg="", err=False, ctx=None):
    ctx = ctx or {}
    who = ctx.get("who")
    nav = "".join(
        f'<a href="{u}" class="{"on" if active == u else ""}">{n}</a>'
        for u, n in [("/", "Dashboard"), ("/reviews", "Reviews"), ("/monthly", "Monthly"),
                     ("/codes", "New codes"), ("/quality", "Quality")])
    flash = f'<div class="flash{" err" if err else ""}">{esc(msg)}</div>' if msg else ""
    who_html = (f'<span class="who">signed in as <b>{esc(who["name"])}</b>'
                f'{" (uid " + str(who["uid"]) + ")" if who.get("uid") else ""}</span>') if who else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · AI Complaint Validation</title><style>{CSS}</style></head>
<body><header><span class="brand">AI Complaint Validation</span><nav>{nav}</nav>{who_html}</header>
<main>{ctx.get("banner", "")}{flash}{body}</main></body></html>"""


# ---------------------------------------------------------------- review text
# Review text in this corpus is an Amazon return form, not a review: up to six
# pipe-separated segments where the first one or two are picklist values the
# customer chose ('Changed Mind', 'My needs changed') and the rest is whatever
# they typed. 61% of rows contain a pipe and 39% contain no free text at all.
# Rendering both kinds in one grey block is why the queue read as noise.
PROSE_MIN = 25          # keep in step with apply_views.HAS_PROSE


def text_parts(t):
    """-> (chips, prose). Mirrors the has_prose rule in apply_views.py exactly."""
    t = (t or "").strip()
    if not t:
        return [], []
    segs = [s.strip() for s in t.split("|") if s.strip()]
    if len(segs) == 1:
        return ([], segs) if len(segs[0]) >= 8 else (segs, [])
    chips, prose = [], []
    for s in segs:
        (prose if len(s) >= PROSE_MIN and " " in s else chips).append(s)
    seen, uniq = set(), []           # the comment box often repeats the picklist answer
    for s in prose:
        if s.lower() in seen:
            continue
        seen.add(s.lower())
        uniq.append(s)
    return chips, uniq


def chips_html(chips, limit=None):
    if not chips:
        return ""
    shown = chips[:limit] if limit else chips
    more = f'<span class="chip q">+{len(chips) - len(shown)}</span>' if limit and len(chips) > limit else ""
    return "".join(f'<span class="chip">{esc(c)}</span>' for c in shown) + more


def text_html(t):
    """Full text for the detail view: chips above, the customer's words below."""
    chips, prose = text_parts(t)
    out = f'<div class="txtwrap">{chips_html(chips)}</div>' if chips else ""
    if prose:
        out += f'<div class="txtbody">{esc(" ".join(prose))}</div>'
    elif chips:
        out += '<div class="txtnone">No customer comment — the return reason above is the only text.</div>'
    else:
        out += '<div class="txtnone">No review text on this row.</div>'
    return out


def text_snippet(t, n=140):
    """One-line version for the list: chips, then the prose (or 'code only')."""
    chips, prose = text_parts(t)
    head = chips_html(chips, limit=2)
    if prose:
        s = " ".join(prose)
        body = f'<span class="meta">{esc(s[:n])}{"…" if len(s) > n else ""}</span>'
    else:
        body = '<span class="meta" style="color:#9d174d">code only — nothing to check</span>'
    return head + (f'<br>{body}' if head else body)


# ---------------------------------------------------------------- refresh health
# Mirrors refresh_status.py. Duplicated on purpose: this file is deployed alone,
# and a missing import would silently drop the one signal that says the numbers
# are stale.
def refresh_status(cur):
    cur.execute(f"""SELECT finished_at, n_errors, note FROM runs
                     WHERE run_id LIKE 'refresh-%%' AND finished_at IS NOT NULL
                     ORDER BY finished_at DESC LIMIT 1""")
    last = cur.fetchone()
    cur.execute(f"""SELECT finished_at FROM runs
                     WHERE run_id LIKE 'refresh-%%' AND n_errors=0 AND finished_at IS NOT NULL
                     ORDER BY finished_at DESC LIMIT 1""")
    ok = cur.fetchone()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    last_ok = ok["finished_at"] if ok else None
    age = (now - last_ok).total_seconds() / 3600.0 if last_ok else None
    return {"last_ok": last_ok, "last_run": last["finished_at"] if last else None,
            "failed": bool(last and (last["n_errors"] or 0) > 0),
            "note": (last["note"] if last else "") or "", "age_h": age,
            "ok": bool(age is not None and age <= SLA_HOURS)}


def ago(h):
    if h is None:
        return "never"
    if h < 1:
        return f"{int(h * 60)} min ago"
    return f"{h:.0f} h ago" if h < 48 else f"{h / 24:.0f} days ago"


def banner_html(st):
    if st["last_ok"] is None:
        return ('<div class="syncbar bad"><b>No successful refresh recorded.</b> '
                'Treat every figure on this page as unverified until the refresh runs.</div>')
    stamp = st["last_ok"].strftime("%d %b %Y %H:%M UTC")
    if st["ok"]:
        if st["failed"]:
            return (f'<div class="syncbar warn">Data refreshed <b>{stamp}</b> ({ago(st["age_h"])}). '
                    f'<b>Latest attempt failed</b>{": " + esc(st["note"]) if st["note"] else ""} — '
                    f'the figures are from the last good run.</div>')
        return f'<div class="syncbar ok">Data refreshed <b>{stamp}</b> ({ago(st["age_h"])}).</div>'
    return ('<div class="syncbar bad"><b>Data is stale</b> — last successful refresh '
            f'{stamp}, {ago(st["age_h"])}. This page should refresh every 30 minutes. '
            'Do not quote these figures until it is fixed.'
            + (f' Latest error: {esc(st["note"])}' if st["failed"] and st["note"] else "")
            + '</div>')


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


# Review Deletion is the rule's verdict on a duplicate / empty review, not
# something a customer complained about. It stays in the table and the export
# (so nothing disappears) but not in any distribution.
PROCESS_NOISE = "Review Deletion"
# Rows with no resolved complaint (marked not a complaint, or nothing assigned).
# The mix shows them as their own bucket so its counts still reconcile to the
# table's match count instead of quietly summing short.
NO_CODE = "(no complaint assigned)"
# Display-only renames: the stored value never changes, so the pivot, the
# export and the training data keep one vocabulary.
DISPLAY_LABEL = {"vague complaint": "Unspecified"}


def display_label(c):
    return DISPLAY_LABEL.get(norm(c), c)



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
    if q.get("family"):
        # Curated product family (apply_views.PRODUCT_FAMILY): one value covers
        # every market's SKU for the same product, which a title search cannot.
        where.append("v.product_family=%s"); args.append(q["family"])
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
    txt = q.get("tx") or ""
    if txt == "codeonly":
        # No free text: only the return-reason picklist, so there is nothing to
        # read the label against. Filtering these out of the working list is the
        # difference between a queue of 2,104 and a queue of 1,727.
        where.append("v.has_prose=0")
    elif txt == "prose":
        where.append("v.has_prose=1")
    if q.get("dup") == "1":
        # One row per repeated review (same text, SKU and date). Never applied
        # unless asked for: the default counts every row and discloses the
        # repeats, because a silent dedupe would change every number on the page
        # without telling the reader.
        where.append("v.dup_rank=1")
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
    """Dropdown. vals is (value, count) or (value, label, count) — the label is
    display-only, the value is what the filter compares against, so a renamed
    label ('Unspecified') can never be submitted as an unknown value."""
    opts = ""
    for item in vals:
        v, lbl, n = item if len(item) == 3 else (item[0], item[0], item[1])
        opts += (f'<option value="{esc(v)}" {"selected" if str(cur_val) == str(v) else ""}>'
                 f'{esc(lbl)} ({n:,})</option>')
    return f'<select name="{name}"><option value="">{any_label}</option>{opts}</select>'


def approved_codes(cur):
    cur.execute("SELECT code, category FROM complaint_codes WHERE approved=1 ORDER BY category, code")
    return cur.fetchall()


# ------------------------------------------------------- approved -> vocabulary
# cxvocab builds the classifier's menu from her two audit CSVs plus
# data/vocab_additions.csv. Approving a code only wrote complaint_codes, which the
# classifier never reads, so "the classifier can use it" was false: an approved
# code could never be emitted. This writes the same approval into the file the
# classifier loads, idempotently, so the next batch sees it.
VOCAB_HEADER = ["Complaint Types", "Category"]


def vocab_path():
    """Where data/vocab_additions.csv lives on this host, or None.

    CX_VOCAB_ADDITIONS overrides. Otherwise the app's own directory (deployed next
    to it) and the project default are tried — and a path is only returned when
    its directory exists, so a box without the data/ tree reports the problem
    instead of silently writing into a directory nobody reads.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.environ.get("CX_VOCAB_ADDITIONS", "").strip(),
             os.path.join(here, "data", "vocab_additions.csv"),
             os.path.join(here, "..", "data", "vocab_additions.csv"),
             "/opt/data/ai-complaint-validation/data/vocab_additions.csv"]
    for p in cands:
        if p and os.path.isdir(os.path.dirname(os.path.abspath(p))):
            return os.path.abspath(p)
    return None


def vocab_upsert(code, category):
    """Add or refresh one approved code in data/vocab_additions.csv.

    Idempotent: the same (code, category) twice rewrites the same file. Returns
    (path, error) — error is a short reason when the file could not be written.
    """
    path = vocab_path()
    if not path:
        return None, "no data/ directory beside the app"
    rows = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.reader(fh) if r]
    except OSError:
        rows = []
    if rows and norm(rows[0][0]) == norm(VOCAB_HEADER[0]):
        head, body = rows[0], rows[1:]
    else:
        head, body = VOCAB_HEADER, rows
    out, found = [], False
    for r in body:
        if norm(r[0]) == norm(code):
            out.append([code, category]); found = True
        else:
            out.append(r)
    if not found:
        out.append([code, category])
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(head)
            w.writerows(out)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None, f"could not write {path}: {e}"
    return path, None


# ---------------------------------------------------------------- complaint mix
# The question the whole project exists to answer: "if we have an increased
# amount of returns on our seat cushion I need to be able to see during specific
# date ranges what people were complaining about". A row table cannot answer it:
# the Complaint control is a filter, not a breakdown, and a filtered count of
# 1,248 says nothing about WHICH complaints grew.
#
# The Mix panel is built from the SAME build_where() as the table below it, so
# its counts reconcile to the table's match count by construction and the two
# can never disagree. It adds the two things a breakdown needs and a table does
# not give: a month-by-month series, and a comparison against the preceding
# equal-length period, so "an increased amount" is visible instead of remembered.

def _d(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _month_bounds(m):
    try:
        y, mo = int(m[:4]), int(m[5:7])
        first = date(y, mo, 1)
    except (TypeError, ValueError, IndexError):
        return None, None
    nxt = date(y + (mo == 12), (mo % 12) + 1, 1)
    return first, nxt - timedelta(days=1)


def _corpus_range(cur):
    cur.execute("SELECT MIN(review_date) a, MAX(review_date) b FROM v_ai_complaints")
    r = cur.fetchone()
    return r["a"], r["b"]


def mix_window(q, plot, lo, hi):
    """(start, end, basis) for the change column.

    `plot` is the TRIMMED, contiguous month list the series draws — never the raw
    list of months present. A filter with one stray 2025-07 row used to stretch
    the default window over 396 days for a two-month chart, which suppressed
    every arrow as thin; the window now comes from the months that are actually
    plotted, and the last three of those are contiguous by construction.

    Explicit dates win; a month filter means that month; neither means the last
    three months plotted here, and the page says so. A delta against a window the
    reader has to guess is not evidence.
    """
    f, t = _d(q.get("from")), _d(q.get("to"))
    if (f or t) and lo and hi:
        return (f or lo), (t or hi), "the dates you filtered"
    if q.get("month"):
        a, b = _month_bounds(q["month"])
        if a:
            return a, b, f"month {q['month']}"
    keys = [m for m, _ in plot if m][-3:]
    if keys:
        a, _ = _month_bounds(keys[0])
        _, b = _month_bounds(keys[-1])
        if a and b:
            return a, b, ("last %d month%s here" % (len(keys), "" if len(keys) == 1 else "s"))
    return None, None, ""


def prior_window(f, t):
    """The equal-length period immediately before (f, t], same number of days."""
    span = (t - f).days + 1
    pt = f - timedelta(days=1)
    return pt - timedelta(days=span - 1), pt, span


def _pct(cur_n, prev_n, cur_days, prev_days):
    """Percentage change between two RATES, or None when a rate is impossible."""
    if not cur_days or not prev_days or not prev_n:
        return None
    return ((cur_n / cur_days) / (prev_n / prev_days) - 1) * 100.0


def _delta_cell(cur_n, prev_n, cur_days, prev_days, title=""):
    """One change cell, as a change in RATE PER DAY WITH DATA.

    Both counts MUST come from their own window, and both windows MUST be
    divided by the days they actually hold data for. The corpus starts
    2026-06-01, so "the 80 days before 2026-08-01" holds only 61 dates with data:
    dividing by 80 made a 61-day baseline look like an 80-day one and inflated
    every default-page percentage (+86% displayed where the rates say +41.6%),
    and flipped the sign on three codes (Too Big displayed +9%, rates say -17.1%).
    """
    if prev_n is None:
        return ""
    tt = f' title="{esc(title)}"' if title else ""
    if not cur_days or not prev_days:
        return f'<span class="dl flat"{tt}>—</span>' if (cur_n or prev_n) else ""
    if not prev_n:
        return (f'<span class="dl new"{tt}>new</span>') if cur_n else ""
    pct = _pct(cur_n, prev_n, cur_days, prev_days)
    if pct is None:
        return ""
    if abs(pct) < 1:
        return f'<span class="dl flat"{tt}>±0%</span>'
    cls = "up" if pct > 0 else "down"
    arrow = "▲" if pct > 0 else "▼"
    return f'<span class="dl {cls}"{tt}>{arrow}{abs(pct):.0f}%</span>'


def _trim_months(months):
    """The contiguous run of months holding >= 99.5% of the rows, plus the rest.

    The corpus is 99.9% Jun-Oct 2026 with a handful of stragglers back to 2023.
    Plotting 19 months makes the line read as "the data is broken" and hides the
    shape of the months she actually works with. Same rule as the static mirror's
    coverage_text; nothing is dropped silently — the caption reports the rest.
    """
    if len(months) <= 3:
        return months, 0
    total = sum(n for _, n in months)
    best, acc, start = None, 0, 0
    for i, (_, n) in enumerate(months):
        acc += n
        while acc - months[start][1] >= total * 0.995 and start < i:
            acc -= months[start][1]
            start += 1
        if acc >= total * 0.995 and (best is None or (i - start) < (best[1] - best[0])):
            best = (start, i)
    if not best:
        return months, 0
    a, b = best
    kept = months[a:b + 1]
    return kept, total - sum(n for _, n in kept)


MN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SUF = {1: "st", 2: "nd", 3: "rd"}


def _ord(d):
    """19 -> '19th'. A caption that says 'to 19' reads like a typo."""
    if 10 <= d % 100 <= 20:
        return f"{d}th"
    return f"{d}{SUF.get(d % 10, 'th')}"


def _month_days(m):
    _, b = _month_bounds(m)
    return b.day if b else 0


def _mix_svg(points, partial_day=None):
    """One line, one point per month, server-side SVG — no JS dependency.

    points: [(month 'YYYY-MM', n)]. data-points carries the count so a test can
    assert the series length without parsing the path.

    A month that is still running is drawn as a DASHED last segment and labelled
    '(to the 19th)'. Plotting the 19 days of October as a whole month made a flat
    daily rate read as a 36% fall — the most expensive kind of wrong, because it
    is the shape she quotes.
    """
    if not points:
        return ""
    W, H, L, R, T, B = 520, 172, 36, 14, 14, 26
    iw, ih = W - L - R, H - T - B
    mx = max(n for _, n in points) or 1
    n = len(points)
    xs = [L + (iw / 2 if n == 1 else i * iw / (n - 1)) for i in range(n)]
    ys = [T + ih - (v / mx) * ih for _, v in points]
    grid = "".join(
        f'<line x1="{L}" x2="{W - R}" y1="{T + ih * (1 - k / 4):.1f}" '
        f'y2="{T + ih * (1 - k / 4):.1f}" stroke="#eef0f3"/>'
        for k in range(5))
    lab = (f'<text x="{L - 6}" y="{T + 4}" text-anchor="end" font-size="9.5" fill="#9ca3af">{mx:,}</text>'
           f'<text x="{L - 6}" y="{T + ih + 3}" text-anchor="end" font-size="9.5" fill="#9ca3af">0</text>')
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f'<polygon points="{L},{T + ih:.1f} {pts} {xs[-1]:.1f},{T + ih:.1f}" fill="#2b5cff" opacity=".07"/>'
    part = bool(partial_day) and n > 1
    solid_n = n - 1 if part else n
    solid = " ".join(f"{xs[i]:.1f},{ys[i]:.1f}" for i in range(solid_n))
    line = (f'<polyline points="{solid}" fill="none" stroke="#2b5cff" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>')
    if part:
        line += (f'<polyline points="{xs[-2]:.1f},{ys[-2]:.1f} {xs[-1]:.1f},{ys[-1]:.1f}" '
                 f'fill="none" stroke="#2b5cff" stroke-width="2" stroke-dasharray="4 3" '
                 f'stroke-linecap="round"/>')
    dots, labels = "", ""
    step = 1 if n <= 8 else max(1, n // 8)
    for i, (m, v) in enumerate(points):
        x, y = xs[i], ys[i]
        dash = (' stroke-dasharray="2.2 1.8"' if part and i == n - 1 else "")
        title = (f'{m} (to the {_ord(partial_day)}): {v:,}' if part and i == n - 1 else f"{m}: {v:,}")
        dots += (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="#fff" stroke="#2b5cff" '
                 f'stroke-width="2"{dash}><title>{esc(title)}</title></circle>')
        if i % step == 0 or i == n - 1:
            try:
                mlab = MN[int(m[5:7]) - 1] + ("" if m[:4] == points[0][0][:4] else " " + m[2:4])
            except (ValueError, IndexError):
                mlab = m
            if part and i == n - 1:
                mlab = f"{mlab} (to the {_ord(partial_day)})"
                labels += (f'<text x="{x:.1f}" y="{H - 8}" text-anchor="end" font-size="9.5" '
                           f'fill="#9ca3af">{esc(mlab)}</text>')
            else:
                labels += (f'<text x="{x:.1f}" y="{H - 8}" text-anchor="middle" font-size="9.5" '
                           f'fill="#9ca3af">{esc(mlab)}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Complaints per month" '
            f'data-points="{n}" data-partial="{partial_day or 0}" '
            f'style="width:100%;height:auto;display:block">{grid}{lab}{area}{line}{dots}{labels}</svg>')


def mix_block(cur, q, n):
    """The complaint mix for the current filter: bars, month series, change.

    Three numbers in this panel used to be counted over different row sets than
    the label beside them claimed: the change column compared an all-time count
    to a windowed one, the series counted rows the distribution excludes, and a
    half-finished month was plotted as a whole one. Every count below is now
    taken over the set its heading names.

    The change column is a change in RATE PER DAY WITH DATA (W1): both windows
    are divided by the number of distinct review dates they hold, because the
    corpus starts 2026-06-01 and a calendar-day denominator makes a 61-day
    baseline look like an 80-day one.
    """
    w, args = build_where(q)
    cur.execute(f"""SELECT COALESCE(v.final_complaint, %s) c, COUNT(*) n,
                           SUM(v.has_prose=0) no_prose, SUM(v.human_validated=1) hv,
                           SUM(v.dup_rank > 1) dupes
                      FROM v_ai_complaints v {w}
                     GROUP BY c ORDER BY n DESC""", [NO_CODE] + args)
    dist = cur.fetchall()

    # The series must count what the distribution counts: final_complaint set and
    # not Review Deletion. Otherwise the two halves of one panel disagree
    # (seat-cushion Jul-Sep: 1,204 plotted vs 1,186 in v_ai_complaints_monthly,
    # the gap being 17 Review Deletion + 1 unassigned).
    ws = (w + " AND " if w else "WHERE ") + (
        "v.review_date IS NOT NULL AND v.final_complaint IS NOT NULL "
        "AND v.final_complaint <> %s")
    cur.execute(f"""SELECT DATE_FORMAT(v.review_date,'%%Y-%%m') m, COUNT(*) n,
                           COUNT(DISTINCT v.review_date) d
                      FROM v_ai_complaints v {ws}
                     GROUP BY DATE_FORMAT(v.review_date,'%%Y-%%m') ORDER BY 1""",
                args + [PROCESS_NOISE])
    mrows = cur.fetchall()
    months = [(r["m"], int(r["n"])) for r in mrows if r["m"]]
    mdays = {r["m"]: int(r["d"]) for r in mrows if r["m"]}
    plot, omitted = _trim_months(months)

    # W2: the range is the FILTERED range, not the corpus range. A filter whose
    # data stops on 2026-07-16 used to be plotted with July drawn as a complete
    # month (and every mid-month filter lost its dashed last point), because the
    # partial-month test compared the filter to MAX(review_date) over the whole
    # table.
    cur.execute(f"""SELECT MIN(v.review_date) lo, MAX(v.review_date) hi
                      FROM v_ai_complaints v {ws}""", args + [PROCESS_NOISE])
    rng = cur.fetchone()
    lo, hi = rng["lo"], rng["hi"]
    corpus_hi = _corpus_range(cur)[1]

    noise = blank = 0
    bars, nocomment, n_no, n_hv, n_dup = [], {}, 0, 0, 0
    for r in dist:
        c, k = r["c"], int(r["n"])
        n_no += int(r["no_prose"] or 0)
        n_hv += int(r["hv"] or 0)
        n_dup += int(r["dupes"] or 0)
        if c == NO_CODE:
            blank = k
        elif c == PROCESS_NOISE:
            noise = k
        else:
            bars.append((c, k))
            nocomment[c] = int(r["no_prose"] or 0)
    bars_total = sum(k for _, k in bars)

    # W3: the window is built from the TRIMMED contiguous months the chart draws.
    # Fed the raw month list, one stray 2025-07 row stretched the default window
    # to 396 days for a two-month chart and suppressed every arrow as thin.
    f, t, basis = mix_window(q, plot, lo, hi)
    clamped = False
    if f and t and hi and t > hi:
        # A window that ends after the data does not exist: it inflates the
        # current side against a complete prior window and dates the page in the
        # future ("comparing 2026-08-01 to 2026-10-31" with 12 days unread).
        t, clamped = hi, True
    if f and t and f > t:
        f = t

    def _win(a, b):
        qq = {k: v for k, v in q.items() if k not in ("month", "from", "to")}
        qq["from"], qq["to"] = a.isoformat(), b.isoformat()
        return build_where(qq)

    def _window_dist(a, b):
        ww, aa = _win(a, b)
        cur.execute(f"""SELECT COALESCE(v.final_complaint, %s) c, COUNT(*) n
                          FROM v_ai_complaints v {ww} GROUP BY c""", [NO_CODE] + aa)
        return {r["c"]: int(r["n"]) for r in cur.fetchall()}

    def _window_days(a, b):
        """Distinct review_date values in the window, under the SAME filter.

        This is the denominator of the change column. Counting calendar days
        instead credits a window with days it has no data for: 2026-05-13 to
        07-31 is 80 days but only 61 dates with data.
        """
        ww, aa = _win(a, b)
        cur.execute(f"SELECT COUNT(DISTINCT v.review_date) d FROM v_ai_complaints v {ww}", aa)
        return int(cur.fetchone()["d"] or 0)

    curw, prior, pf, pt, span = {}, {}, None, None, 0
    cur_days = prior_days = 0
    prior_total = cur_total = 0
    if f and t:
        pf, pt, span = prior_window(f, t)
        curw = _window_dist(f, t)
        prior = _window_dist(pf, pt)
        cur_days = _window_days(f, t)
        prior_days = _window_days(pf, pt)
        cur_total, prior_total = sum(curw.values()), sum(prior.values())
        if not prior_total:
            prior = {}          # no prior data: show the period, not a wall of "new"

    # A percentage needs a denominator worth dividing by. Below 100 prior reviews
    # or half the current side, the prior window is a coverage artefact and any
    # arrow is noise dressed as a trend (64 vs 1,204 printed ▲10200%).
    thin = bool(prior) and (prior_total < 100 or prior_total < 0.5 * cur_total)
    # W1's second half: a prior window with materially FEWER days of data than
    # the current one is the corpus-start artefact. The rate basis above is the
    # fix for the arithmetic; this flag makes the page say so out loud, because a
    # reader who assumes two equal calendar windows would read the rate as a
    # count change (that assumption is what produced +86% instead of +41.6%).
    daygap = bool(prior) and bool(prior_days) and bool(cur_days) and prior_days < 0.9 * cur_days
    show_delta = bool(prior) and not thin

    if not n:
        return ('<div class="mix"><div class="mixhead"><h2>Complaint mix</h2></div>'
                '<p class="mixnote">Nothing matches those filters.</p></div>')

    mx = max([k for _, k in bars], default=1) or 1
    top = bars[:12]
    # W6: the same floor the riser uses. A row of 6 against 1 is not "▲500%"; it
    # is six reviews. Below the floor the cell says how small the counts are and
    # prints no percentage at all.
    floor = max(20, int(math.ceil(0.01 * cur_total))) if cur_total else 20
    rows = []
    for c, k in top:
        if show_delta:
            cv, pv = curw.get(c, 0), prior.get(c, 0)
            if max(cv, pv) < floor:
                ttl = (f"{cv:,} here vs {pv:,} before — below the {floor:,}-review floor, "
                       f"too small to read as a trend")
                d = f'<span class="meta" title="{esc(ttl)}">n&lt;{floor:,}</span>'
            else:
                d = _delta_cell(cv, pv, cur_days, prior_days,
                                f"{cv:,} over {cur_days:,} days with data "
                                f"({cv / max(cur_days, 1):.2f}/day) vs {pv:,} over "
                                f"{prior_days:,} days ({pv / max(prior_days, 1):.2f}/day)")
        elif f and t:
            d = '<span class="dl flat">—</span>'
        else:
            d = ""
        win = (f'<div class="meta">all-time · {curw.get(c, 0):,} in window</div>' if f and t else "")
        nocom = (f'<div class="meta">{nocomment.get(c, 0):,} no comment</div>'
                 if nocomment.get(c, 0) else "")
        rows.append(
            f'<tr><td class="c" title="{esc(c)}">{esc(display_label(c))}</td>'
            f'<td class="n">{k:,}{win}{nocom}</td><td class="sh">{k * 100.0 / n:.1f}%</td>'
            f'<td style="width:40%"><div class="mixbar" style="width:{max(2, int(k * 100.0 / mx))}%"></div></td>'
            f'<td style="width:78px">{d}</td></tr>')
    rest = bars[12:]
    if rest:
        rn = sum(k for _, k in rest)
        rows.append(f'<tr><td class="c" style="color:var(--mut)">{len(rest)} other complaints</td>'
                    f'<td class="n">{rn:,}</td><td class="sh">{rn * 100.0 / n:.1f}%</td>'
                    f'<td><div class="mixbar" style="opacity:.35;width:{max(2, int(rn * 100.0 / mx))}%"></div></td><td></td></tr>')
    if blank:
        rows.append(f'<tr><td class="c" style="color:var(--mut)">{esc(NO_CODE)}</td>'
                    f'<td class="n">{blank:,}</td><td class="sh">{blank * 100.0 / n:.1f}%</td>'
                    f'<td><div class="mixbar blank" style="width:{max(2, int(blank * 100.0 / mx))}%"></div></td><td></td></tr>')
    if noise:
        rows.append(f'<tr><td class="c" style="color:var(--mut)">{esc(PROCESS_NOISE)} '
                    f'<span style="font-weight:400">· process noise, excluded</span></td>'
                    f'<td class="n">{noise:,}</td><td class="sh">{noise * 100.0 / n:.1f}%</td>'
                    f'<td><div class="mixbar noise" style="width:{max(2, int(noise * 100.0 / mx))}%"></div></td><td></td></tr>')

    # What grew, ranked by REVIEWS. Maximising relative change names noise: a
    # 9-vs-3 code (+200%) is not the story next to +214 reviews (+78%). The
    # percentage beside the count is the same RATE change the table shows, so the
    # sentence and the column cannot disagree.
    riser = ""
    if show_delta:
        movers = sorted(((curw.get(c, 0) - prior.get(c, 0), c) for c in set(curw) | set(prior)
                         if c not in (NO_CODE, PROCESS_NOISE)), reverse=True)
        up = [(d, c) for d, c in movers if d >= floor][:3]
        if up:
            parts = []
            for delta, c in up:
                pv = prior.get(c, 0)
                pct = _pct(delta + pv, pv, cur_days, prior_days)
                pct = "new" if pct is None else f"{pct:+.0f}%"
                parts.append(f'<b>{esc(display_label(c))}</b> +{delta:,} ({pct})')
            riser = (f' Biggest increase{"" if len(up) == 1 else "s"} by review count against the '
                     f'{prior_days:,} days with data before: ' + " · ".join(parts) + ".")
        else:
            riser = (f' No complaint grew by {floor:,}+ reviews against the {prior_days:,} days '
                     f'with data before, so nothing here is named as a rise.')

    # A prior window that holds a fraction of the current one is a coverage
    # artefact: the corpus starts mid-window and the percentages would read as a
    # surge. The arrows are removed, not merely footnoted.
    caveat = ""
    if thin:
        caveat = (f' The preceding period ({pf.isoformat()} to {pt.isoformat()}) holds only '
                  f'{prior_total:,} reviews over {prior_days:,} day{"s" if prior_days != 1 else ""} '
                  f'with data against {cur_total:,} here, so no percentage is shown: the change '
                  f'would be coverage, not trend.')
    elif daygap:
        starts = f' (this selection has no reviews before {lo.isoformat()})' if lo and pf and lo > pf else ''
        caveat = (f' The preceding window holds {prior_days:,} days with data against {cur_days:,} '
                  f'here{starts}, so every percentage is a change PER DAY WITH DATA, not per '
                  f'calendar day: an equal-length calendar window would credit the baseline with '
                  f'days it has no reviews for and inflate every rise.')

    when = ""
    if f and t:
        when = (f' · comparing <b>{f.isoformat()}</b> to <b>{t.isoformat()}</b> '
                f'({cur_days:,} days with data) against the {prior_days:,} days with data before '
                f'({pf.isoformat()} to {pt.isoformat()})') if prior else \
               f' · period {f.isoformat()} to {t.isoformat()}'
    elif basis:
        when = f' · period: {esc(basis)}'
    # W2: the caption names the FILTERED data end, and says when that is earlier
    # than the corpus, so a short selection cannot read as a stalled corpus.
    if hi and (clamped or (corpus_hi and hi < corpus_hi)):
        when += f' · data to {hi.isoformat()}'
        if corpus_hi and hi < corpus_hi:
            when += f' (whole corpus to {corpus_hi.isoformat()})'

    # prose / validity disclosure: the top answer is a return-reason picklist,
    # and nothing has been human-validated. Both were invisible, so a slide could
    # quote a validated figure that does not exist.
    n_pr = n - n_no
    disc = (f'<b>{n_pr:,}</b> of <b>{n:,}</b> rows carry a customer comment; the other '
            f'{n - n_pr:,} hold the return-reason picklist only, so no label on them can be checked '
            f'against what a customer wrote')
    val = (f' <b>{n_hv:,}</b> of <b>{n:,}</b> rows are human-validated'
           + ('' if n_hv else ' — no count on this page is a validated figure'))

    # Toggles. Every count is computed from the EXACT dict its link carries, so a
    # count can never describe a different row set than the page it opens. (W5:
    # the strip dropped `status` while the link kept it, so
    # /reviews?family=Seat+cushions&status=flagged offered "all rows 1,571"
    # linking to a page of 137.)
    def _tog(label, over, on):
        d = {k: v for k, v in q.items() if k not in ("page", "msg", "err")}
        d.update(over)
        d = {k: v for k, v in d.items() if v not in (None, "")}
        ww, aa = build_where(d)
        cur.execute(f"SELECT COUNT(*) n FROM v_ai_complaints v {ww}", aa)
        k = int(cur.fetchone()["n"] or 0)
        cls = ' class="on"' if on else ""
        return f'<a href="/reviews?{esc(qstr(d))}"{cls}>{label} {k:,}</a>'

    deduped = q.get("dup") == "1"
    togs = (f'{_tog("all rows", {"tx": ""}, not q.get("tx"))} · '
            f'{_tog("with a comment", {"tx": "prose"}, q.get("tx") == "prose")} · '
            f'{_tog("picklist only", {"tx": "codeonly"}, q.get("tx") == "codeonly")} · '
            f'{_tog("human-validated only", {"status": "decided"}, q.get("status") == "decided")} · '
            f'{_tog("count every row" if deduped else "distinct complaints", {"dup": "" if deduped else "1"}, False)}')

    # W8: duplicated review rows are disclosed, never silently collapsed. The
    # flag is 'same MD5(text), SKU and review_date' (view: dup_rank/dup_group).
    if deduped:
        qd = {k: v for k, v in q.items() if k != "dup"}
        wd, ad = build_where(qd)
        cur.execute(f"SELECT COUNT(*) n FROM v_ai_complaints v {wd}", ad)
        hidden = max(0, int(cur.fetchone()["n"] or 0) - n)
        plural = hidden != 1
        dupnote = (f' <b>{hidden:,}</b> ' + ("rows that repeat" if plural else "row that repeats") +
                   " a review already counted for the same SKU on the same date " +
                   ("are" if plural else "is") +
                   " hidden by this view — one row per review, applied on request and named here")
    elif n_dup:
        plural = n_dup != 1
        dupnote = (f' <b>{n_dup:,}</b> ' + ("rows repeat" if plural else "row repeats") +
                   " a review already counted for the same SKU on the same date; " +
                   ("they are counted" if plural else "it is counted") + ", not removed — "
                   f'<a href="/reviews?{esc(qstr(q, dup="1", page=""))}">show {n - n_dup:,} distinct '
                   f'complaints</a>')
    else:
        dupnote = ' No row repeats another review on the same SKU and date.'

    # The last point is a partial month: say so and give the daily rate, because
    # a short month looks like a fall. The rate is per day WITH DATA, the same
    # basis as the change column.
    part = None
    if plot and hi and plot[-1][0] == f"{hi.year:04d}-{hi.month:02d}" and hi.day < _month_days(plot[-1][0]):
        part = hi.day
    partnote = ""
    if part and len(plot) > 1:
        pm, pn = plot[-2]
        pdays = mdays.get(pm) or 1
        cdays = mdays.get(plot[-1][0]) or part
        partnote = (f' {MN[int(plot[-1][0][5:7]) - 1]} is drawn dashed because this selection\'s data '
                    f'stops on the {_ord(part)}: {plot[-1][1]:,} reviews on {cdays} day'
                    f'{"s" if cdays != 1 else ""} with data is {plot[-1][1] / cdays:.1f}/day against '
                    f'{pn / pdays:.1f}/day in {MN[int(pm[5:7]) - 1]} — a short month, not a fall.')

    hdr = ('complaint · reviews · share of matches'
           + (f' · change per day with data vs the {prior_days:,} days before' if show_delta else '')
           + (' · no earlier reviews here, so there is nothing to compare'
              if (f and t and not prior)
              else ('' if show_delta or not (f and t) else ' · change withheld (thin prior period)')))

    return f"""<div class="mix">
  <div class="mixhead"><h2>Complaint mix</h2>
    <span class="r">{n:,} reviews match these filters{when}</span></div>
  <div class="txtog">Show: {togs}</div>
  <div class="mixgrid">
    <div>
      <div style="font-size:11.5px;color:var(--mut);margin-bottom:5px">{hdr}</div>
      <table class="mixtbl"><tbody>{''.join(rows) or
        '<tr><td class="mixnote">No complaint is labelled in this selection.</td></tr>'}</tbody></table>
    </div>
    <div class="mixpanel">
      <div style="font-size:12.5px;font-weight:650;margin-bottom:2px">Complaints per month</div>
      {_mix_svg(plot, part) or '<div class="mixnote">No dated rows in this selection.</div>'}
      <div class="cap">{len(plot)} month{'s' if len(plot) != 1 else ''} with data{' · ' + plot[0][0] + ' to ' + plot[-1][0] if plot else ''}{f' · data to {hi.isoformat()}' if hi else ''}{f' · {omitted:,} older reviews outside this range are not drawn' if omitted else ''}{partnote}</div>
    </div>
  </div>
  <p class="mixnote"><b>{n:,}</b> rows match · <b>{bars_total:,}</b> are complaints · {blank:,} carry no
  complaint · {noise:,} are process noise (Review Deletion) shown for reconciliation only and kept out of the
  distribution.{riser}{caveat}</p>
  <p class="mixnote">{disc}.{val}.{dupnote}</p>
</div>"""


# ---------------------------------------------------------------- dashboard

def p_dashboard(cur, q, msg="", ctx=None):
    cur.execute("SELECT COUNT(*) c FROM classifications"); total = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE human_validated=0 AND needs_review=1"); nr = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE human_validated=0 AND needs_review=1 AND has_prose=0"); nr_co = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM proposed_codes WHERE status='pending'"); pend = cur.fetchone()["c"]
    # DISTINCT review_id, not COUNT(*): a revised decision adds a row, and
    # counting rows would inflate "human decisions" every time she changes her mind.
    cur.execute("SELECT COUNT(DISTINCT review_id) c FROM corrections"); corr = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM v_ai_complaints WHERE final_complaint IS NULL"); nofinal = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(DISTINCT final_complaint) c FROM v_ai_complaints"); labels = cur.fetchone()["c"]
    cur.execute("SELECT MIN(review_date) a, MAX(review_date) b FROM classifications"); rng = cur.fetchone()
    cur.execute("""SELECT SUM(source='ncx') ncx, SUM(source='fba') fba, COUNT(*) n FROM v_ai_complaints""")
    src = cur.fetchone()

    # Review Deletion is a rule verdict, not a complaint: it is listed (and kept
    # in the export) but never in the distribution, where it read as the 13th
    # most common thing customers complain about.
    cur.execute("""SELECT final_complaint complaint, COUNT(*) n FROM v_ai_complaints
                    WHERE final_complaint IS NOT NULL AND final_complaint <> %s
                    GROUP BY final_complaint ORDER BY n DESC LIMIT 14""", (PROCESS_NOISE,))
    top = cur.fetchall()
    mx = max([r["n"] for r in top], default=1)
    bars = "".join(
        f'<tr><td><a href="/reviews?complaint={urllib.parse.quote(r["complaint"])}">{esc(display_label(r["complaint"]))}</a></td>'
        f'<td class="mono num">{r["n"]:,}</td>'
        f'<td><div style="height:8px;border-radius:4px;background:var(--accent);opacity:.85;width:{int(r["n"]/mx*100)}%"></div></td></tr>'
        for r in top)

    body = f"""<h1>Dashboard</h1>
<p class="sub">Resolved labels across {total:,} reviews, {rng['a']} to {rng['b']}. Every count uses the resolved
complaint: the team's correction where one exists, the AI's label otherwise. {nofinal:,} row(s) carry no
complaint — marked not a complaint, or the AI could not assign one — and are excluded from these figures.</p>
<div class="grid">
  <div class="kpi"><div class="n">{total:,}</div><div class="l">Reviews classified</div>
    <div class="d">{src['ncx'] or 0:,} ncx · {src['fba'] or 0:,} fba</div></div>
  <div class="kpi"><div class="n">{nr:,}</div><div class="l">Need a human look</div>
    <div class="d"><a href="/reviews?status=flagged&tx=prose">unreviewed and flagged</a>
      · {nr_co:,} are <a href="/reviews?status=flagged&tx=codeonly">code only</a></div></div>
  <div class="kpi"><div class="n">{corr:,}</div><div class="l">Reviews decided by a human</div>
    <div class="d"><a href="/reviews?status=decided">view decided</a> · revisions count once</div></div>
  <div class="kpi"><div class="n">{labels}</div><div class="l">Codes in use</div>
    <div class="d">distinct resolved labels</div></div>
  <div class="kpi"><div class="n">{pend}</div><div class="l">New codes to decide</div>
    <div class="d"><a href="/codes">review the queue</a></div></div>
</div>
<h2>Most common complaints</h2>
<p class="sub" style="margin:-6px 0 10px">Whole corpus, no filters. For a date range and a product —
“what did seat-cushion buyers complain about in Jul–Sep, and what grew?” — use the
<a href="/reviews?family=Seat+cushions">complaint mix on Reviews</a>.</p>
<div class="card"><table><thead><tr><th>Complaint</th><th class="num">Reviews</th><th style="width:38%"></th></tr></thead>
<tbody>{bars}</tbody></table></div>"""
    return page("Dashboard", body, "/", msg, ctx=ctx)


# ---------------------------------------------------------------- reviews list

def p_reviews(cur, q, msg="", ctx=None):
    w, args = build_where(q)
    cur.execute(f"SELECT COUNT(*) c FROM v_ai_complaints v {w}", args)
    n = cur.fetchone()["c"]
    cur.execute(f"SELECT COUNT(*) c FROM v_ai_complaints v {w} AND v.has_prose=0"
                if w else f"SELECT COUNT(*) c FROM v_ai_complaints v WHERE v.has_prose=0", args)
    n_co = cur.fetchone()["c"]
    try:
        pg = max(1, int(q.get("page") or 1))
    except ValueError:
        pg = 1
    off = (pg - 1) * PAGE_SIZE
    cur.execute(f"""SELECT v.review_id, v.review_date, v.product, v.parent, v.sku, v.asin, v.title,
                           v.source, v.country, v.ai_complaint, v.final_complaint,
                           v.human_decision, v.human_validated, v.needs_review, v.is_new_code,
                           v.human_complaint, v.has_prose,
                           LEFT(COALESCE(v.review_text,''),400) txt
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
            label = (f'<b>{esc(display_label(r["final_complaint"]) or "")}</b> '
                     f'<span class="meta">(was {esc(display_label(r["ai_complaint"]))})</span>')
        else:
            label = esc(display_label(r["final_complaint"]) or "—")
        trs.append(f"""<tr>
          <td class="mono">{esc(r["review_date"])}</td>
          <td>{product_cell(r)}</td>
          <td class="mono">{esc(r["parent"] or "—")}</td>
          <td>{amz_sku(r["sku"])}</td>
          <td>{amz_asin(r["asin"])}</td>
          <td>{label}<br>{pill}</td>
          <td class="meta" style="min-width:300px">{text_snippet(r["txt"])}</td>
          <td><a href="/review/{r['review_id']}">open</a></td></tr>""")

    products = options(cur, "SELECT product k, COUNT(*) n FROM v_ai_complaints WHERE product IS NOT NULL GROUP BY product ORDER BY n DESC")
    parents = options(cur, "SELECT parent k, COUNT(*) n FROM v_ai_complaints WHERE parent IS NOT NULL GROUP BY parent ORDER BY n DESC")
    families = options(cur, "SELECT product_family k, COUNT(*) n FROM v_ai_complaints WHERE product_family IS NOT NULL GROUP BY product_family ORDER BY n DESC")
    sources = options(cur, "SELECT source k, COUNT(*) n FROM v_ai_complaints WHERE source IS NOT NULL GROUP BY source ORDER BY n DESC")
    countries = options(cur, "SELECT country k, COUNT(*) n FROM v_ai_complaints WHERE country IS NOT NULL GROUP BY country ORDER BY n DESC")
    complaints = options(cur, """SELECT final_complaint k, COUNT(*) n FROM v_ai_complaints
                                  WHERE final_complaint IS NOT NULL GROUP BY final_complaint ORDER BY n DESC""")

    statuses = "".join(f'<option value="{v}"{" selected" if q.get("status")==v else ""}>{lbl}</option>'
                       for v, lbl in [("new", "Unreviewed"), ("flagged", "Flagged, unreviewed"),
                                      ("decided", "Decided"), ("newcode", "New code")])
    # The complaint dropdown keeps the stored value (it is a filter, so it must
    # match the column) and only changes what it reads as.
    complaint_select = select('complaint', [(k, display_label(k), n) for k, n in complaints],
                              q.get("complaint") or "")
    body = f"""<h1>Reviews</h1>
<p class="sub">{n:,} match{'' if n == 1 else ''} · page {pg} of {max(1, (n + PAGE_SIZE - 1)//PAGE_SIZE)}
  · showing {len(rows)}{'' if q.get("tx") else f' · <b>{n_co:,}</b> of them are code only (no customer comment)'}
  · <a href="/export.csv?{esc(qstr(q))}">export CSV</a>
  · <a href="/export.csv?{esc(qstr(q, text='1'))}">export with text</a></p>
<form class="filters" method="get">
  <div><label>Search text or title</label><input name="q" value="{esc(q.get('q',''))}" placeholder="seat cushion"></div>
  <div><label>Product family</label>{select('family', families, q.get('family'))}</div>
  <div><label>Product / title</label><input name="product" value="{esc(q.get('product',''))}" placeholder="SC-B or sitzkissen"></div>
  <div><label>Parent</label>{select('parent', parents, q.get('parent'))}</div>
  <div><label>SKU</label><input name="sku" value="{esc(q.get('sku',''))}" placeholder="SC-B-IR"></div>
  <div><label>Complaint</label>{complaint_select}</div>
  <div><label>Source</label>{select('source', sources, q.get('source'))}</div>
  <div><label>Country</label>{select('country', countries, q.get('country'))}</div>
  <div><label>Status</label><select name="status"><option value="">Any</option>{statuses}</select></div>
  <div><label>Text</label><select name="tx">
    <option value="">Any</option>
    <option value="prose"{" selected" if q.get("tx")=="prose" else ""}>Has a customer comment</option>
    <option value="codeonly"{" selected" if q.get("tx")=="codeonly" else ""}>Code only (not checkable)</option>
  </select></div>
  <div><label>From</label><input type="date" name="from" value="{esc(q.get('from',''))}"></div>
  <div><label>To</label><input type="date" name="to" value="{esc(q.get('to',''))}"></div>
  <div><button class="primary">Filter</button></div>
  <div><a href="/reviews">clear</a></div>
</form>
{mix_block(cur, q, n)}
<div class="card"><table><thead><tr><th>Date</th><th>Product</th><th>Parent</th><th>SKU</th><th>ASIN</th>
<th>Complaint</th><th>Review</th><th></th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="8" class="empty">Nothing matches those filters.</td></tr>'}</tbody></table></div>
<div class="pager">{f'<a href="/reviews?{esc(qstr(q, page=pg-1))}">← previous</a>' if pg > 1 else ''}
  <span class="sp">page {pg}</span>
  {f'<a href="/reviews?{esc(qstr(q, page=pg+1))}">next →</a>' if off + PAGE_SIZE < n else ''}</div>"""
    return page("Reviews", body, "/reviews", msg, ctx=ctx)


# ---------------------------------------------------------------- one review

def reviewer_label(uid, name):
    """Who made a call. Never invents a person: 'unattributed' is a fact."""
    if name:
        return esc(name) + (f" · uid {uid}" if uid else "")
    return f"user #{uid}" if uid else "unattributed"


def decision_text(decision, new_complaint):
    if decision == "corrected":
        return f"corrected to <b>{esc(new_complaint or '—')}</b>"
    if decision == "accepted":
        return "<b>confirmed</b> as the AI's label"
    if decision == "rejected":
        return "marked <b>not a complaint</b>"
    return esc(decision or "")


def p_review(cur, rid, q, msg="", err=False, ctx=None):
    cur.execute("SELECT * FROM v_ai_complaints v WHERE v.review_id=%s", (rid,))
    r = cur.fetchone()
    if not r:
        return page("Not found", '<div class="card"><div class="empty">No such review.</div></div>',
                    "/reviews", msg or "No such review.", True, ctx)
    cur.execute("""SELECT id, decision, new_complaint, reviewed_by, reviewed_by_name, note, created_at
                     FROM corrections WHERE review_id=%s ORDER BY id DESC""", (rid,))
    prior = cur.fetchall()
    state = prior[0] if prior else None

    by_cat = defaultdict(list)
    for c in approved_codes(cur):
        by_cat[c["category"] or "Uncategorised"].append(c["code"])
    groups = "".join(
        f'<optgroup label="{esc(cat)}">' + "".join(
            f'<option value="{esc(c)}"{" selected" if c == r["final_complaint"] else ""}>{esc(c)}</option>'
            for c in codes) + "</optgroup>"
        for cat, codes in sorted(by_cat.items()))

    prior_html = "".join(
        f'<div class="meta">· {esc(p["created_at"])} — {decision_text(p["decision"], p["new_complaint"])}'
        f' · {reviewer_label(p["reviewed_by"], p["reviewed_by_name"])}'
        f'{" — " + esc(p["note"]) if p["note"] else ""}'
        f'{" <b>· current</b>" if p is state else ""}</div>' for p in prior) \
        or '<div class="meta">No decisions yet.</div>'

    if state:
        decision_box = (
            f'<div class="decision"><b>Decided:</b> {decision_text(state["decision"], state["new_complaint"])}'
            f' · {reviewer_label(state["reviewed_by"], state["reviewed_by_name"])}'
            f' · {esc(state["created_at"])}'
            f'{"<br>note: " + esc(state["note"]) if state["note"] else ""}'
            f'<br><span class="meta">Changing it below <b>supersedes</b> this decision — the row above stays in the '
            f'history, and every figure switches to your new call.</span></div>')
    else:
        decision_box = ('<div class="decision">No decision yet. Whatever you record below is what the dashboard, '
                        'the monthly rollup and the export will use for this review.</div>')

    warn = ""
    if not r["has_prose"]:
        warn = ('<div class="syncbar bad" style="margin:12px 0"><b>Code only.</b> This row has no customer comment — '
                'the return reason is the whole text, so there is little to check the label against. '
                'You can still decide it.</div>')

    body = f"""<h1>Review {esc(rid)}</h1>
<p class="sub">{product_cell(r)} · parent {esc(r["parent"] or "—")} · SKU {amz_sku(r["sku"])}
  · ASIN {amz_asin(r["asin"])}<br>{esc(r["review_date"])} · {esc(r["source"] or "")} {esc(r["country"] or "")}
  {("· order " + esc(r["order_id"])) if r["order_id"] else ""}</p>
{warn}
<div class="review">
  <div>
    <div class="text" style="padding:0;background:#fff">{text_html(r["review_text"])}</div>
    <div style="margin-top:14px" class="meta">AI assigned <b>{esc(r["ai_complaint"] or "—")}</b>
      {("· category " + esc(r["ai_category"])) if r["ai_category"] else ""}
      {("· second choice " + esc(r["second_complaint"])) if r["second_complaint"] else ""}</div>
    <div style="margin-top:4px" class="meta">Reporting as <b>{esc(r["final_complaint"] or "—")}</b>
      {("· " + esc(r["final_category"])) if r["final_category"] else ""}</div>
    <div style="margin-top:16px">{decision_box}</div>
    <div style="margin-top:12px"><b class="meta">History</b>{prior_html}</div>
  </div>
  <div class="card" style="padding:14px">
    <form method="post" action="/review/{rid}">
      {f'<label>Confirm the AI label</label><button class="primary" name="action" value="accept" style="width:100%;margin-bottom:14px">Confirm “{esc(r["ai_complaint"])}”</button>' if r["ai_complaint"] else '<div class="meta" style="margin-bottom:12px">The AI assigned no code to this row — pick one below, or mark it not a complaint.</div>'}
      <label>Correct it</label>
      <select name="pick" style="width:100%;margin-bottom:8px"><option value="">choose a code…</option>{groups}</select>
      <input name="note" placeholder="optional note" style="width:100%;margin-bottom:8px">
      <button name="action" value="correct" style="width:100%">Save correction</button>
    </form>
    <form method="post" action="/review/{rid}" style="margin-top:10px">
      <input name="note" placeholder="why (optional)" style="width:100%;margin-bottom:8px">
      <button class="danger" name="action" value="reject" style="width:100%">Not a complaint / remove from reporting</button>
    </form>
    <div class="meta" style="margin-top:10px">Recorded against your login
      ({esc((ctx or {}).get("who", {}).get("name", "unknown"))}), not a shared account.</div>
  </div>
</div>"""
    return page(f"Review {rid}", body, "/reviews", msg, err, ctx)


def do_review(cur, rid, f, who):
    cur.execute("SELECT complaint FROM classifications WHERE review_id=%s", (rid,))
    row = cur.fetchone()
    if not row:
        # corrections.review_id has no foreign key, so a bad POST would otherwise
        # write a row pointing at nothing.
        return "No such review.", True
    ai = row["complaint"]
    act = f.get("action")
    note = (f.get("note") or "")[:500] or None
    who = who or {}

    if act == "accept":
        dec, newc = "accepted", None
    elif act == "correct":
        pick = (f.get("pick") or "").strip()
        if not pick:
            return "Pick a code first.", True
        # Case-insensitive lookup and store the TAXONOMY spelling: the picker is
        # built from complaint_codes, so a correction must not introduce a second
        # casing of a code that already exists ('Not As Advertised/misleading').
        cur.execute("SELECT code FROM complaint_codes WHERE LOWER(code)=LOWER(%s) AND approved=1", (pick,))
        found = cur.fetchone()
        if not found:
            return f"“{pick}” is not an approved code — approve it on the New codes page first.", True
        pick = found["code"]
        dec, newc = ("accepted", None) if norm(pick) == norm(ai) else ("corrected", pick)
    elif act == "reject":
        dec, newc = "rejected", None
    else:
        return "Unknown action.", True

    # A decision is a new row, never an update: v_ai_complaints reads the latest id
    # per review, so inserting is what makes a revision take effect - and the
    # earlier row stays as evidence of what was decided first.
    cur.execute("""SELECT decision, new_complaint FROM corrections
                    WHERE review_id=%s ORDER BY id DESC LIMIT 1""", (rid,))
    prev = cur.fetchone()
    if prev and prev["decision"] == dec and norm(prev["new_complaint"]) == norm(newc):
        return "That is already the recorded decision — nothing changed.", False

    cur.execute("""INSERT INTO corrections (review_id, ai_complaint, new_complaint, decision,
                                            reviewed_by, reviewed_by_name, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (rid, ai, newc, dec, who.get("uid"), who.get("name"), note))
    what = {"accepted": "Confirmed.", "corrected": f"Corrected to {newc}.", "rejected":
            "Marked as not a complaint; it leaves complaint reporting."}[dec]
    if dec == "accepted" and act == "correct":
        what = "That is the AI label — recorded as confirmed."
    if prev:
        what += " Supersedes the earlier decision; reporting now uses this one."
    return what, False



# ---------------------------------------------------------------- monthly

PAGE_ROLLUP = 200
MONTHLY_COLS = ["month", "quarter", "product_family", "product", "parent", "sku", "asin", "title",
                "title_short", "complaint", "complaint_display", "category", "n",
                "pct_of_sku_month", "n_human_validated"]


def month_grain(v):
    """'2026-07-14' or '2026-07' or '2026-7' -> '2026-07'. '' when unusable.

    The monthly page is month-grain, so a date has to be resolved to a month
    ONCE, in one place, and the page has to say that is what it did. Comparing
    the string '2026-07' to a date only worked by accident of collation
    (LEFT('2026-07-14',7) = '2026-07' >= '2026-07'), and the same 7-day range
    meant all of July here and one week on Reviews.
    """
    m = re.match(r"^\s*(\d{4})-(\d{1,2})", (v or ""))
    if not m:
        return ""
    mo = int(m.group(2))
    return f"{int(m.group(1)):04d}-{mo:02d}" if 1 <= mo <= 12 else ""


def reviews_url_for_month(q):
    """The Reviews link for the SAME period this page filtered on.

    A month range goes over as month=YYYY-MM (one month) or as the first/last
    day of the months (a span), never as the raw day-grain date: 'from=2026-07-14'
    reads as a week there and as July here.
    """
    mf, mt = month_grain(q.get("from")), month_grain(q.get("to"))
    d = {k: v for k, v in q.items() if k not in ("page", "from", "to")}
    if mf and mf == mt:
        d["month"] = mf
    else:
        if mf:
            a, _ = _month_bounds(mf)
            d["from"] = (a or _d(mf)).isoformat() if (a or _d(mf)) else mf
        if mt:
            _, b = _month_bounds(mt)
            d["to"] = b.isoformat() if b else mt
    return "/reviews?" + qstr(d)


def build_where_monthly(q):
    """One WHERE builder for the rollup, its count and /monthly.csv.

    Operates on v_ai_complaints_monthly, so a date range is expressed against the
    month column: from=2026-07-14 means "from month 2026-07". The table, the
    pager and the export therefore cannot disagree about what is filtered.
    """
    where, args = [], []
    if q.get("month"):
        where.append("month=%s"); args.append(q["month"])
    if q.get("product"):
        where.append("(product LIKE %s OR title LIKE %s)"); args += [f'%{q["product"]}%'] * 2
    if q.get("family"):
        where.append("product_family=%s"); args.append(q["family"])
    if q.get("parent"):
        where.append("parent=%s"); args.append(q["parent"])
    if q.get("complaint"):
        where.append("complaint=%s"); args.append(q["complaint"])
    mf, mt = month_grain(q.get("from")), month_grain(q.get("to"))
    if mf:
        where.append("month >= %s"); args.append(mf)
    elif q.get("from"):
        where.append("1=0")           # an unparseable date must not mean "all of it"
    if mt:
        where.append("month <= %s"); args.append(mt)
    elif q.get("to"):
        where.append("1=0")
    return ("WHERE " + " AND ".join(where)) if where else "", args


def p_monthly(cur, q, msg="", ctx=None):
    w, args = build_where_monthly(q)
    cur.execute(f"SELECT COUNT(*) c FROM v_ai_complaints_monthly {w}", args)
    n = cur.fetchone()["c"]
    try:
        pg = max(1, int(q.get("page") or 1))
    except ValueError:
        pg = 1
    pages = max(1, (n + PAGE_ROLLUP - 1) // PAGE_ROLLUP)
    pg = min(pg, pages)
    off = (pg - 1) * PAGE_ROLLUP
    cur.execute(f"""SELECT month, quarter, product_family, product, parent, sku, asin,
                           complaint, category, n, pct_of_sku_month, n_human_validated,
                           LEFT(COALESCE(title,''),70) title, title_short
                      FROM v_ai_complaints_monthly {w}
                     ORDER BY month DESC, n DESC, sku, complaint
                     LIMIT {PAGE_ROLLUP} OFFSET {off}""", args)
    rows = cur.fetchall()
    trs = "".join(
        f"""<tr><td class="mono">{esc(r["month"])}</td>
        <td>{esc(r["product"] or "—")}<div class="title">{esc(r["title"] or "")}</div></td>
        <td>{esc(r["product_family"] or "—")}</td>
        <td class="mono">{esc(r["parent"] or "—")}</td>
        <td>{amz_sku(r["sku"])}{f'<div class="title">{esc(r["title_short"])}</div>' if r["title_short"] else ""}</td>
        <td>{esc(display_label(r["complaint"]))}</td><td class="meta">{esc(r["category"] or "")}</td>
        <td class="mono num">{r["n"]:,}</td><td class="mono num">{r["pct_of_sku_month"]}%</td>
        <td class="mono num">{int(r["n_human_validated"] or 0):,}</td></tr>""" for r in rows)

    months = options(cur, """SELECT month k, SUM(n) n FROM v_ai_complaints_monthly
                              WHERE month IS NOT NULL GROUP BY month ORDER BY month DESC""")
    parents = options(cur, "SELECT parent k, SUM(n) n FROM v_ai_complaints_monthly WHERE parent IS NOT NULL GROUP BY parent ORDER BY n DESC")
    families = options(cur, "SELECT product_family k, SUM(n) n FROM v_ai_complaints_monthly WHERE product_family IS NOT NULL GROUP BY product_family ORDER BY n DESC")
    complaints = options(cur, "SELECT complaint k, SUM(n) n FROM v_ai_complaints_monthly GROUP BY complaint ORDER BY n DESC")
    complaint_sql = select('complaint', [(k, display_label(k), v) for k, v in complaints], q.get("complaint") or "")
    mf, mt = month_grain(q.get("from")), month_grain(q.get("to"))
    grain_note = ""
    if mf or mt:
        grain_note = (f' <b>Months {mf or "first"} to {mt or "last"}</b> — this page is month grain, so a date '
                      f'like the 14th means that whole month (the Reviews link above carries month=).')
    body = f"""<h1>Monthly</h1>
<p class="sub">The grain the team reports on: SKU × month × complaint, with the share of that SKU-month and how
many rows a human validated. {n:,} rows match{'' if n == 1 else ''} · page {pg} of {pages}
(rows {off + 1:,}–{off + len(rows):,}) · <a href="/monthly.csv?{esc(qstr(q))}">export CSV</a>.
Process noise (Review Deletion) and unclassified categories are not in this rollup; the review-grain rows
behind any filter are on <a href="{esc(reviews_url_for_month(q))}">Reviews</a>,
where the same filters also show the complaint mix.{grain_note}</p>
<form class="filters" method="get">
  <div><label>Month</label>{select('month', months, q.get('month'))}</div>
  <div><label>From month</label><input type="month" name="from" value="{esc(month_grain(q.get('from')))}"></div>
  <div><label>To month</label><input type="month" name="to" value="{esc(month_grain(q.get('to')))}"></div>
  <div><label>Product family</label>{select('family', families, q.get('family'))}</div>
  <div><label>Product / title</label><input name="product" value="{esc(q.get('product',''))}" placeholder="seat cushion"></div>
  <div><label>Parent</label>{select('parent', parents, q.get('parent'))}</div>
  <div><label>Complaint</label>{complaint_sql}</div>
  <div><button class="primary">Filter</button></div>
  <div><a href="/monthly">clear</a></div>
</form>
<div class="card"><table><thead><tr><th>Month</th><th>Product</th><th>Family</th><th>Parent</th><th>SKU</th>
<th>Complaint</th><th>Category</th><th class="num">Reviews</th><th class="num">% of SKU-month</th>
<th class="num">Human validated</th></tr></thead>
<tbody>{trs or '<tr><td colspan="10" class="empty">Nothing matches those filters.</td></tr>'}</tbody></table></div>
<div class="pager">{f'<a href="/monthly?{esc(qstr(q, page=pg-1))}">← previous</a>' if pg > 1 else ''}
  <span class="sp">page {pg} of {pages}</span>
  {f'<a href="/monthly?{esc(qstr(q, page=pg+1))}">next →</a>' if pg < pages else ''}</div>"""
    return page("Monthly", body, "/monthly", msg, ctx=ctx)


def monthly_csv(cur, q):
    w, args = build_where_monthly(q)
    cur.execute(f"""SELECT month, quarter, product_family, product, parent, sku, asin, title,
                           title_short, complaint, category, n, pct_of_sku_month, n_human_validated
                      FROM v_ai_complaints_monthly {w}
                     ORDER BY month DESC, n DESC, sku, complaint""", args)
    buf = io.StringIO()
    wtr = csv.writer(buf)
    wtr.writerow(MONTHLY_COLS)
    for r in cur.fetchall():
        row = dict(r)
        # The STORED code in `complaint` — the same value /export.csv writes — so
        # the two exports join. The readable label is a separate column, added
        # here and in export_csv, never a rename of one and not the other.
        row["complaint_display"] = display_label(row.get("complaint")) or ""
        wtr.writerow([row.get(c) for c in MONTHLY_COLS])
    return (buf.getvalue().encode("utf-8-sig"), "text/csv",
            f"attachment; filename=ai-complaints-monthly-{date.today().isoformat()}.csv")


# ---------------------------------------------------------------- codes

def p_codes(cur, q, msg="", ctx=None):
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
            # A deny must always name its replacement. The prescribed mapping is
            # PRE-SELECTED and named on the button, never applied invisibly: the
            # page promises "a deny without a replacement is refused", and a
            # hidden default made that promise false for all three pending codes.
            mapped = DENY_MAP.get(norm(r["code"]))
            mapped_ok = mapped if any(c["code"] == mapped for c in approved) else None
            repl_opts = "".join(
                f'<option value="{esc(c["code"])}"{" selected" if mapped_ok == c["code"] else ""}>'
                f'{esc(c["code"])}</option>' for c in approved)
            deny_lbl = f'Deny → remap to {esc(mapped_ok)}' if mapped_ok else "Deny"
            act = f"""<form method="post" action="/codes" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
              <input type="hidden" name="code" value="{esc(r['code'])}">
              <select name="category" title="category for an approved code">
                <option value="">category…</option>
                {''.join(f'<option value="{esc(c)}">{esc(c)}</option>' for c in cats)}</select>
              <button name="action" value="approved" class="primary">Approve</button>
              <select name="replacement" title="deny: the code you mean instead (required)">
                <option value="">remap to…</option>{repl_opts}</select>
              <button name="action" value="denied" class="danger">{deny_lbl}</button>
            </form>"""
            if mapped_ok:
                act += (f'<div class="meta">prescribed mapping pre-selected: '
                        f'{esc(r["code"])} → {esc(mapped_ok)} — change it or deny without one and the deny is refused</div>')
            elif mapped:
                act += (f'<div class="meta" style="color:var(--bad)">the prescribed mapping '
                        f'“{esc(mapped)}” is not an approved code — choose a replacement</div>')
        else:
            act = st + (f'<div class="meta">remapped to {esc(denials.get(r["code"]))}</div>'
                        if denials.get(r["code"]) else "")
        trs.append(f"""<tr><td><b>{esc(r["code"])}</b></td><td class="mono num">{r["n_rows"]:,}</td>
          <td class="meta">{esc((r["example_review"] or "")[:150])}</td><td>{act}</td></tr>""")

    body = f"""<h1>New complaint codes</h1>
<p class="sub">Codes the classifier produced that the module does not have. <b>Approve</b> adds the code to the
vocabulary with a category — its reviews stop being “new”, and the code is written to the vocabulary file the
classifier loads, so the next classification batch can emit it. <b>Deny</b> suppresses the code and remaps its
reviews to the one you choose; a deny without a replacement is refused, because it would silently drop those
reviews out of every rollup. Where a replacement was prescribed for the code, it is selected in the dropdown and
named on the button — nothing is applied behind your back.</p>
<div class="card"><table><thead><tr><th>Code</th><th class="num">Reviews</th><th>Example</th><th>Decision</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="4" class="empty">Nothing proposed.</td></tr>'}</tbody></table></div>"""
    return page("New codes", body, "/codes", msg, ctx=ctx)


def do_code(cur, f, who):
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
                        WHERE code=%s""", ((who or {}).get("uid"), cat, code))
        cur.execute("""INSERT INTO complaint_codes (code, category, source, approved)
                       VALUES (%s,%s,'chadia-approved',1)
                       ON DUPLICATE KEY UPDATE approved=1, source='chadia-approved',
                         category=COALESCE(VALUES(category), complaint_codes.category)""", (code, cat))
        cur.execute("""UPDATE classifications SET category=%s
                        WHERE complaint=%s AND (category IS NULL OR category='')""", (cat, code))
        cur.execute("UPDATE classifications SET is_new_code=0 WHERE complaint=%s", (code,))
        n = cur.rowcount
        # The same approval, written where the classifier actually reads it. The
        # database row above is the durable record (source='chadia-approved',
        # approved=1) and is what the refresh host syncs from; this file is the
        # leg the classifier loads, so a failure here is reported, never silent.
        vpath, verr = vocab_upsert(code, cat)
        if vpath:
            tail = (f" Written to {os.path.relpath(vpath)} — the next classification batch can emit it.")
        else:
            tail = (f" The durable record is in complaint_codes (approved=1, source='chadia-approved'); "
                    f"<b>the classifier vocabulary file was NOT updated</b> ({verr}), so it cannot be "
                    f"emitted until data/vocab_additions.csv is written on the refresh host — set "
                    f"CX_VOCAB_ADDITIONS there and sync: SELECT code, category FROM complaint_codes "
                    f"WHERE approved=1 AND source='chadia-approved'.")
        return (f"Approved “{code}” as {cat}. {max(n, 0):,} review(s) are no longer flagged as a new code."
                + tail), False

    # deny ---------------------------------------------------------------
    # No hidden default: the replacement must be a value that arrived in the form.
    # The page pre-selects a prescribed mapping and says so on the button, so the
    # promise "a deny without a replacement is refused" is literally true.
    repl = (f.get("replacement") or "").strip()
    if not repl:
        return ("A deny needs a replacement code: without one those reviews keep a label you rejected, "
                "or silently leave reporting. Pick “remap to…”.", True)
    cur.execute("SELECT category FROM complaint_codes WHERE code=%s AND approved=1", (repl,))
    tgt = cur.fetchone()
    if not tgt:
        return f"Replacement “{repl}” is not an approved code.", True
    cur.execute("""UPDATE proposed_codes SET status='denied', decided_at=NOW(), decided_by=%s, note=%s
                    WHERE code=%s""", ((who or {}).get("uid"), f"-> {repl}", code))
    cur.execute("""INSERT INTO complaint_codes (code, category, source, approved)
                   VALUES (%s,NULL,'chadia-denied',0)
                   ON DUPLICATE KEY UPDATE approved=0, source='chadia-denied'""", (code,))
    # code_denials is what sync_corrections.write_denials_json reads on the refresh
    # host, so this mapping also governs FUTURE classifications (cxvocab.canonical).
    cur.execute("""INSERT INTO code_denials (code, replacement, note, decided_by)
                   VALUES (%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE replacement=VALUES(replacement), note=VALUES(note),
                     decided_by=VALUES(decided_by), decided_at=NOW()""",
                (code, repl, (f.get("note") or "")[:500] or None, (who or {}).get("uid")))
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

    The pool is rows with a CUSTOMER COMMENT (`has_prose=1`). 8,475 of 22,187 rows
    carry only the return-reason picklist, so a sample drawn from everything was
    38% uncheckable: the checker is asked to grade a label against 'Too big'.
    Stratification is unchanged; only the pool it draws from is now checkable.
    """
    wk = iso_week()
    cur.execute("SELECT COUNT(*) n FROM weekly_samples WHERE sample_week=%s", (wk,))
    if cur.fetchone()["n"]:
        return wk
    cur.execute("""SELECT review_id, COALESCE(final_category,'Uncategorised') cat
                     FROM v_ai_complaints
                    WHERE review_date IS NOT NULL AND final_complaint IS NOT NULL
                      AND has_prose=1""")
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
    """Agreement over the rows that can actually be CHECKED.

    A row with no customer comment (has_prose=0) holds the return-reason picklist
    as its whole text — there is nothing to check the label against, so it cannot
    be evidence either way and is kept out of the denominator (and named).
    The pool is prose-only now, but a week drawn before that change still carries
    code-only rows; excluding them from the arithmetic is what stops 18 of 45
    uncheckable rows from being scored as if they were agreement.
    """
    usable = [r for r in rows if int(r.get("has_prose") or 0) == 1]
    skipped = len(rows) - len(usable)
    acc = sum(1 for r in usable if r["your_call"] and norm(r["your_call"]) == norm(r["ai_complaint"]))
    cor = sum(1 for r in usable if r["your_call"] and NOT_A_COMPLAINT not in norm(r["your_call"])
              and norm(r["your_call"]) != norm(r["ai_complaint"]))
    rej = sum(1 for r in usable if r["your_call"] and NOT_A_COMPLAINT in norm(r["your_call"]))
    return acc, cor, rej, acc + cor, skipped   # rejected rows leave the denominator


def sample_rows(cur, wk):
    cur.execute("""SELECT s.review_id, s.your_call, s.note, s.decided_at,
                          v.review_date, v.sku, v.product, v.parent, v.asin, v.title,
                          v.ai_complaint, v.final_complaint, v.final_category, v.has_prose,
                          LEFT(COALESCE(v.review_text,''),400) txt
                     FROM weekly_samples s
                     JOIN v_ai_complaints v ON v.review_id = s.review_id
                    WHERE s.sample_week=%s
                    ORDER BY s.review_id""", (wk,))
    return cur.fetchall()


def record_sample(cur, wk, sample_by=None):
    """Roll the filled calls into quality_checks. Under 5 calls is progress, not a
    measurement: recording 100% on n=1 would put a meaningless point on the trend."""
    rows = sample_rows(cur, wk)
    acc, cor, rej, n, skipped = sample_stats(rows)
    if n < 5:
        return None
    pct = round(acc * 100.0 / n, 2)
    note = (f"weekly random sample: {acc} agreed, {cor} corrected, {rej} removed "
            f"(excluded from the denominator)")
    if skipped:
        note += f"; {skipped} code-only row(s) excluded (nothing to check)"
    cur.execute("""INSERT INTO quality_checks (week_start, sample_n, agreement_pct, checked_by, sample_ref, note)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE sample_n=VALUES(sample_n),
                     agreement_pct=VALUES(agreement_pct), sample_ref=VALUES(sample_ref),
                     note=VALUES(note)""",
                (week_start(), n, pct, sample_by, f"weekly-{wk}", note))
    return pct


def p_quality(cur, q, msg="", ctx=None):
    wk = ensure_sample(cur)
    rows = sample_rows(cur, wk)
    acc, cor, rej, n, skipped = sample_stats(rows)
    pct = record_sample(cur, wk, (ctx or {}).get('who', {}).get('uid'))
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
          <td class="meta" style="white-space:normal;min-width:280px">{text_snippet(r["txt"], 120)} <a href="/review/{r['review_id']}">full</a></td>
          <td><b>{esc(r["ai_complaint"] or "—")}</b><div class="meta">{esc(r["final_category"] or "")}</div></td>
          <td>{sel}</td></tr>""")

    trend = "".join(
        f'<tr><td class="mono">{esc(c["week_start"])}</td><td class="mono num">{c["sample_n"]}</td>'
        f'<td class="mono num">{c["agreement_pct"]}%</td><td class="meta">{esc(c["note"] or "")}</td></tr>'
        for c in checks)

    if pct is not None:
        head, note = f"{pct}%", f"n={n} of {len(rows)} called" + (
            f" · {skipped} code-only excluded" if skipped else "")
    elif filled:
        head, note = f"{filled}/{len(rows)}", "called — agreement records at 5+"
    else:
        head, note = f"0/{len(rows)}", f"drawn for {wk} — no calls yet"

    body = f"""<h1>Quality</h1>
<p class="sub">The weekly random check: {SAMPLE_N} reviews drawn at random from the classified corpus,
stratified by complaint category so a small but important category is not swamped by FBA returns.
Record the call for each row — the agreement is computed from your calls, there is no percentage to type.
“Not a complaint” leaves the denominator, exactly as in the sheet workflow. The sample is drawn only from
reviews that carry a <b>customer comment</b>: a row whose whole text is the return-reason picklist gives
you nothing to check a label against, so it is not drawn — and any code-only row left over from an older
week is excluded from the agreement and named, rather than scored as a pass or a fail.</p>
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
    return page("Quality", body, "/quality", msg, ctx=ctx)


def do_quality(cur, environ, who):
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
                        WHERE sample_week=%s AND review_id=%s""",
                    (v[:120], (who or {}).get("uid"), wk, rid))
        n += 1
    pct = record_sample(cur, wk, (who or {}).get("uid"))
    if pct is not None:
        return f"Saved {n} call(s); week {wk} agreement {pct}%.", False
    return f"Saved {n} call(s); agreement is recorded once at least 5 rows are called.", False


# ---------------------------------------------------------------- export

EXPORT_COLS = ["review_id", "review_date", "month", "product_family", "product", "parent", "sku",
               "asin", "title", "source", "country", "ai_complaint", "ai_category", "final_complaint",
               "complaint_display", "final_category", "human_decision", "human_complaint",
               "needs_review", "is_new_code", "has_prose"]


def export_csv(cur, q):
    w, args = build_where(q)
    cols = EXPORT_COLS + (["review_text"] if q.get("text") == "1" else [])
    cur.execute(f"""SELECT v.review_id, v.review_date, DATE_FORMAT(v.review_date,'%%Y-%%m') month,
                           v.product_family, v.product, v.parent, v.sku, v.asin, v.title,
                           v.source, v.country,
                           v.ai_complaint, v.ai_category, v.final_complaint, v.final_category,
                           v.human_decision, v.human_complaint, v.needs_review, v.is_new_code,
                           v.has_prose, v.review_text
                      FROM v_ai_complaints v {w}
                     ORDER BY v.review_date DESC, v.review_id DESC""", args)
    buf = io.StringIO()
    wtr = csv.writer(buf)
    wtr.writerow(cols)
    for r in cur.fetchall():
        row = dict(r)
        # final_complaint is the STORED code ('Vague Complaint'); the readable
        # label rides in its own column. Renaming in one export only split the
        # same row across two vocabularies and broke every join back to the
        # monthly rollup.
        row["complaint_display"] = display_label(row.get("final_complaint")) or ""
        wtr.writerow([row.get(c) for c in cols])
    return (buf.getvalue().encode("utf-8-sig"), "text/csv",
            f"attachment; filename=ai-complaints-{date.today().isoformat()}.csv")


# ---------------------------------------------------------------- dispatch

def auth_user(environ):
    """The signed-in user as {pw, uid, name}, or None.

    Returning the person (not a boolean) is the whole point: every write stamps
    reviewed_by from this dict, so the audit trail names whoever actually decided.
    """
    auth = environ.get("HTTP_AUTHORIZATION", "")
    if not auth.startswith("Basic "):
        return None
    try:
        u, _, p = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
    except Exception:                                          # noqa: BLE001
        return None
    rec = USERS.get(u)
    if not rec or not rec["pw"] or not secrets.compare_digest(p, rec["pw"]):
        return None
    return rec


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

    who = auth_user(environ)
    if not who:
        start_response("401 Unauthorized",
                       [("WWW-Authenticate", 'Basic realm="AI Complaint Validation"'),
                        ("Content-Type", "text/plain; charset=utf-8")])
        return [b"Authentication required"]

    q = parse_qs(environ)
    msg, err = q.get("msg", ""), q.get("err") == "1"
    conn = db()
    try:
        cur = conn.cursor()
        # One context object for every page: who is signed in, and how fresh the
        # data is. The freshness strip goes on every screen because a stale number
        # is worse than no number.
        ctx = {"who": who, "banner": banner_html(refresh_status(cur))}

        if method == "POST" and path.startswith("/review/"):
            try:
                rid = int(path.rsplit("/", 1)[1])
            except ValueError:
                start_response("404 Not Found", [("Content-Type", "text/plain")])
                return [b"bad id"]
            m, e = do_review(cur, rid, parse_form(environ), who)
            return redirect(start_response, f"/review/{rid}", m, e)

        if method == "POST" and path == "/codes":
            m, e = do_code(cur, parse_form(environ), who)
            return redirect(start_response, "/codes", m, e)

        if method == "POST" and path == "/quality":
            m, e = do_quality(cur, environ, who)
            return redirect(start_response, "/quality", m, e)

        if path == "/export.csv":
            body, ctype, disp = export_csv(cur, q)
            start_response("200 OK", [("Content-Type", ctype), ("Content-Disposition", disp)])
            return [body]

        if path == "/monthly.csv":
            body, ctype, disp = monthly_csv(cur, q)
            start_response("200 OK", [("Content-Type", ctype), ("Content-Disposition", disp)])
            return [body]

        if path == "/reviews":
            html = p_reviews(cur, q, msg, ctx)
        elif path.startswith("/review/"):
            try:
                rid = int(path.rsplit("/", 1)[1])
            except ValueError:
                start_response("404 Not Found", [("Content-Type", "text/plain")])
                return [b"bad id"]
            html = p_review(cur, rid, q, msg, err, ctx)
        elif path == "/codes":
            html = p_codes(cur, q, msg, ctx)
        elif path == "/quality":
            html = p_quality(cur, q, msg, ctx)
        elif path == "/monthly":
            html = p_monthly(cur, q, msg, ctx)
        else:
            html = p_dashboard(cur, q, msg, ctx)

        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]
    finally:
        conn.close()


if __name__ == "__main__":
    if not any(u["pw"] for u in USERS.values()):
        sys.exit("APP_PASS not set — refusing to serve without a password")
    port = int(os.environ.get("APP_PORT", "8080"))
    host = os.environ.get("APP_HOST", "127.0.0.1")
    print(f"serving on {host}:{port} as {', '.join(sorted(USERS))}")
    make_server(host, port, app).serve_forever()
