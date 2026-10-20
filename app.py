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
                    one stays as history). Opened as a slide-over drawer ON
                    /reviews (?partial=1 returns just the panel's inner HTML, and a
                    decision posts back into it), so reviewing never costs the
                    filters or the page; the bare URL still renders a full page for
                    a direct link.
  /monthly          SKU x month x complaint rollup (the PowerBI grain), paginated
  /monthly.csv      the same rollup, same filters; a SKU-month's orders/units/
                    refunds ride on ONE row (exposure_row=1) so SUM is correct
  /monthly_sku.csv  one row per SKU-month — the pivot export (R1)
  /rules            the picklist -> code mapping, published for review: it labels the
                    8,516 records no prose sample can reach
  /codes            proposed codes: approve (joins the vocabulary AND the classifier's
                    vocabulary file) or deny (durable; the replacement is required and
                    pre-selected when one was prescribed)
  /quality          the weekly 45-row random check: record a call per row
  /export.csv       the current filter as CSV (?text=1 adds the review text)
  /login            branded sign-in form (email + password), sets a signed cookie
  /logout           clears the session cookie
  /healthz          unauthenticated liveness probe for the deploy script

Product identity (product / parent / sku / asin / title) is enriched into
`classifications` by enrich_products.py on the refresh host, straight from
amzmanager.all_products. This app never connects to the source database.

Auth: a branded sign-in page at /login (email + password), not HTTP Basic — the
browser's grey Basic dialog was the complaint. A successful POST sets a signed,
HttpOnly session cookie (HMAC-SHA256 over 'username|expiry', keyed on APP_SECRET,
12 hours) so no password is ever stored client-side. APP_USERS="user:uid:password,..."
is the real list, so every decision is stamped with the person who made it;
APP_USER/APP_PASS/APP_REVIEWER_UID is the single-user form the first deployment
used. The Email field also accepts a full address: the part before '@' is the
username, so chadia@upperechelonproducts.com signs in as chadia. TLS is terminated
by the reverse proxy in front of the box, and X-Forwarded-Proto decides whether
the cookie carries Secure. APP_SECRET has no default: without it the app refuses
to start, because a default key would let anyone forge a session.

This file is deployed on its own (deploy_on_box.sh installs exactly app.py into
/opt/ai-complaints/app), so it stays a single file: dependency-light, stdlib WSGI
+ pymysql, no imports from the rest of the project.
"""
import base64
import csv
import hashlib
import hmac
import io
import math
import os
import random
import re
import secrets
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

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
# Sessions. APP_SECRET is deliberately NOT defaulted: a built-in key would let
# anyone who has read this file mint a valid cookie for any username. The app
# refuses to start without one (see __main__); deploy_on_box.sh generates it.
APP_SECRET = os.environ.get("APP_SECRET", "").strip()
SESSION_COOKIE = "cx_session"
SESSION_HOURS = 12
SAMPLE_N = int(os.environ.get("APP_SAMPLE_N", "45"))
# F9: the weekly sample is drawn from rows with a customer comment from this ISO
# week on. Earlier weeks already have a persisted sample and are not re-drawn;
# their code-only rows are marked N/A on the page instead.
PROSE_POOL_FROM = os.environ.get("APP_PROSE_POOL_FROM", "2026-W44")
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

# Brand assets. app.py is installed on its own (deploy_on_box.sh copies this file
# into $APP_DIR/app/), so the three logo files ship in a `static/` directory
# BESIDE this file and are served by the route in app(). A whitelist, not a path:
# no traversal, no directory listing, and a missing file is a 404 rather than a
# 500. If the directory is absent the pages still render (the <img> 404s) — a
# missing logo must never be a missing login page.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {
    "logo-full.png": "image/png",
    "logo-mark.png": "image/png",
    "logo-mark-light.png": "image/png",
}

CSS = """
:root{--bg:#fbfbfd;--card:#fff;--ink:#1a1a1f;--mut:#6b6b76;--line:#e6e6ec;--accent:#2b5cff;--warn:#b45309;--bad:#b42318;--ok:#067647}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
/* Left sidebar, matching the static site: same 208px rail, same brand block,
   same link treatment. The static page is the reference for both surfaces. */
.wrap{display:flex;min-height:100vh}
aside.side{width:208px;flex:0 0 208px;background:var(--card);border-right:1px solid var(--line);
  padding:24px 0;position:sticky;top:0;height:100vh;overflow:auto;display:flex;flex-direction:column}
.side-brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:16px;
  letter-spacing:-.02em;padding:0 20px 18px;color:var(--ink)}
.side-brand img.mark{height:24px;width:auto;display:block;flex:none}
.side-brand span{display:block;font-size:10px;font-weight:600;color:#9ca3af;
  letter-spacing:.09em;text-transform:uppercase;margin-top:3px}
a.snav{display:block;padding:8px 20px;color:#374151;font-size:13.5px;
  border-left:2px solid transparent;text-decoration:none}
a.snav:hover{background:#fafbfc;text-decoration:none;color:#374151}
a.snav.on{color:#2563eb;font-weight:650;background:#f5f8ff;border-left-color:#2563eb}
.side-sep{margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
.side-sep .seplbl{display:block;padding:0 20px 4px;font-size:10px;font-weight:700;
  letter-spacing:.09em;text-transform:uppercase;color:#9ca3af}
.side-foot{margin-top:auto;padding:14px 20px 0;border-top:1px solid var(--line);
  font-size:11.5px;color:var(--mut);line-height:1.6}
.side-foot b{color:var(--ink)}
.side-foot a{display:block;margin-top:5px;color:var(--mut);font-weight:600;
  border-bottom:1px solid var(--line);width:max-content}
.side-foot a:hover{color:var(--ink);text-decoration:none;border-bottom-color:var(--ink)}
main{flex:1;min-width:0;max-width:1400px;margin:0;padding:26px 28px 60px}
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
/* Provenance: WHO produced the label this row reports as. Distinct from the
   status pill beside it, which appears only when the row has NO label — a
   labelled row can be AI-classified or human-classified and carries no status. */
.pill.prov{background:#f4f4f5;border-color:#e4e4e7;color:#52525b}
.pill.prov.human{background:#eef3ff;border-color:#c9d9f8;color:#1d4ed8}
.pill.prov.excl{background:#fdf3f2;border-color:#f2d3cf;color:#a4504a}
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
.who a{margin-left:12px;color:var(--mut);font-weight:600;border-bottom:1px solid var(--line)}
.who a:hover{color:var(--ink);text-decoration:none;border-bottom-color:var(--ink)}
.decision{background:#fafafc;border:1px solid var(--line);border-radius:8px;padding:11px 13px;margin-bottom:12px;font-size:13px}
.decision b{font-weight:650}

/* ---------------------------------------------------------------- row links
   The row is the control: pointer, a hover wash and an explicit "open" in the
   last cell, because a table that opens something must look like it does. */
tr.rowlink{cursor:pointer}
tr.rowlink:hover td{background:#f5f8ff}
tr.rowlink:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
td.open-cell{white-space:nowrap;color:var(--mut);font-size:12px;text-align:right}
tr.rowlink:hover td.open-cell{color:var(--accent)}
td.open-cell a{color:inherit;text-decoration:none}
.chev{display:inline-block;margin-left:3px;font-size:15px;line-height:1;transform:translateY(1px)}
a.lnk{color:var(--accent);text-decoration:underline;text-underline-offset:2px;text-decoration-thickness:1px}
a.lnk:hover{text-decoration-thickness:2px}

/* ---------------------------------------------------------------- list text
   The REVIEW cell is clamped to three lines and the cell itself opens the
   record — the drawer has the full text. A hard character slice (the old
   txt[:140]) cut mid-word with no way to read the rest; a line clamp ends on a
   line boundary with an ellipsis and never hides content for good. */
td.rv{min-width:300px;max-width:520px;cursor:pointer}
.rvclip{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;
  white-space:normal;line-height:1.45;font-size:13px;color:#4b5563}
.rvclip .chip{margin-top:1px}
.rvprose{display:inline}
.rvprose.none{color:#9d174d}

/* ---------------------------------------------------------------- drawer
   One panel, 620px, its own scroll, over a dimmed backdrop. It replaces the
   navigation to a second tab: the table stays behind it and the URL stays put. */
body.drw-open{overflow:hidden}
.drw-bd{position:fixed;inset:0;z-index:55;background:rgba(15,23,42,.44);opacity:0;
  transition:opacity .18s ease}
.drw-bd.on{opacity:1}
.drw{position:fixed;top:0;right:0;bottom:0;z-index:60;width:100%;max-width:620px;
  transform:translateX(101%);transition:transform .22s cubic-bezier(.32,.72,0,1);will-change:transform}
.drw.on{transform:none}
.drw-panel{position:relative;height:100%;overflow-y:auto;overscroll-behavior:contain;
  background:var(--card);border-left:1px solid var(--line);box-shadow:-24px 0 60px rgba(16,24,40,.18)}
.drw-x{position:absolute;top:14px;right:16px;z-index:2;display:flex;align-items:center;justify-content:center;
  width:34px;height:34px;padding:0;border:1px solid var(--line);border-radius:8px;background:#fff;
  color:var(--mut);font-size:19px;line-height:1;cursor:pointer}
.drw-x:hover{background:#f6f6fa;color:var(--ink)}
.drw-body{padding:22px 24px 40px;font-size:14px;line-height:1.6;color:var(--ink)}
.drw-head{padding-right:46px}
.drw-kicker,.drw-meta,.drw-sub{font-size:12px;line-height:1.55;color:var(--mut)}
.drw-title{font-size:20px;font-weight:650;letter-spacing:-.01em;line-height:1.3;margin:4px 0 6px}
.drw-sec{margin-top:24px}
.drw-sec>h3{font-size:20px;line-height:1.25;font-weight:650;letter-spacing:-.01em;margin:0 0 10px}
.drw-sec .lead{font-size:12px;color:var(--mut);line-height:1.55;margin:0 0 10px}
.drw-quote{border:1px solid var(--line);border-radius:8px;background:#fafafc;padding:16px;
  font-size:14px;line-height:1.65;white-space:pre-wrap}
.drw-quote .txtwrap{padding:0 0 10px;border-bottom:1px solid #f1f1f5;margin-bottom:10px}
.drw-quote .txtwrap:last-child{border:none;margin:0;padding:0}
.lblrow{display:flex;gap:12px;align-items:baseline;padding:4px 0;font-size:14px;line-height:1.5}
.lblrow .k{flex:0 0 108px;font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--mut)}
.dec{border:1px solid var(--line);border-radius:8px;background:#fafafc;padding:15px 16px;
  font-size:14px;line-height:1.6}
.dec .lblrow:first-child{padding-top:0}
.dec .lblrow:last-child{padding-bottom:0}
.dec .m{font-size:12px;line-height:1.55;color:var(--mut);margin-top:6px}
/* One border treatment for every boxed block in the panel: 1px, 8px radius. A
   neighbour with a heavier or rounder border reads as a different kind of thing. */
.act{border:1px solid var(--line);border-radius:8px;background:#fff;padding:16px;margin:0 0 12px;
  display:flex;flex-direction:column;gap:12px}
.act h4{font-size:14px;font-weight:650;line-height:1.4;margin:0}
.act p{font-size:14px;line-height:1.55;color:var(--mut);margin:0}
.act label{margin:0}
.act select,.act input,.act textarea{width:100%;min-width:0}
.act .btnrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.act .hint{font-size:12px;color:var(--mut)}
.act button{align-self:flex-start}
.hist{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}
.hist li{border-left:2px solid var(--line);padding:1px 0 1px 12px;font-size:14px;line-height:1.55}
.hist li.cur{border-left-color:var(--accent)}
.hist .m{font-size:12px;line-height:1.55;color:var(--mut);margin-top:2px}
.kv{display:grid;grid-template-columns:118px 1fr;gap:7px 14px;margin:0;font-size:14px;line-height:1.5}
.kv dt{font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--mut);padding-top:2px}
.kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
@media(max-width:680px){.drw{max-width:100%}.drw-body{padding:18px 16px 32px}}
/* Under 900px the rail becomes a top bar, exactly as the static page does it:
   the same six links in one scrolling row, brand left, Sign out right. */
@media(max-width:900px){
  .wrap{display:block}
  aside.side{position:static;width:auto;flex:none;height:auto;flex-direction:row;align-items:center;
    gap:2px;padding:8px 10px;border-right:0;border-bottom:1px solid var(--line);overflow-x:auto}
  .side-brand{padding:0 12px 0 4px;gap:7px;white-space:nowrap;flex:none}
  .side-brand span{display:none}
  a.snav{padding:6px 10px;border-left:0;border-bottom:2px solid transparent;white-space:nowrap;flex:none}
  a.snav.on{border-left:0;border-bottom-color:var(--accent);background:transparent}
  .side-sep{margin:0;padding:0;border:0;display:contents}
  .side-sep .seplbl{display:none}
  .side-foot{margin:0 0 0 auto;padding:0 4px 0 8px;border:0;white-space:nowrap;flex:none;font-size:11.5px}
  .side-who{display:none}
  .side-foot a{display:inline;margin:0;border:0}
  main{padding:18px 16px 48px}
  /* Narrow screens: nothing may push the body wider than the viewport. Native
     selects size to their longest option, and the mix table sized to content;
     both are bounded here rather than left to overflow the page. */
  form.filters>div{flex:1 1 140px;min-width:0}
  form.filters select,form.filters input{min-width:0;width:100%;max-width:100%}
  .mix{overflow:hidden}
  .mixgrid{grid-template-columns:minmax(0,1fr)}
  .mixtbl{table-layout:fixed;width:100%}
  .mixtbl td.c{max-width:none;white-space:normal}
}
"""


# The drawer's client half. Kept next to the page it belongs to rather than in a
# static file: app.py is deployed on its own (deploy_on_box.sh), so an external
# asset would 404 on the box. Progressive enhancement: the "open" anchor and both
# forms are real links/forms, so without JS the review is still reachable.
DRAWER_JS = """<script>
(function () {
  var dlg = document.getElementById('drw'),
      bd  = document.getElementById('drwBd'),
      body = document.getElementById('drwBody'),
      panel = document.getElementById('drwPanel'),
      closeBtn = document.getElementById('drwClose'),
      lastFocus = null, curId = null;

  if (!dlg || !bd || !body) return;

  function isOpen() { return !dlg.hidden; }

  function markRow(id, state) {
    if (!id || !state) return;
    var tr = document.querySelector('tr[data-review-id="' + id + '"]');
    if (!tr) return;
    var pill = tr.querySelector('[data-pill]');
    if (!pill) return;
    var label = state === 'rejected' ? 'not a complaint'
              : state === 'corrected' ? 'corrected' : 'confirmed';
    pill.textContent = label;
    pill.className = 'pill ' + (state === 'rejected' ? 'bad' : 'ok');
  }

  function wireForms() {
    Array.prototype.forEach.call(body.querySelectorAll('form.act'), function (f) {
      f.addEventListener('submit', function (ev) {
        ev.preventDefault();
        var btn = ev.submitter || f.querySelector('button[value]'),
            fd  = new FormData(f),
            enc = [];
        if (btn && btn.name) fd.set(btn.name, btn.value);
        fd.set('drawer', '1');
        // URL-encoded, not multipart: the app reads the body with parse_qs, so a
        // multipart FormData posts arrive as an empty form ("Unknown action").
        fd.forEach(function (v, k) {
          enc.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
        });
        f.classList.add('busy');
        Array.prototype.forEach.call(f.querySelectorAll('button'), function (b) { b.disabled = true; });
        fetch(f.getAttribute('action'), {
          method: 'POST', body: enc.join('&'),
          headers: { 'X-Drawer': '1', 'Content-Type': 'application/x-www-form-urlencoded' },
          credentials: 'same-origin'
        }).then(function (r) {
          var st = r.headers.get('X-Row-State');
          if (st) markRow(curId, st);
          return r.text();
        }).then(function (h) {
          body.innerHTML = h;
          wireForms();
          panel.scrollTop = 0;
          var first = body.querySelector('button, select, input, a');
          if (first) first.focus();
        }).catch(function () {
          f.classList.remove('busy');
          Array.prototype.forEach.call(f.querySelectorAll('button'), function (b) { b.disabled = false; });
        });
      });
    });
  }

  function open(id) {
    lastFocus = document.activeElement;
    curId = String(id);
    fetch('/review/' + curId + '?partial=1', {
      headers: { 'X-Drawer': '1' }, credentials: 'same-origin'
    }).then(function (r) { return r.text(); }).then(function (h) {
      body.innerHTML = h;
      wireForms();
      dlg.hidden = false; bd.hidden = false;
      requestAnimationFrame(function () {
        dlg.classList.add('on'); bd.classList.add('on');
        document.body.classList.add('drw-open');
        panel.scrollTop = 0;
        var first = body.querySelector('button, select, input, a');
        if (first) first.focus();
      });
    });
  }

  function close() {
    if (!isOpen()) return;
    dlg.classList.remove('on'); bd.classList.remove('on');
    document.body.classList.remove('drw-open');
    setTimeout(function () { dlg.hidden = true; bd.hidden = true; }, 200);
    curId = null;
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  document.addEventListener('click', function (ev) {
    var tr = ev.target.closest && ev.target.closest('tr.rowlink');
    if (!tr) return;
    var a = ev.target.closest('a');
    if (a && !a.classList.contains('rowopen')) { ev.stopPropagation(); return; }  // Amazon links win
    ev.preventDefault();
    var id = tr.getAttribute('data-review-id');
    if (id && id !== curId) open(id);
  });

  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && isOpen()) { close(); return; }
    if ((ev.key === 'Enter' || ev.key === ' ') && ev.target.matches &&
        ev.target.matches('tr.rowlink')) {
      ev.preventDefault();
      open(ev.target.getAttribute('data-review-id'));
    }
  });

  if (closeBtn) closeBtn.addEventListener('click', function () { close(); });
  bd.addEventListener('click', function () { close(); });

  // A #review-<id> link still opens the panel (no reload): the drawer is the
  // destination, the list is the page.
  function fromHash() {
    var m = /^#review-(\\d+)$/.exec(location.hash);
    if (m) open(m[1]); else close();
  }
  window.addEventListener('hashchange', fromHash);
  fromHash();
})();
</script>"""


# ---------------------------------------------------------------- plumbing

def db():
    """A connection that cannot hold the server hostage.

    F14: the app served requests one at a time (wsgiref's plain server) and had
    no statement timeout, so one slow query froze every page for ~18 minutes and
    blocked the nightly RENAME that rebuilds review_duplicates. The server is now
    threaded (see ThreadingWSGIServer) AND every connection carries a statement
    timeout, because threading alone turns one slow query into N concurrent slow
    queries against a box that is also running the refresh.
    """
    conn = pymysql.connect(
        host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
        password=os.environ["CXAPP_DB_PASSWORD"], database=os.environ["CXAPP_DB_NAME"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=True,
        connect_timeout=int(os.environ.get("APP_DB_CONNECT_TIMEOUT", "10")),
        read_timeout=int(os.environ.get("APP_DB_READ_TIMEOUT", "60")))
    cur = conn.cursor()
    # MariaDB: max_statement_time is in seconds (fractional allowed). A page that
    # needs longer than this is a bug in the page, not a reason to block the app.
    try:
        cur.execute("SET SESSION max_statement_time = %s",
                    (float(os.environ.get("APP_STMT_TIMEOUT", "30")),))
    except pymysql.err.MySQLError:
        pass                      # MySQL/older MariaDB: read_timeout still bounds it
    return conn


def esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def page(title, body, active="", msg="", err=False, ctx=None):
    ctx = ctx or {}
    who = ctx.get("who")
    # The three primary links are the static site's sidebar, in its order and
    # with its styling: the two surfaces are one product and must not look like
    # two. Dashboard/Monthly/Rules are app-only pages, so they sit below a
    # separator instead of pushing Reviews off the top.
    def link(u, n):
        return f'<a href="{u}" class="snav{" on" if active == u else ""}">{n}</a>'
    nav = "".join(link(u, n) for u, n in
                   [("/", "Overview"), ("/reviews", "Reviews"), ("/codes", "New codes"),
                    ("/quality", "Quality"), ("/monthly", "Monthly"), ("/rules", "Rules")])
    flash = f'<div class="flash{" err" if err else ""}">{esc(msg)}</div>' if msg else ""
    who_html = ""
    if who:
        ident = f'signed in as <b>{esc(who["name"])}</b>'
        if who.get("uid"):
            ident += f' · uid {who["uid"]}'
        who_html = f'<div class="side-who">{ident}</div><a href="/logout">Sign out</a>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · AI Complaint Validation</title><style>{CSS}{ctx.get("css", "")}</style></head>
<body><div class="wrap">
<aside class="side">
  <div class="side-brand"><img class="mark" src="/static/logo-mark-light.png" alt="UEP" width="84" height="24"><span>Complaint validation</span></div>
  {nav}
  <div class="side-foot">{who_html}</div>
</aside>
<main>{ctx.get("banner", "")}{flash}{body}</main>
</div>
{ctx.get("scripts", "")}</body></html>"""


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


def text_clip(t):
    """The list cell: chips above the FULL comment, clamped by CSS to three
    lines. No character slice here — slicing is what cut words in half and left
    the reader no way to see the rest. Clicking the cell opens the drawer,
    which repeats it unclamped."""
    chips, prose = text_parts(t)
    head = chips_html(chips, limit=2)
    if prose:
        body = f'<span class="rvprose">{esc(" ".join(prose))}</span>'
    else:
        body = '<span class="rvprose none">code only — nothing to check</span>'
    return f'<div class="rvclip">{head}{body}</div>'


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


# Review Deletion is the rule's verdict on empty or non-complaint text, not
# something a customer complained about — and not the same thing as a repeat
# (dup_rank), which is one comment recorded twice on one order. It stays in the table and the export
# (so nothing disappears) but not in any distribution.
PROCESS_NOISE = "Review Deletion"
# Rows with no resolved complaint (marked not a complaint, or nothing assigned).
# The mix shows them as their own bucket so its counts still reconcile to the
# table's match count instead of quietly summing short.
NO_CODE = "(no complaint assigned)"
# Display-only renames: the stored value never changes, so the pivot, the
# export and the training data keep one vocabulary.
DISPLAY_LABEL = {"vague complaint": "Unspecified"}


def display_label(c, cur=None):
    """The one label reader. With a cursor it also resolves the TAXONOMY spelling,
    case-insensitively: the classifier wrote 'Not Needed/unwanted' while the
    taxonomy says 'Not Needed/Unwanted', and printing both for one complaint reads
    as two complaints."""
    if cur is not None:
        c = canonical(cur, c)
    return DISPLAY_LABEL.get(norm(c), c)


# complaint_codes is the taxonomy: one spelling per code. The classifier emits its
# own casing ('Not Needed/unwanted', 'Not As Advertised/misleading'), so anything
# displayed goes through this map rather than being echoed as stored. Cached for a
# minute because every page renders dozens of labels and the table changes once a
# week at most.
_TAXONOMY = {"at": 0.0, "map": {}}


def taxonomy(cur, ttl=60):
    now = time.time()
    if _TAXONOMY["map"] and now - _TAXONOMY["at"] < ttl:
        return _TAXONOMY["map"]
    try:
        cur.execute("SELECT code FROM complaint_codes")
        _TAXONOMY["map"] = {norm(r["code"]): r["code"] for r in cur.fetchall() if r["code"]}
        _TAXONOMY["at"] = now
    except Exception:                                        # noqa: BLE001
        pass                       # a label is not worth a 500: fall back to stored
    return _TAXONOMY["map"]


def canonical(cur, c):
    """Taxonomy spelling for a stored complaint, case-insensitive; unchanged if unknown."""
    if not c:
        return c
    return taxonomy(cur).get(norm(c), c)


def amz_asin(asin, title=""):
    """The ASIN, linked to its Amazon detail page. The product title rides in the
    tooltip: the column no longer carries a title line, but the title must stay
    one hover away. No ASIN -> an em dash, never a link to /dp/."""
    if not asin:
        return "—"
    tip = f' title="{esc(title.strip())}"' if (title or "").strip() else ""
    return (f'<a href="{AMZ_DP}{urllib.parse.quote(asin)}" target="_blank" '
            f'rel="noopener" class="mono lnk" data-ext="1" onclick="event.stopPropagation()"'
            f'{tip}>{esc(asin)}</a>')


def amz_sku(sku):
    if not sku:
        return "—"
    return (f'<a href="{AMZ_SKU_SEARCH}{urllib.parse.quote(sku)}" target="_blank" '
            f'rel="noopener" class="mono lnk" data-ext="1" onclick="event.stopPropagation()">'
            f'{esc(sku)}</a>')


def short_title(t, n=90):
    """Truncate on a word boundary: 'for Hip and Bac' reads as a bug, '…Bac…' not much better."""
    t = (t or "").strip()
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0].rstrip(" ,-–—")
    return cut + "…"


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
        # F8: EXACT by default. A substring match made sku=6 return 3,655 rows
        # against 2,613 for the SKU actually called '6' — the filter silently
        # widened to '6-in-anything'. 'contains' is an explicit choice, and the
        # page echoes the SKU values it matched.
        if q.get("skulike") == "1":
            where.append("v.sku LIKE %s"); args.append(f'%{q["sku"]}%')
        else:
            where.append("v.sku=%s"); args.append(q["sku"].strip())
    if q.get("complaint"):
        # Case-insensitive on purpose: the picklist is the TAXONOMY spelling while
        # older rows store the classifier's casing ('Not Needed/unwanted'). An exact
        # match would filter 7,861 records down to nothing.
        where.append("LOWER(v.final_complaint)=LOWER(%s)"); args.append(q["complaint"].strip())
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
    if st in ("nolabel", "new"):
        # The ONLY state that is waiting for a person: no label at all, because
        # the classifier could not read a complaint here. Legitimate: rows the AI
        # labelled — confident or not — are finished work, not a queue.
        where.append("v.final_complaint IS NULL")
    elif st == "decided":
        where.append("v.human_validated=1")
    elif st in ("unsure", "flagged"):
        # Confidence, not a queue. These rows all carry a label; the filter exists
        # so anyone auditing the classifier can reach its low-confidence calls.
        where.append("v.needs_review=1")
    elif st == "newcode":
        where.append("v.is_new_code=1")
    elif st == "ai":
        # Provenance, not status: no human decision exists, so the label on
        # screen is the classifier's.
        where.append("v.human_decision IS NULL")
    elif st == "human":
        # accepted + corrected only. A rejected row carries a human decision but
        # reports no label at all, so it must not answer a 'Human Classified'
        # filter — that would put an 'Excluded' badge under a human filter.
        where.append("v.human_decision IN ('accepted','corrected')")
    elif st == "excluded":
        where.append("v.human_decision='rejected'")
    txt = q.get("tx") or ""
    if txt == "codeonly":
        # No free text: only the return-reason picklist, so there is nothing to
        # read the label against. Filtering these out of the working list is the
        # difference between a queue of 2,104 and a queue of 1,727.
        where.append("v.has_prose=0")
    elif txt == "prose":
        where.append("v.has_prose=1")
    if q.get("dup") == "1":
        # One row per comment per ORDER (F2: same order_id + same MD5(text)).
        # Never applied unless asked for: the default counts every row and
        # discloses the repeats, because a silent dedupe would change every number
        # on the page without telling the reader.
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
# What this panel may and may not say (sixth review: F1, F3, F5, F12, F13).
#
#  * A DENOMINATOR NOW EXISTS, AND IT IS THE SELLER'S OWN SALES. Since 2026-10-19
#    `sales_by_sku_month` (sku x month, built by build_sales.py from
#    amzmanager.finance_by_products) carries units/orders/refunds per SKU-month, and
#    v_complaint_rates turns it into complaints per 100 orders. So the panel states
#    the EXPOSURE behind the filtered records and the rate when the join covers
#    them. It still cannot call a rise a "return rate": the numerator is complaint
#    RECORDS, and a launch or a promotion moves both sides. When no sales row
#    covers the filter the line says exactly that — never a blank, never a 0.
#
#  * THE CHANGE COLUMN COMPARES RECORDS PER FEED DAY ACROSS WINDOWS OF DIFFERENT
#    FEED COMPOSITION. ncx holds 3,433 records in the prior window and none after
#    2026-07-16; amazon stops in August; the return-reason-only share of rows
#    moves by ~15pp. Comparing those totals measures the FEEDS changing, not
#    customers: on the one feed present in both windows (fba) Too Big is -17%
#    all-source but +41% fba-only, and 11 of the top 14 rows flip sign. So the
#    change is computed ONLY across the feeds that cover both windows, and when a
#    feed is missing from one window, or stops inside it, or the code-only share
#    moves by more than 10pp, the arrows are WITHHELD and the reason is printed.
#    A blank cell that explains itself beats an arrow pointing the wrong way.
#
#  * THE DENOMINATOR IS CALENDAR DAYS IN THE WINDOW INTERSECTED WITH THE DAYS THE
#    WHOLE FEED COVERS. The feed carries every calendar day (141 distinct dates
#    for 2026-06-01..10-19, exactly the calendar count), so a day with no row for
#    a SKU is a genuine zero and must be divided by. Counting only the days a
#    SELECTION has rows for divides by 55 instead of 61 for seat cushions and
#    turns a +118% change into +96%.
#
#  * TODAY IS NOT A COMPLETE DAY (35 rows on 2026-10-19 against 176 on 10-18).
#    The rates stop at yesterday and the page names the day it dropped.

NO_SOURCE = "(no source)"
COVER_SLACK = 3          # days a feed may stop short of a window end and still count as covered


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
    """(start, end, basis) for the change column — the window the READER asked for.

    `plot` is the TRIMMED, contiguous month list the series draws. A filter with
    one stray 2025-07 row used to stretch the default window over 396 days for a
    two-month chart; the window now comes from the months that are actually
    plotted.

    Explicit dates win; a month filter means THAT MONTH'S CALENDAR BOUNDS (F15 —
    it used to be truncated to the last row's date, which turned
    /reviews?month=2025-07 into a 2-day "window"); neither means the last three
    months plotted here, and the page says so. No clamping happens here: the
    caller decides what to do with a window that runs past today, because a
    calendar window and a rate window are not the same thing.
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


def _rate_end(t, today=None):
    """The last day a rate may count: never today, never the future.

    A day still in progress holds a fraction of its records (35 against 176), so
    dividing by it deflates the current rate and turns a flat trend into a fall.
    """
    today = today or date.today()
    if t is None:
        return None
    return min(t, today - timedelta(days=1))


def _win_filter(q, a, b):
    """build_where() for one window: the month/date filters are replaced by a..b."""
    qq = {k: v for k, v in q.items() if k not in ("month", "from", "to")}
    qq["from"], qq["to"] = a.isoformat(), b.isoformat()
    return build_where(qq)


def _window_src(cur, q, a, b):
    """{source: {n, np, mn, mx, codes}} for one window, under the same filters.

    One query per window: per-source totals are what the like-for-like test needs,
    and per-(source, complaint) counts are what the change column needs once the
    comparable feeds are known.
    """
    w, args = _win_filter(q, a, b)
    cur.execute(f"""SELECT IFNULL(v.source, %s) s,
                           COALESCE(v.final_complaint, %s) c,
                           COUNT(*) n, SUM(v.has_prose=0) np,
                           MIN(v.review_date) mn, MAX(v.review_date) mx
                      FROM v_ai_complaints v {w}
                     GROUP BY s, c""", [NO_SOURCE, NO_CODE] + args)
    out = {}
    for r in cur.fetchall():
        d = out.setdefault(r["s"], {"n": 0, "np": 0, "mn": None, "mx": None,
                                    "codes": defaultdict(int)})
        k = int(r["n"] or 0)
        d["n"] += k
        d["np"] += int(r["np"] or 0)
        d["codes"][r["c"]] += k
        if r["mn"] and (d["mn"] is None or r["mn"] < d["mn"]):
            d["mn"] = r["mn"]
        if r["mx"] and (d["mx"] is None or r["mx"] > d["mx"]):
            d["mx"] = r["mx"]
    return out


def _merge_src(src, only=None):
    """(codes, total, no_prose) over the sources in `only` (None = all of them)."""
    codes, tot, np = defaultdict(int), 0, 0
    for s, d in src.items():
        if only is not None and s not in only:
            continue
        tot += d["n"]
        np += d["np"]
        for c, v in d["codes"].items():
            codes[c] += v
    return codes, tot, np


def _source_max(cur):
    """{source: last review_date the feed carries} — for 'the ncx feed ends X'."""
    cur.execute("""SELECT IFNULL(source, %s) s, MAX(review_date) mx
                     FROM v_ai_complaints GROUP BY s""", (NO_SOURCE,))
    return {r["s"]: r["mx"] for r in cur.fetchall()}


def feed_days(cur, a, b):
    """Calendar days in [a,b] that the WHOLE feed covers — UNFILTERED.

    This is the denominator of every rate. It is deliberately not per selection:
    the feed carries every calendar day, so a day with no row for a SKU is a real
    zero and must be divided by. It is also not the raw calendar length: the
    corpus starts 2026-06-01, and days before the feed existed are not zeros,
    they are not data at all.
    """
    cur.execute("""SELECT COUNT(DISTINCT review_date) d FROM v_ai_complaints
                    WHERE review_date BETWEEN %s AND %s""", (a, b))
    return int(cur.fetchone()["d"] or 0)


def sel_days(cur, q, a, b):
    """Days the SELECTION has rows for — used only to describe coverage, never as
    a denominator: it hides the zero days that make a rate a rate."""
    w, args = _win_filter(q, a, b)
    cur.execute(f"SELECT COUNT(DISTINCT v.review_date) d FROM v_ai_complaints v {w}", args)
    return int(cur.fetchone()["d"] or 0)


def _pct(cur_n, prev_n, cur_days, prev_days):
    """Percentage change between two RATES, or None when a rate is impossible."""
    if not cur_days or not prev_days or not prev_n:
        return None
    return ((cur_n / cur_days) / (prev_n / prev_days) - 1) * 100.0


def _riser_tail(cv, pv, cd, pd):
    """The parenthetical after '+N records' in the riser (F13).

    A count can rise while its per-day rate falls or barely moves: the prior
    window is a different length, so 'Doesnt Fit +137 (-0%)' was printable — a
    negative percentage beside a positive count, which reads as a bug and is one.
    Below |1%| the answer is 'flat per day'; above it, the sign always agrees with
    the count because the rate is what changed.
    """
    p = _pct(cv, pv, cd, pd)
    if p is None:
        return "new"
    if abs(p) < 1:
        return "flat per day"
    return f"{p:+.0f}% per day"


def _delta_cell(cur_n, prev_n, cur_days, prev_days, title="", flat="±0%"):
    """One change cell, as a change in RECORDS PER FEED DAY.

    Both counts come from their own window and both are divided by the calendar
    days THAT WINDOW'S FEED covers (feed_days), so a 61-day baseline is not
    compared as if it held 80 days and a zero day still counts as a zero.

    Below |1%| the cell says '±0%' rather than printing a direction it cannot
    support: a count that rose while its daily rate fell is not '▲'.
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
        return f'<span class="dl flat"{tt}>{flat}</span>'
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


def _nice_axis(v):
    """A readable top-of-axis value: 1, 2, 2.5, 5 x 10^k, never 1.37."""
    if v <= 0:
        return 1
    import math as _m
    k = 10 ** int(_m.floor(_m.log10(v)))
    for mult in (1, 2, 2.5, 5, 10):
        if v <= mult * k * 1.0001:
            return mult * k
    return 10 * k


def _mix_svg(points, partial_day=None, rate=None, breaks=()):
    """Two series, one point per month, server-side SVG — no JS dependency.

    points: [(month 'YYYY-MM', n)]. data-points carries the count so a test can
    assert the series length without parsing the path.
    rate:   {month: complaints per 100 orders} or None — drawn on a RIGHT axis as a
            dashed second series (R10). The counts alone can rise while the rate
            falls (a launch, a promotion, a harvest); the rate is the line that
            answers "is it up?".
    breaks: month indices where the set of contributing feeds changes. Those months
            are shaded and marked with a dashed rule, because a rate whose
            composition changed is not comparable with the month before it (R4).

    A month that is still running is drawn as a DASHED last segment and labelled
    '(to the 19th)'. Plotting the 19 days of October as a whole month made a flat
    daily rate read as a 36% fall — the most expensive kind of wrong, because it
    is the shape she quotes.
    """
    if not points:
        return ""
    W, H, L, R, T, B = 520, 172, 36, 40, 14, 26
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

    # ---- feed-mix changes: shade the month and mark the boundary --------------
    bands = ""
    half = (iw / max(n - 1, 1)) / 2
    for i in sorted({b for b in breaks if 0 <= b < n}):
        x0, x1 = max(L, xs[i] - half), min(W - R, xs[i] + half)
        bands += (f'<rect x="{x0:.1f}" y="{T}" width="{max(x1 - x0, 2):.1f}" height="{ih:.1f}" '
                  f'fill="#f59e0b" opacity=".10"/>'
                  f'<line x1="{xs[i]:.1f}" y1="{T}" x2="{xs[i]:.1f}" y2="{T + ih:.1f}" '
                  f'stroke="#b45309" stroke-width="1" stroke-dasharray="3 3" opacity=".55"/>')

    # ---- the rate series, right axis ----------------------------------------
    rline = ""
    if rate:
        vals = [rate.get(m) for m, _ in points]
        live = [v for v in vals if v is not None]
        if live:
            mxr = _nice_axis(max(live))
            yr = lambda v: T + ih - (v / mxr) * ih  # noqa: E731
            rline += (f'<text x="{W - R + 5}" y="{T + 4}" font-size="9.5" fill="#b45309">{mxr:g}</text>'
                      f'<text x="{W - R + 5}" y="{T + ih + 3}" font-size="9.5" fill="#b45309">0</text>')
            seg = []
            segs = []
            for i, v in enumerate(vals):
                if v is None:
                    if seg:
                        segs.append(seg)
                    seg = []
                else:
                    seg.append((xs[i], yr(v)))
            if seg:
                segs.append(seg)
            for s in segs:
                if len(s) > 1:
                    rline += (f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in s)}" '
                              f'fill="none" stroke="#b45309" stroke-width="1.8" '
                              f'stroke-dasharray="5 3" stroke-linejoin="round"/>')
            for i, v in enumerate(vals):
                if v is None:
                    continue
                rline += (f'<circle cx="{xs[i]:.1f}" cy="{yr(v):.1f}" r="2.6" fill="#b45309">'
                          f'<title>{points[i][0]}: {v:.2f} per 100 orders'
                          f'{"; feeds changed here" if i in breaks else ""}</title></circle>')

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
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Complaints per month, and per 100 orders" '
            f'data-points="{n}" data-partial="{partial_day or 0}" data-breaks="{len(list(breaks))}" '
            f'style="width:100%;height:auto;display:block">'
            f'{bands}{grid}{lab}{area}{line}{rline}{dots}{labels}</svg>')


# Must equal apply_views.RATE_FLOOR. A rate quoted below it is arithmetic on a
# handful of orders, not a level: /monthly?sku=S-Hum printed 200.00 per 100
# orders because one 0-order month contributed a complaint and the other month
# supplied the single order it was divided by.
RATE_FLOOR = 100


def _scope_where(q):
    """The PRODUCT predicates of a filter, and nothing else (R2).

    The complaint predicate is deliberately absent: a rate conditioned on the
    complaint can only ever divide by the orders that produced that complaint, so
    "Too Firm, seat cushions, July" was 27 records over the 26,107 orders in the 7
    SKU-months that happened to record it, against a family total of 32,848 — a
    0.10 that read 25.8% higher than the true 0.08. Every order in the family could
    have produced a Too Firm complaint, so every one of them belongs in the
    denominator. Same for source and country: a complaint from one feed or one
    marketplace can come from any order, so neither narrows the exposure.
    """
    where, args = [], []
    if q.get("product"):
        where.append("(product LIKE %s OR title LIKE %s)")
        args += [f'%{q["product"]}%'] * 2
    if q.get("family"):
        where.append("product_family=%s"); args.append(q["family"])
    if q.get("parent"):
        where.append("parent=%s"); args.append(q["parent"])
    if q.get("sku"):
        if q.get("skulike") == "1":
            where.append("sku LIKE %s"); args.append(f'%{q["sku"]}%')
        else:
            where.append("sku=%s"); args.append(q["sku"].strip())
    return ("WHERE " + " AND ".join(where)) if where else "", args


def scope_skus(cur, q):
    """Every SKU the corpus attributes to this product scope, in ANY month.

    Not just the SKUs that complained in the months in view: a family's August
    orders include the SKU that recorded nothing in August. Taking the SKU set from
    the whole rollup and then summing sales for all of it is what makes the family
    denominator 39,390 for August instead of the 38,513 that a complaint-conditioned
    join produced.

    The boundary this cannot cross: a SKU that has never appeared in a complaint
    cannot be attributed to a family from this database, so its orders are not in
    any family denominator. That is a known, small omission and it is documented.
    """
    w, args = _scope_where(q)
    cur.execute(f"SELECT DISTINCT sku FROM v_ai_complaints_monthly {w}"
                f"{' AND' if w else ' WHERE'} sku IS NOT NULL", args)
    return [r["sku"] for r in cur.fetchall()]


def month_orders(cur, q, months):
    """{month: orders} for the product scope — the denominator, all SKUs, no code.

    One row per (sku, month) in sales_by_sku_month, summed once per month, so no
    complaint code can multiply it.
    """
    skus = scope_skus(cur, q)
    if not skus or not months:
        return {}
    ph = ",".join(["%s"] * len(skus))
    cur.execute(f"""SELECT month, SUM(orders) orders, SUM(units) units,
                           SUM(refunds_us) refunds_us, SUM(orders_us) orders_us,
                           COUNT(*) sku_months, SUM(orders = 0) zero_months
                      FROM sales_by_sku_month
                     WHERE sku IN ({ph}) AND month BETWEEN %s AND %s
                     GROUP BY month""", skus + [months[0], months[-1]])
    return {r["month"]: r for r in cur.fetchall()}


def exposure(cur, q, monthly=False, months=None):
    """Orders/units behind the PRODUCT FILTER — the denominator (R2, R3).

    Two queries, on purpose:

      1. the numerator: the filter AS ASKED (complaint code included), at
         (sku, month), LEFT JOINed to sales so the excluded records can be counted
         rather than silently dropped;
      2. the denominator: the same product scope with the complaint predicate
         REMOVED, every SKU the corpus knows for that scope, every month in
         `months`, including SKU-months that recorded nothing.

    `months` is the window the figure describes (a list of 'YYYY-MM'). It matters:
    the corpus holds a handful of 2023–2025 stragglers, and dividing a five-month
    selection by four years of orders reports the CORPUS's coverage as a rate.
    The caller passes the window it is describing — the charted months on
    /reviews, the months of the selection on /monthly — and any record outside it
    is counted as `outside`, never silently divided.

    `monthly=True` reads the month-grain rollup (for /monthly, whose from/to are
    months, not dates); the default reads the record grain (for /reviews).
    """
    if monthly:
        w, args = build_where_monthly(q)
        num = (f"SELECT sku, month AS m, SUM(n_complaints) n FROM v_complaint_rates {w} "
               f"GROUP BY sku, month")
    else:
        w, args = build_where(q)
        # The numerator is COMPLAINT RECORDS, the same set v_ai_complaints_monthly
        # counts: a resolved complaint, not 'Review Deletion', not an unusable
        # category. Without this clause /reviews rated 472 records against the 468
        # /monthly shows for the same filter.
        wx = (w + " AND " if w else "WHERE ") + (
            "v.sku IS NOT NULL AND v.review_date IS NOT NULL "
            "AND v.final_complaint IS NOT NULL AND v.final_complaint <> %s "
            "AND (v.final_category IS NULL OR v.final_category NOT IN ('NEW','NO_TEXT','ERR'))")
        args = args + [PROCESS_NOISE]
        num = (f"SELECT v.sku, DATE_FORMAT(v.review_date,'%%Y-%%m') m, COUNT(*) n "
               f"FROM v_ai_complaints v {wx} "
               f"GROUP BY v.sku, DATE_FORMAT(v.review_date,'%%Y-%%m')")
    cur.execute(f"""SELECT x.sku, x.m, x.n, s.orders, s.units, s.refunds_us, s.orders_us
                      FROM ({num}) x
                      LEFT JOIN sales_by_sku_month s
                             ON s.sku = x.sku AND s.month = x.m""", args)
    rows = cur.fetchall()

    outside = 0
    if months:
        keep = set(months)
        outside = sum(int(r["n"] or 0) for r in rows if r["m"] not in keep)
        rows = [r for r in rows if r["m"] in keep]
    else:
        months = sorted({r["m"] for r in rows if r["m"]})

    recs = sum(int(r["n"] or 0) for r in rows)
    nosales = sum(int(r["n"] or 0) for r in rows if r["orders"] is None)
    zero = sum(int(r["n"] or 0) for r in rows if r["orders"] == 0)
    # R3: a record in a 0-order month has no denominator. It is COUNTED SEPARATELY,
    # never added to the numerator: that is how a 1-order SKU-month printed 200
    # complaint records per 100 orders.
    cov = recs - nosales - zero
    cond_orders = sum(int(r["orders"] or 0) for r in rows if (r["orders"] or 0) > 0)

    den = list(month_orders(cur, q, months).values())
    orders = sum(int(d["orders"] or 0) for d in den)
    return {
        "recs": recs, "recs_cov": cov, "recs_nosales": nosales, "recs_zero": zero,
        "pairs": len(rows), "covered": sum(1 for r in rows if r["orders"] is not None),
        "cond_orders": cond_orders, "cond_recs": cov, "cond_skus": len(rows),
        "orders": orders,
        "units": sum(int(d["units"] or 0) for d in den),
        "refunds_us": sum(int(d["refunds_us"] or 0) for d in den),
        "orders_us": sum(int(d["orders_us"] or 0) for d in den),
        "sku_months": sum(int(d["sku_months"] or 0) for d in den),
        "zero_months": sum(int(d["zero_months"] or 0) for d in den),
        "months": months,
        "outside": outside,
        "code_filtered": bool((q.get("complaint") or "").strip()),
    }


def _months_label(months):
    """'Jun 2026–Oct 2026', or one month, or ''."""
    if not months:
        return ""
    a, b = months[0], months[-1]
    la = f"{MN[int(a[5:7]) - 1]} {a[:4]}"
    lb = f"{MN[int(b[5:7]) - 1]} {b[:4]}"
    return la if a == b else f"{la}–{lb}"


def exposure_line(cur, q, monthly=False, months=None):
    """The exposure for the product scope, the rate — and every figure it excludes.

    Never blank and never a zero when the truth is "unknown". Three separate
    statements, because they are three different facts and each used to be
    silently merged into a rate:

      * the RATE, over every order in the product scope (R2 — the complaint filter
        does not narrow the denominator);
      * the CONDITIONAL figure, labelled as such, when a code filter is on (R2);
      * what was EXCLUDED from the numerator — no sales row, or 0 orders (R3) —
        and the floor below which no rate is quoted at all (R3).
    """
    e = exposure(cur, q, monthly=monthly, months=months)
    recs = e["recs"]
    if not recs:
        return ""
    orders, units = e["orders"], e["units"]
    months_txt = _months_label(e["months"])
    sm = "s" if e["sku_months"] != 1 else ""

    if not e["sku_months"]:
        return ('<p class="mixnote"><b>Sales exposure:</b> no sales data covers this filter '
                f'({e["pairs"]:,} SKU-month{"s" if e["pairs"] != 1 else ""} in these records, '
                'none of them in <code>sales_by_sku_month</code>), so there is no '
                'complaints-per-100-orders for it. The counts and shares above are unaffected — '
                'they need no denominator.</p>')
    if not orders:
        return ('<p class="mixnote"><b>Sales exposure:</b> '
                f'{e["sku_months"]:,} SKU-month{sm} in scope {months_txt} '
                f'{"have" if e["sku_months"] != 1 else "has"} a sales row but <b>0 orders</b>, so '
                f'the rate is undefined — there is nothing to divide these {recs:,} complaint '
                'records by. It is not 0.00: that would claim no complaints per order, which this '
                'data cannot support.</p>')

    bits = []
    if orders < RATE_FLOOR:
        # R3: below the floor the arithmetic is real and the inference is not.
        bits.append(f'<b>Sales exposure:</b> {orders:,} order{"s" if orders != 1 else ""} — '
                    f'<b>too few to quote a rate</b> (floor {RATE_FLOOR}), across '
                    f'{e["sku_months"]:,} SKU-month{sm} {months_txt}.')
    elif not e["recs_cov"]:
        bits.append(f'<b>Sales exposure:</b> {orders:,} orders across {e["sku_months"]:,} '
                    f'SKU-month{sm} {months_txt}, but none of these {recs:,} records is in a '
                    'SKU-month with orders — no rate is computable for them.')
    else:
        rate = 100.0 * e["recs_cov"] / orders
        tail = f' ({1000.0 * e["recs_cov"] / units:.2f} per 1,000 units)' if units else ""
        bits.append(f'<b>Sales exposure:</b> {orders:,} orders and {units:,} units across '
                    f'{e["sku_months"]:,} SKU-month{sm} {months_txt} — <b>{rate:.2f}</b> '
                    f'complaint records per 100 orders{tail}.')

        # R2: if the complaint predicate HAD been allowed to shape the denominator,
        # this is the number it would have produced. It is labelled, never headline.
        if e["code_filtered"] and e["cond_orders"] and e["cond_orders"] < orders:
            cr = 100.0 * e["recs_cov"] / e["cond_orders"]
            bits.append(f'This code\'s {e["recs_cov"]:,} records sit in {e["cond_skus"]:,} of those '
                        f'SKU-months, which hold {e["cond_orders"]:,} orders: <b>among the '
                        f'{e["cond_skus"]:,} SKU-months that recorded this code</b> the rate is '
                        f'{cr:.2f} per 100. That is a conditional figure — the other '
                        f'{orders - e["cond_orders"]:,} orders could have produced the same '
                        'complaint and did not.')

    # R3: excluded records, stated as counts, never folded into the numerator.
    if e["recs_nosales"] or e["recs_zero"]:
        why = []
        if e["recs_nosales"]:
            why.append(f'{e["recs_nosales"]:,} in SKU-months with no sales row')
        if e["recs_zero"]:
            why.append(f'{e["recs_zero"]:,} in SKU-months that sold 0 orders')
        bits.append(f'Excluded from the numerator: {", ".join(why)} — there is no denominator '
                    'there, and dividing by it would print a rate the data cannot support.')
    if e["sku_months"] and e["zero_months"]:
        bits.append(f'{e["zero_months"]:,} of the {e["sku_months"]:,} SKU-month{sm} in scope sold '
                    '0 orders (shown as “—”, never 0).')
    if e["outside"]:
        bits.append(f'{e["outside"]:,} record{"s" if e["outside"] != 1 else ""} fall outside '
                    f'{months_txt or "this window"} and are not in this rate: the denominator is '
                    'the orders of the months the panel describes, and dividing a five-month '
                    'selection by four years of orders would report the corpus coverage, not a '
                    'complaint rate.')

    # R7: the refund figure, which is the metric that answers the returns question.
    if e["orders_us"] >= RATE_FLOOR:
        rr = 100.0 * e["refunds_us"] / e["orders_us"]
        bits.append(f'<b>US refunds booked in these months:</b> {e["refunds_us"]:,} on '
                    f'{e["orders_us"]:,} US orders = <b>{rr:.2f}%</b> — refunds booked in the '
                    'month ÷ orders placed in the month, not a cohort rate; the feed fills '
                    'refunds for US and CA only, so it is not a worldwide return rate.')
    elif e["orders_us"]:
        bits.append(f'US refunds: {e["refunds_us"]:,} on {e["orders_us"]:,} US orders — too few '
                    'to quote a rate.')

    bits.append('Orders/units are the seller\'s own finance feed for the scope, all '
                'marketplaces; complaints are review records, so this is a record rate, not '
                'a return rate.')
    return '<p class="mixnote">' + " ".join(bits) + '</p>'


def source_rate_table(cur, q, months=None):
    """Complaints per 100 orders BY SOURCE BY MONTH, with each feed's coverage (R4).

    The all-source rate is a composition of feeds. ncx records stop on 2026-07-16
    and amazon on 2026-08-29, so July's all-source peak is partly those feeds
    arriving; on fba — the only feed present in every month — September is the peak.
    A month-over-month comparison is therefore only valid WITHIN one feed, and this
    table is the evidence for that rule rather than a claim about it.
    """
    w, args = _scope_where(q)
    cur.execute(f"""SELECT IFNULL(source,'(none)') s, MIN(review_date) lo,
                           MAX(review_date) hi, COUNT(*) n
                      FROM v_ai_complaints {w} GROUP BY s""", args)
    feeds = {r["s"]: r for r in cur.fetchall()}
    cur.execute(f"""SELECT month, IFNULL(source,'(none)') s, SUM(n_complaints) n
                      FROM v_complaint_rates_by_source {w} GROUP BY month, s""", args)
    cnt = {}
    for r in cur.fetchall():
        cnt.setdefault(r["s"], {})[r["month"]] = int(r["n"] or 0)
    if months is None:
        months = sorted({m for d in cnt.values() for m in d})
    months = [m for m in months if any(m in d for d in cnt.values())]
    if not months or not cnt:
        return ""
    od = month_orders(cur, q, months)

    def cell(s, m):
        n = cnt.get(s, {}).get(m)
        o = int((od.get(m) or {}).get("orders") or 0)
        lo, hi = feeds[s]["lo"], feeds[s]["hi"]
        a, b = _month_bounds(m)
        if a is None:
            return '<td class="num">—</td>'
        if hi is None or a > hi or b < lo:
            return '<td class="num" title="this feed has no records in this month">—</td>'
        # Partial month: say so INSIDE the cell, because a rate over 16 of 31 days
        # divided by a whole month's orders is not comparable with a full month.
        partial = ""
        if lo > a:
            partial = f'<div class="meta">{lo.day}–{b.day} only</div>'
        elif hi < b:
            partial = f'<div class="meta">to the {_ord(hi.day)} only</div>'
        if n is None:
            return f'<td class="num">0<div class="meta">0 records{partial}</div></td>'
        if not o:
            return f'<td class="num">—<div class="meta">{n} records, no orders</div></td>'
        return (f'<td class="num">{100.0 * n / o:.2f}'
                f'<div class="meta">{n:,} rec{partial}</div></td>')

    hdr = "".join(f'<th class="num">{MN[int(m[5:7]) - 1]} {m[:4][2:]}</th>' for m in months)
    trs = "".join(
        f'<tr><td><b>{esc(s)}</b><div class="meta">{feeds[s]["lo"]} → {feeds[s]["hi"]}</div></td>'
        + "".join(cell(s, m) for m in months) + '</tr>'
        for s in sorted(cnt, key=lambda x: (x == "(none)", -sum(cnt[x].values()))))
    return f"""<div class="card" style="margin-top:10px">
<div style="font-size:12.5px;font-weight:650;margin-bottom:2px">Complaints per 100 orders by feed, by month</div>
<div class="cap" style="margin-bottom:6px">Every order in the scope is the denominator for every feed:
a complaint from one feed can come from any order. A cell marked “only” covers part of the month, so it
is not comparable with a full month.</div>
<table><thead><tr><th>Feed</th>{hdr}</tr></thead><tbody>{trs}</tbody></table>
<div class="cap"><b>A month-over-month comparison is only valid within one feed.</b> The all-source rate
is a composition: ncx stops on {feeds.get("ncx", {}).get("hi", "—")} and amazon on
{feeds.get("amazon", {}).get("hi", "—")}, so a month where either is still running is not comparable
with one where it is not. fba is the only feed present every month — it is the series to quote.</div>
</div>"""


def mix_block(cur, q, n):
    """The complaint mix for the current filter: share first, then change, then exposure.

    SHARE IS THE HEADLINE (F5). Share of the mix needs no denominator at all, so
    it is the one comparison a feed change cannot fake; that is why it leads.

    EXPOSURE IS THE SECOND STATEMENT (2026-10-19). The counts here are complaint
    RECORDS; the seller's finance feed now supplies orders/units for the same
    SKU-months, so the panel prints the exposure behind the filtered set and the
    records per 100 orders when that join covers it. It says "no sales data covers
    this filter" when it does not, and never prints 0.

    The change column is a change in RECORDS PER FEED DAY across the feeds that
    cover BOTH windows (F1) — never a change in raw totals, which move when a
    feed starts or stops. When the two windows are not comparable the arrows are
    withheld and the reason is printed beside them.
    """
    today = date.today()
    w, args = build_where(q)
    cur.execute(f"""SELECT COALESCE(v.final_complaint, %s) c, COUNT(*) n,
                           SUM(v.has_prose=0) no_prose, SUM(v.human_validated=1) hv,
                           SUM(v.dup_rank > 1) dupes
                      FROM v_ai_complaints v {w}
                     GROUP BY c ORDER BY n DESC""", [NO_CODE] + args)
    dist = cur.fetchall()

    # The series counts what the distribution counts: final_complaint set and not
    # Review Deletion, or the two halves of one panel disagree.
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

    # ---- the window ------------------------------------------------------
    # F15: an explicit month keeps its CALENDAR bounds. Truncating it to the last
    # row's date turned /reviews?month=2025-07 into "2 days with data against the
    # 1 days with data before" — a window the reader never asked for, and one that
    # hid the days the feed covers but the SKU does not. Missing days are zeros
    # (feed_days), so the window does not need to be shortened to be correct.
    f, t, basis = mix_window(q, plot, lo, hi)
    explicit_month = bool((q.get("month") or "").strip())
    clamped = False
    if f and t:
        if explicit_month and t >= today:
            t, clamped = today, True      # this month's last days have not happened
        elif not explicit_month and hi and t > hi:
            t, clamped = hi, True         # a default window stops where the data does
        if f > t:
            f = t
    # Rates never divide by a day still in progress.
    rate_t = _rate_end(t, today)
    rate_ok = bool(f and rate_t and rate_t >= f)
    pf = pt = None
    span = 0
    if f and t:
        # The prior window is equal-length to the window the RATES use, so the two
        # sides describe the same number of days even when today is dropped.
        pf, pt, span = prior_window(f, rate_t if rate_ok else t)

    cur_src = _window_src(cur, q, f, rate_t) if rate_ok else {}
    prior_src = _window_src(cur, q, pf, pt) if (pf and pt) else {}
    curw_all, cur_all_n, cur_all_np = _merge_src(cur_src)
    priorw_all, prior_all_n, prior_all_np = _merge_src(prior_src)

    # ---- like-for-like feeds (F1) ----------------------------------------
    # A feed counts as comparable only when it has records in BOTH windows and
    # neither side stops short of its window end. Everything else is named in the
    # label and, if it exists at all, withholds the arrows.
    gmax = _source_max(cur)
    dropped, truncated, lfl, reasons = [], [], [], []
    for s in sorted(set(cur_src) | set(prior_src)):
        cn = cur_src.get(s, {}).get("n", 0)
        pn = prior_src.get(s, {}).get("n", 0)
        if not cn or not pn:
            dropped.append(s)
            if pn and not cn:
                end = gmax.get(s)
                reasons.append(f"the {s} feed ends "
                               f"{end.isoformat() if end else 'before this window'}, so this "
                               f"comparison is not like-for-like")
            else:
                reasons.append(f"{cn:,} record{'s' if cn != 1 else ''} labelled {s} appear only "
                               f"in the current window")
            continue
        gap_c = (t - cur_src[s]["mx"]).days if cur_src[s].get("mx") else 99
        gap_p = (pt - prior_src[s]["mx"]).days if prior_src[s].get("mx") else 99
        if gap_c > COVER_SLACK or gap_p > COVER_SLACK:
            truncated.append(s)
            reasons.append(f"the {s} feed stops on "
                           f"{cur_src[s]['mx'].isoformat() if cur_src[s].get('mx') else 'no date'} "
                           f"in this window, {gap_c} day{'s' if gap_c != 1 else ''} before it ends")
            continue
        lfl.append(s)
    # The composition test the review found by hand: the picklist-only (has_prose=0)
    # share of rows rose 29.9% -> 45.3%, so a "change" in any code partly measures
    # more unreadable rows arriving.
    share_c = 100.0 * cur_all_np / cur_all_n if cur_all_n else 0.0
    share_p = 100.0 * prior_all_np / prior_all_n if prior_all_n else 0.0
    if prior_all_n and abs(share_c - share_p) > 10:
        reasons.append(f"the return-reason-only share of records moved from {share_p:.1f}% to "
                       f"{share_c:.1f}% ({share_c - share_p:+.1f}pp)")

    curw, cur_total, _ = _merge_src(cur_src, lfl)
    priorw, prior_total, _ = _merge_src(prior_src, lfl)

    # Denominators. feed_days is the whole feed (zero days count as zeros);
    # sel_days only DESCRIBES coverage, it never divides anything.
    cur_feed = feed_days(cur, f, rate_t) if rate_ok else 0
    prior_feed = feed_days(cur, pf, pt) if (pf and pt) else 0
    cur_sel = sel_days(cur, q, f, rate_t) if rate_ok else 0
    prior_sel = sel_days(cur, q, pf, pt) if (pf and pt) else 0

    # A percentage needs a denominator worth dividing by. Below 100 prior records
    # or half the current side, the prior window is a coverage artefact.
    thin = bool(prior_total) and (prior_total < 100 or prior_total < 0.5 * cur_total)
    # The prior window holds fewer feed days than its calendar length when the
    # corpus starts inside it. The rates are still correct (they divide by the
    # days the feed covers), but the reader must be told the calendar window and
    # the feed window are not the same thing.
    pcal = ((pt - pf).days + 1) if (pf and pt) else 0
    daygap = bool(prior_feed) and bool(pcal) and prior_feed < pcal
    show_delta = (bool(prior_total) and not thin and bool(lfl) and not reasons
                  and bool(cur_feed) and bool(prior_feed) and rate_ok)

    if not n:
        return ('<div class="mix"><div class="mixhead"><h2>Complaint mix</h2></div>'
                '<p class="mixnote">Nothing matches those filters.</p></div>')

    mx = max([k for _, k in bars], default=1) or 1
    top = bars[:12]
    floor = max(20, int(math.ceil(0.01 * cur_total))) if cur_total else 20
    src_label = ", ".join(lfl) if lfl else "no single feed"

    rows = []
    for c, k in top:
        share = k * 100.0 / n
        was = (f'<div class="meta">was {priorw_all.get(c, 0) * 100.0 / prior_all_n:.1f}%</div>'
               if (prior_all_n and rate_ok) else "")
        if show_delta:
            cv, pv = curw.get(c, 0), priorw.get(c, 0)
            if max(cv, pv) < floor:
                ttl = (f"{cv:,} here vs {pv:,} before ({src_label}) — below the {floor:,}-record "
                       f"floor, too small to read as a trend")
                d = f'<span class="meta" title="{esc(ttl)}">n&lt;{floor:,}</span>'
                dv = ""
            else:
                d = _delta_cell(cv, pv, cur_feed, prior_feed,
                                f"{src_label} only: {cv:,} records over {cur_feed:,} feed days "
                                f"({cv / max(cur_feed, 1):.2f}/day) vs {pv:,} over "
                                f"{prior_feed:,} ({pv / max(prior_feed, 1):.2f}/day)")
                p = _pct(cv, pv, cur_feed, prior_feed)
                dv = "" if p is None else f"{p:.4f}"
        elif f and t:
            d = '<span class="dl flat">—</span>'
            dv = ""
        else:
            d, dv = "", ""
        win = (f'<div class="meta">{curw_all.get(c, 0):,} in window</div>' if f and t else "")
        nocom = (f'<div class="meta">{nocomment.get(c, 0):,} no comment</div>'
                 if nocomment.get(c, 0) else "")
        rows.append(
            f'<tr data-code="{esc(c)}" data-n="{k}" data-win="{curw_all.get(c, 0)}" data-delta="{dv}">'
            f'<td class="c" title="{esc(c)}">{esc(display_label(c, cur))}</td>'
            f'<td class="sh">{share:.1f}%{was}</td>'
            f'<td class="n">{k:,}{win}{nocom}</td>'
            f'<td style="width:30%"><div class="mixbar" style="width:{max(2, int(k * 100.0 / mx))}%"></div></td>'
            f'<td style="width:92px">{d}</td></tr>')
    rest = bars[12:]
    if rest:
        rn = sum(k for _, k in rest)
        rows.append(f'<tr><td class="c" style="color:var(--mut)">{len(rest)} other complaints</td>'
                    f'<td class="sh">{rn * 100.0 / n:.1f}%</td><td class="n">{rn:,}</td>'
                    f'<td><div class="mixbar" style="opacity:.35;width:{max(2, int(rn * 100.0 / mx))}%"></div></td><td></td></tr>')
    if blank:
        rows.append(f'<tr><td class="c" style="color:var(--mut)">{esc(NO_CODE)}</td>'
                    f'<td class="sh">{blank * 100.0 / n:.1f}%</td><td class="n">{blank:,}</td>'
                    f'<td><div class="mixbar blank" style="width:{max(2, int(blank * 100.0 / mx))}%"></div></td><td></td></tr>')
    if noise:
        rows.append(f'<tr><td class="c" style="color:var(--mut)">{esc(PROCESS_NOISE)} '
                    f'<span style="font-weight:400">· process noise, excluded</span></td>'
                    f'<td class="sh">{noise * 100.0 / n:.1f}%</td><td class="n">{noise:,}</td>'
                    f'<td><div class="mixbar noise" style="width:{max(2, int(noise * 100.0 / mx))}%"></div></td><td></td></tr>')

    # F5: share of the mix is the headline. It is the one comparison that needs no
    # units/orders denominator, and the only one the feed change cannot fake.
    lead = ""
    if bars:
        parts = []
        for c, k in bars[:3]:
            s = k * 100.0 / n
            ps = (f' <span style="color:#9ca3af">(was {priorw_all.get(c, 0) * 100.0 / prior_all_n:.1f}%)</span>'
                  if (prior_all_n and rate_ok) else "")
            parts.append(f'<b>{esc(display_label(c, cur))}</b> {s:.1f}%{ps}')
        lead = ('<p class="mixnote"><b>Share of complaint records — the headline:</b> '
                + " · ".join(parts)
                + f'. Shares add to 100% of the {n:,} matching records and need no units '
                  f'denominator, so they stay comparable across the two windows even where the '
                  f'record counts do not.</p>')

    # What grew, ranked by RECORD COUNT and reported as the same per-day rate the
    # column shows. F13: a count that rose while its rate did not is 'flat per
    # day', never '+137 (-0%)'.
    riser = ""
    if show_delta:
        movers = sorted(((curw.get(c, 0) - priorw.get(c, 0), c) for c in set(curw) | set(priorw)
                         if c not in (NO_CODE, PROCESS_NOISE)), reverse=True)
        up = [(d, c) for d, c in movers if d >= floor][:3]
        if up:
            parts = []
            for delta, c in up:
                pv = priorw.get(c, 0)
                tail = _riser_tail(delta + pv, pv, cur_feed, prior_feed)
                parts.append(f'<b>{esc(display_label(c, cur))}</b> +{delta:,} records ({tail})')
            riser = (f' Biggest increase{"s" if len(up) != 1 else ""} by record count on '
                     f'{esc(src_label)} against {prior_feed:,} feed days before: '
                     + " · ".join(parts) + ".")
        else:
            riser = (f' No complaint grew by {floor:,}+ records on {esc(src_label)} against the '
                     f'{prior_feed:,} feed days before, so nothing here is named as a rise.')

    caveat = ""
    if f and t and not rate_ok:
        caveat = (' No complete day falls inside this window (it starts today or later), so there '
                  'is no per-day rate to compute — today holds a fraction of its records.')
    elif reasons:
        caveat = (' <b>Change withheld — this comparison is not like-for-like:</b> '
                  + "; ".join(reasons)
                  + '. The counts and shares above are unaffected; only the arrows need the same '
                    'feeds on both sides, because a feed that starts or stops inside a window '
                    'changes the total without any customer changing their behaviour.')
    elif thin:
        caveat = (f' The preceding period ({pf.isoformat()} to {pt.isoformat()}) holds only '
                  f'{prior_total:,} records on {src_label} against {cur_total:,} here, so no '
                  f'percentage is shown: the change would be coverage, not trend.')
    elif daygap:
        caveat = (f' The preceding window is {pcal:,} calendar days but the feed covers only '
                  f'{prior_feed:,} of them (it starts inside that window), so the rate divides by '
                  f'the days the feed covers and a covered day with no record counts as a zero.')

    when = ""
    if f and t:
        if prior_all_n and rate_ok:
            when = (f' · comparing <b>{f.isoformat()}</b> to <b>{rate_t.isoformat()}</b> against '
                    f'<b>{pf.isoformat()}</b> to <b>{pt.isoformat()}</b>')
            if show_delta:
                when += (f' · <b>{esc(src_label)}</b> only ({cur_total:,} records here vs '
                         f'{prior_total:,} before)')
            elif lfl:
                when += f' · {esc(src_label)} only, arrows withheld'
            else:
                when += ' · no feed covers both windows'
            excl = [s for s in dropped + truncated if prior_src.get(s, {}).get("n")]
            if excl:
                bits = []
                for s in excl:
                    bits.append(f'{s} {prior_src[s]["n"]:,} before, '
                                f'{cur_src.get(s, {}).get("n", 0):,} here')
                when += ' · excluded: ' + ", ".join(bits)
        elif prior_all_n:
            # The window holds no complete day (today, or the future): there is no
            # rate to compare, so name the period and say why no arrows appear.
            when = (f' · period {f.isoformat()} to {t.isoformat()} — no complete day to '
                    f'compare against')
        else:
            when = f' · period {f.isoformat()} to {t.isoformat()}'
    elif basis:
        when = f' · period: {esc(basis)}'
    if hi and (clamped or (corpus_hi and hi < corpus_hi)):
        when += f' · data to {hi.isoformat()}'
        if corpus_hi and hi < corpus_hi:
            when += f' (whole corpus to {corpus_hi.isoformat()})'
    if t and rate_t and rate_t < t:
        when += f' · today ({t.isoformat()}) is incomplete and is not counted in the rates'

    n_pr = n - n_no
    disc = (f'<b>{n_pr:,}</b> of <b>{n:,}</b> records carry a customer comment; the other '
            f'{n - n_pr:,} hold the return-reason picklist only, so no label on them can be checked '
            f'against what a customer wrote')
    val = (f' <b>{n_hv:,}</b> of <b>{n:,}</b> records have a decision recorded in this app so far'
           f' — the labels were validated before launch by the CX team (553,055 human-labelled'
           f' review×complaint pairs) and by Chadia\u2019s blind 220-row check of this AI (83.6% agreement)')

    # Each toggle REPLACES its own dimension and KEEPS the others, so with
    # status=flagged "all rows" means flagged-and-no-text (137), not 1,571. That
    # makes the five counts five different WHERE clauses; one scan cannot produce
    # them (tried it: it silently widened every toggle to the un-statused family).
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
    dup_over = {"dup": "" if deduped else "1"}
    togs = (f'{_tog("all rows", {"tx": ""}, not q.get("tx"))} · '
            f'{_tog("with a comment", {"tx": "prose"}, q.get("tx") == "prose")} · '
            f'{_tog("picklist only", {"tx": "codeonly"}, q.get("tx") == "codeonly")} · '
            f'{_tog("human-validated only", {"status": "decided"}, q.get("status") == "decided")} · '
            f'{_tog("count every row" if deduped else "distinct complaints", dup_over, False)}')

    # F2: the repeat key is (order_id, MD5(text)) — the SAME comment recorded twice
    # on the SAME order. Keying on text+SKU+date merged different customers who
    # happened to type the same short phrase on the same day (887 of 892 old groups
    # spanned two or more orders; 'Did not match the sizing required' was 7 rows
    # across 106 orders) and the footer called that a repeated review.
    if deduped:
        qd = {k: v for k, v in q.items() if k != "dup"}
        wd, ad = build_where(qd)
        cur.execute(f"SELECT COUNT(*) n FROM v_ai_complaints v {wd}", ad)
        hidden = max(0, int(cur.fetchone()["n"] or 0) - n)
        dupnote = (f' <b>{hidden:,}</b> rows that repeat a comment already recorded on the same '
                   f'order are hidden by this view — one row per comment per order, applied on '
                   f'request and named here')
    elif n_dup:
        dupnote = (f' <b>{n_dup:,}</b> rows repeat a comment already recorded on the same order; '
                   f'they are counted, not removed — '
                   f'<a href="/reviews?{esc(qstr(q, dup="1", page=""))}">show {n - n_dup:,} distinct '
                   f'complaints</a>.')
    else:
        dupnote = ' No row repeats a comment already recorded on the same order.'

    part = None
    if plot and hi and plot[-1][0] == f"{hi.year:04d}-{hi.month:02d}" and hi.day < _month_days(plot[-1][0]):
        part = hi.day
    partnote = ""
    if part and len(plot) > 1:
        pm, pn = plot[-2]
        pd_ = mdays.get(pm) or 1
        cd_ = mdays.get(plot[-1][0]) or part
        partnote = (f' {MN[int(plot[-1][0][5:7]) - 1]} is drawn dashed because this selection\'s data '
                    f'stops on the {_ord(part)}: {plot[-1][1]:,} records on {cd_} day'
                    f'{"s" if cd_ != 1 else ""} with data is {plot[-1][1] / cd_:.1f}/day against '
                    f'{pn / pd_:.1f}/day in {MN[int(pm[5:7]) - 1]} — a short month, not a fall.')

    # F3: the denominators, in the header where a reader can check them.
    cov = ""
    if cur_feed:
        cov = (f'<div class="cap" data-sel="{cur_sel}" data-feed="{cur_feed}">'
               f'{cur_sel:,} of {cur_feed:,} feed days in this window carry a matching record '
               f'(a feed day with no record is a zero, not a gap)</div>')

    hdr = ('complaint · share of matching records · records'
           + (f' · change in complaint records per day, {esc(src_label)} only'
              if show_delta else '')
           + (' · change withheld: the two windows are not like-for-like'
              if (reasons and f and t) else '')
           + (' · no earlier records here, so there is nothing to compare'
              if (f and t and not prior_all_n) else ''))

    # R10: the rate as a second series, and the feed-mix breaks that make a
    # month incomparable with the one before it. A count can rise while its rate
    # falls; without this line the panel cannot answer "is it up?" without
    # re-filtering month by month.
    cur.execute(f"""SELECT DATE_FORMAT(v.review_date,'%%Y-%%m') m, IFNULL(v.source,'(none)') s
                      FROM v_ai_complaints v {ws} GROUP BY m, s""", args + [PROCESS_NOISE])
    src_sets = {}
    for r in cur.fetchall():
        if r["m"]:
            src_sets.setdefault(r["m"], set()).add(r["s"])
    od = month_orders(cur, q, [m for m, _ in plot])
    rate_pts, breaks, prev = {}, [], None
    for i, (m, k) in enumerate(plot):
        st = tuple(sorted(src_sets.get(m, ())))
        if prev is not None and st != prev:
            breaks.append(i)
        prev = st
        o = int((od.get(m) or {}).get("orders") or 0)
        rate_pts[m] = (100.0 * k / o) if o else None

    exp_line = exposure_line(cur, q, months=[m for m, _ in plot])
    src_tbl = source_rate_table(cur, q, months=[m for m, _ in plot])

    return f"""<div class="mix" data-n="{n}" data-cur-feed="{cur_feed}" data-prior-feed="{prior_feed}" data-cur-sel="{cur_sel}" data-prior-sel="{prior_sel}" data-lfl="{esc(",".join(lfl))}" data-guard="{1 if reasons else 0}" data-rate-breaks="{len(breaks)}">
  <div class="mixhead"><h2>Complaint mix</h2>
    <span class="r">{n:,} complaint records match these filters{when}</span></div>
  {lead}
  <div class="txtog">Show: {togs}</div>
  <div class="mixgrid">
    <div>
      <div style="font-size:11.5px;color:var(--mut);margin-bottom:5px">{hdr}</div>
      <table class="mixtbl"><tbody>{''.join(rows) or
        '<tr><td class="mixnote">No complaint is labelled in this selection.</td></tr>'}</tbody></table>
    </div>
    <div class="mixpanel">
      <div style="font-size:12.5px;font-weight:650;margin-bottom:2px">Complaint records per month, and per 100 orders</div>
      {_mix_svg(plot, part, rate=rate_pts, breaks=breaks) or '<div class="mixnote">No dated rows in this selection.</div>'}
      <div class="cap">{len(plot)} month{'s' if len(plot) != 1 else ''} with data{' · ' + plot[0][0] + ' to ' + plot[-1][0] if plot else ''}{f' · data to {hi.isoformat()}' if hi else ''}{f' · {omitted:,} older records outside this range are not drawn' if omitted else ''}{partnote}{cov}</div>
      <div class="cap">— <span style="color:#2b5cff">records</span> · – – <span style="color:#b45309">per 100 orders, all sources</span>{' · shaded month = the set of feeds contributing records changed' if breaks else ''}. The rate divides this selection's records by the scope's orders for the whole month, so a shaded month is not comparable with the one before it — <b>for a quotable trend use the fba row of the per-feed table below</b>, the only feed present every month.</div>
    </div>
  </div>
  <p class="mixnote"><b>{n:,}</b> records match · <b>{bars_total:,}</b> are complaints · {blank:,} carry no
  complaint · {noise:,} are process noise (Review Deletion) shown for reconciliation only and kept out of the
  distribution.{riser}{caveat}</p>
  <p class="mixnote">{disc}.{val}.{dupnote}</p>
  {exp_line}
  {src_tbl}
  <p class="mixnote">A rise in this panel is a rise in <b>complaint records</b>: a launch, a promotion
  and a quality problem all move it. Whether it moved faster than the orders did is the dashed rate line
  above and the <a href="/monthly?{esc(qstr(q))}">Monthly</a> page
  (<code>/monthly</code> and <code>/monthly_sku.csv</code>), where the same records sit beside their
  denominator — not the record counts.</p>
</div>"""


# ---------------------------------------------------------------- dashboard
# The overview, not the workspace: four numbers that are doors, the mix, the
# trend, one rate strip, and a link to the records. Everything that used to be
# prose on this page (reconciliation, feed coverage, what the rate is, the
# validation wording) is in the one collapsed disclosure at the bottom.
#
# Presentation only: every query below is the one the page already used, or the
# one /reviews uses for the same figure, so no number can move.

DASH_CSS = """
.dash h1{font-size:26px;letter-spacing:-.025em;margin:0}
.dash .dsub{color:#6b7280;font-size:12.5px;margin:5px 0 0}
.dhead{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}
.dacts{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.dbtn{display:inline-block;font-size:12.5px;font-weight:600;padding:6px 12px;border:1px solid #e3e6eb;
  border-radius:7px;background:#fff;color:#374151;white-space:nowrap}
.dbtn:hover{border-color:#cfd6e0;text-decoration:none}
.ro{font-size:10.5px;color:#9ca3af;font-weight:650;text-transform:uppercase;letter-spacing:.06em}
.dfilters{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap;margin:20px 0 8px}
.df{display:flex;flex-direction:column;gap:5px;min-width:0}
.df label{font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#9ca3af}
.df select,.df input{font:inherit;font-size:13px;padding:6px 9px;border:1px solid #dfe3e9;border-radius:7px;
  background:#fff;color:var(--ink);min-width:0;max-width:100%}
.df select{min-width:172px}
.df input{width:172px}
.df.grow{flex:1 1 180px}
.df.grow input{width:100%}
.df select:focus,.df input:focus{outline:2px solid #dbeafe;border-color:#bfd4f7}
.dapply{padding:6px 14px;font-size:12.5px;border-radius:7px}
.dclear{font-size:12px;color:#6b7280;padding-bottom:8px}
.dcards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:16px;margin:20px 0 28px}
.dcard{display:block;background:var(--card);border:1px solid #e9ebef;border-radius:11px;padding:16px 18px;color:inherit}
.dcard:hover{border-color:#c7d6f2;text-decoration:none}
.dcard .n{font-size:25px;font-weight:680;letter-spacing:-.02em;line-height:1.1;font-variant-numeric:tabular-nums}
.dcard .l{font-size:14px;color:#374151;margin-top:6px;font-weight:600}
.dcard .s{font-size:12px;color:#9ca3af;margin-top:2px}
.duo{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:24px;align-items:start;margin-bottom:24px}
.dpanel{background:var(--card);border:1px solid #e9ebef;border-radius:11px;padding:18px 20px;min-width:0}
.dphead{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.dphead h2{font-size:20px;margin:0;font-weight:680;letter-spacing:-.02em}
.dpcount{font-size:12px;color:#6b7280;font-variant-numeric:tabular-nums;white-space:nowrap}
.drow{display:grid;grid-template-columns:minmax(0,1fr) 58px minmax(40px,34%);align-items:center;gap:12px;
  padding:7px 0;font:inherit;font-size:14px;border:0;background:none;width:100%;text-align:left;color:inherit}
.dname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dnum{text-align:right;font-variant-numeric:tabular-nums;color:#374151}
.dbar{height:8px;background:#f2f4f7;border-radius:4px;overflow:hidden}
.dbar i{display:block;height:100%;background:#7ba3e8;border-radius:4px}
.drow.muted .dname{color:#6b7280}
.drow.muted .dbar i{background:#cdd3dc}
.drow.more{cursor:pointer;border-radius:8px}
.drow.more:hover{background:#fafbfd}
.drow.more .dname{color:#2563eb;font-weight:600}
.darrow{font-size:10px;color:#9ca3af;margin-left:5px}
.dall{margin-top:4px;border-top:1px solid #e9ebef;padding-top:6px}
.dall[hidden]{display:none}
.dtrend svg{display:block;width:100%;height:auto}
.dlegend{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:12px;font-size:12px;color:#4b5563}
.dlegend span{display:inline-flex;align-items:center;gap:6px}
.dlegend .sw{width:10px;height:10px;border-radius:2px;background:#7ba3e8}
.dlegend .swl{width:18px;height:0;border-top:2px dashed #111827;opacity:.6}
.dcap{font-size:12px;color:#9ca3af;margin-top:10px;line-height:1.5}
.dstrip{display:flex;flex-wrap:wrap;align-items:center;gap:4px 9px;background:#fff;border:1px solid #e9ebef;
  border-radius:11px;padding:12px 18px;margin:0 0 14px;font-size:14px;color:#374151}
.dstrip b{font-weight:680;font-variant-numeric:tabular-nums;color:var(--ink)}
.dstrip .sep{color:#d5d9e0}
.dstrip .end{margin-left:auto;color:#6b7280;font-size:12px;display:inline-flex;gap:9px;flex-wrap:wrap}
.dopen{display:inline-block;font-size:13px;margin:0 0 28px}
.dabout{background:#fff;border:1px solid #e9ebef;border-radius:11px;margin-bottom:10px}
.dabout summary{cursor:pointer;font-size:14px;font-weight:650;padding:14px 20px;display:flex;
  justify-content:space-between;align-items:center;gap:12px;list-style:none}
.dabout summary::-webkit-details-marker{display:none}
.dabout summary span{color:#9ca3af;font-size:12px}
.dabout .dbody{padding:2px 20px 18px;border-top:1px solid #f1f2f5;max-width:940px}
.dabout p{margin:12px 0 0;font-size:12.5px;color:#4b5563;line-height:1.6}
.dabout b{color:#111827}
.dabout code{font-size:11.5px}
@media(max-width:1240px){.dcards{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:960px){.dcards{grid-template-columns:repeat(2,minmax(0,1fr))}.duo{grid-template-columns:1fr}}
@media(max-width:560px){
  .dcards{grid-template-columns:1fr}
  .df{flex:1 1 140px}
  .df select{min-width:0;width:100%}
  .df input{width:100%}
  .dstrip{font-size:13px}
}
"""

DASH_MIX_JS = """<script>
(function(){
  /* An empty filter is not a filter: disable it on submit so the URL carries only
     what was actually chosen. */
  var f=document.querySelector('.dfilters');
  if(f) f.addEventListener('submit',function(){
    [].forEach.call(f.elements,function(el){
      if(el.name && !String(el.value).trim()) el.disabled=true;
    });
  });
  var b=document.getElementById('mixmore'),a=document.getElementById('mixall');
  if(!b||!a) return;
  b.addEventListener('click',function(){
    var open=a.hasAttribute('hidden');
    if(open){a.removeAttribute('hidden')}else{a.setAttribute('hidden','')}
    b.setAttribute('aria-expanded',open?'true':'false');
    var t=b.querySelector('.darrow'); if(t) t.textContent=open?'\\u25be':'\\u25b8';
  });
})();
</script>"""


def _dash_day(iso):
    """2026-08-29 -> '29 Aug 2026'. The caption names a feed's last day."""
    if not iso:
        return ""
    d = iso if isinstance(iso, str) else iso.isoformat()
    try:
        return f"{int(d[8:10])} {MN[int(d[5:7]) - 1]} {d[:4]}"
    except (ValueError, IndexError):
        return d


def _dash_month(m):
    try:
        return f"{MN[int(m[5:7]) - 1]} {m[:4]}"
    except (ValueError, IndexError):
        return m


def _dash_coverage(months, by_src, src_max, omitted):
    """One line naming the feeds whose coverage stops inside the range."""
    if len(months) < 2:
        base = "One month shown."
    else:
        last = months[-1]
        parts = []
        for s in sorted(by_src):
            have = [m for m in months if by_src[s].get(m)]
            if have and have[-1] != last:
                d = src_max.get(s)
                parts.append(f"{s} ends {_dash_day(d)}" if d else
                             f"{s} stops after {_dash_month(have[-1])}")
        base = ("Coverage changes: " + "; ".join(parts) + "." if parts
                else "Every source reports in every month shown.")
    if omitted:
        base += f" · {omitted:,} older records outside this range are not drawn"
    return base


def _dash_bars_svg(points, rates):
    """Month bars (left axis) and a dashed per-100-orders line (right axis).

    Server-side SVG, no JS: the dashboard must read with scripting off. A month
    with no denominator has no point on the line (never a zero) — the polyline
    breaks there, exactly as /reviews draws it.
    """
    if not points:
        return ""
    W, H, L, R, T, B = 520, 196, 44, 46, 14, 26
    iw, ih = W - L - R, H - T - B
    mx = max(n for _, n in points) or 1
    live = [rates.get(m) for m, _ in points if rates.get(m) is not None]
    rmax = max(live) if live else 0
    rtop = rmax * 1.2 if rmax > 0 else 1
    step = iw / len(points)
    bw = min(44, max(6, step * 0.52))
    g = []
    for k in range(5):
        y = T + ih * (1 - k / 4)
        g.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" stroke="#eef0f3"/>')
        g.append(f'<text x="{L - 6}" y="{y + 3:.1f}" text-anchor="end" font-size="9.5" '
                 f'fill="#9ca3af">{round(mx * k / 4):,}</text>')
        if rmax > 0:
            g.append(f'<text x="{W - R + 6}" y="{y + 3:.1f}" font-size="9.5" '
                     f'fill="#9ca3af">{rtop * k / 4:.1f}</text>')
    skip = 2 if len(points) > 8 else 1
    for i, (m, n) in enumerate(points):
        h = max(1, (n / mx) * ih)
        x = L + i * step + (step - bw) / 2
        y = T + ih - h
        rate = rates.get(m)
        title = f"{_dash_month(m)}: {n:,} complaint records"
        if rate is not None:
            title += f" · {rate:.2f} per 100 orders"
        g.append(f'<rect data-month="{m}" data-n="{n}" x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                 f'height="{h:.1f}" rx="2" fill="#7ba3e8"><title>{esc(title)}</title></rect>')
        if i % skip == 0:
            g.append(f'<text x="{x + bw / 2:.1f}" y="{H - 8}" text-anchor="middle" '
                     f'font-size="9.5" fill="#9ca3af">{MN[int(m[5:7]) - 1]}</text>')
    seg, segs = [], []
    for i, (m, _) in enumerate(points):
        v = rates.get(m)
        x = L + i * step + step / 2
        if v is None:
            if len(seg) > 1:
                segs.append(seg)
            seg = []
            continue
        seg.append((x, T + ih - ih * (v / rtop)))
    if len(seg) > 1:
        segs.append(seg)
    for s in segs:
        g.append('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in s) +
                 '" fill="none" stroke="#111827" stroke-width="1.6" stroke-dasharray="4 3" '
                 'opacity=".55"/>')
    for i, (m, _) in enumerate(points):
        v = rates.get(m)
        if v is None:
            continue
        g.append(f'<circle cx="{L + i * step + step / 2:.1f}" cy="{T + ih - ih * (v / rtop):.1f}" '
                 f'r="2.4" fill="#111827" opacity=".6"/>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" '
            f'aria-label="Complaint records and records per 100 orders by month" '
            f'data-months="{len(points)}">' + "".join(g) + "</svg>")


def p_dashboard(cur, q, msg="", ctx=None):
    # ---- filters --------------------------------------------------------
    # One predicate for the cards, the mix, the chart and the strip: build_where,
    # exactly as /reviews applies it. Range is the static page's rule — the last
    # N months present in the corpus, inclusive — expressed as a from-date.
    fq = {k: q.get(k) for k in ("q", "family", "month", "source") if q.get(k)}
    win = (q.get("range") or "").strip()
    if win in ("3", "6", "12"):
        cur.execute("SELECT MAX(month) m FROM v_ai_complaints_monthly")
        mxm = (cur.fetchone() or {}).get("m")
        if mxm:
            idx = int(mxm[:4]) * 12 + int(mxm[5:7]) - 1 - (int(win) - 1)
            fq["from"] = f"{idx // 12:04d}-{idx % 12 + 1:02d}-01"
    w, args = build_where(fq)
    w_and = (w + " AND ") if w else "WHERE "

    # ---- the four numbers ----------------------------------------------
    cur.execute(f"""SELECT COUNT(*) c, COALESCE(SUM(v.has_prose=0),0) np,
                           COALESCE(SUM(v.human_validated=1),0) hv,
                           MIN(v.review_date) lo, MAX(v.review_date) hi
                      FROM v_ai_complaints v {w}""", args)
    h = cur.fetchone()
    total, no_prose, validated = int(h["c"]), int(h["np"]), int(h["hv"])
    lo, hi = h["lo"], h["hi"]
    cur.execute(f"""SELECT COALESCE(SUM(v.has_prose=0),0) np
                      FROM v_ai_complaints v {w_and}v.needs_review=1""", args)
    nr_co = int(cur.fetchone()["np"])
    # The one genuine backlog: rows with NO label at all. It is a different
    # question from needs_review (the confidence signal), and conflating the two
    # is what put 2,155 already-labelled rows in front of a person as work.
    cur.execute(f"""SELECT COALESCE(SUM(v.final_complaint IS NULL),0) c
                      FROM v_ai_complaints v {w}""", args)
    unlab = int(cur.fetchone()["c"])
    cur.execute("SELECT COUNT(*) c FROM proposed_codes WHERE status='pending'")
    pend = int(cur.fetchone()["c"])
    # DISTINCT review_id, not COUNT(*): a revised decision adds a row, and
    # counting rows would inflate "decisions recorded" every time she changes her mind.
    cur.execute("SELECT COUNT(DISTINCT review_id) c FROM corrections")
    corr = int(cur.fetchone()["c"])
    # ---- provenance ------------------------------------------------------
    # Same predicate as the cards, so the fifth card and the disclosure cannot
    # disagree with the rows. 'rejected' is counted on its own: those records
    # carry a person's decision but report no label, so they are neither.
    cur.execute(f"""SELECT COALESCE(SUM(v.human_decision IN ('accepted','corrected')),0) h,
                           COALESCE(SUM(v.human_decision='rejected'),0) x
                      FROM v_ai_complaints v {w}""", args)
    _pv = cur.fetchone()
    human_n, excl_n = int(_pv["h"]), int(_pv["x"])
    ai_n = total - human_n - excl_n
    human_pct = (100.0 * human_n / total) if total else 0.0
    # One decimal is right for a real share and useless for a single row: 1 of
    # 22,228 is 0.0045%, which must not print as '0.0%' beside a count of 1.
    human_pct_txt = ("0.0%" if not human_n else
                     (f"{human_pct:.2f}%" if human_pct < 1 else f"{human_pct:.1f}%"))

    # ---- what people complained about ----------------------------------
    cur.execute(f"""SELECT v.final_complaint c, COUNT(*) n FROM v_ai_complaints v {w}
                     GROUP BY v.final_complaint ORDER BY n DESC""", args)
    dist = cur.fetchall()
    blank = noise = 0
    bars = []
    for r in dist:
        c, k = r["c"], int(r["n"])
        if c is None:
            blank = k
        elif c == PROCESS_NOISE:
            noise = k
        else:
            bars.append((c, k))
    bars.sort(key=lambda x: (-x[1], x[0]))
    bars_total = sum(k for _, k in bars)
    unassigned = blank + noise
    top, rest = bars[:6], bars[6:]
    rest_n = sum(k for _, k in rest)

    # ---- over time ------------------------------------------------------
    # Counts: resolved complaint, not Review Deletion, dated — the same series
    # /reviews plots. Trimmed to the run holding 99.5%, as /reviews does, so the
    # two charts show the same months.
    ws = w_and + ("v.review_date IS NOT NULL AND v.final_complaint IS NOT NULL "
                  "AND v.final_complaint <> %s")
    cur.execute(f"""SELECT COALESCE(v.source,'') s, DATE_FORMAT(v.review_date,'%%Y-%%m') m,
                           COUNT(*) n
                      FROM v_ai_complaints v {ws}
                     GROUP BY s, m ORDER BY m""", args + [PROCESS_NOISE])
    by_src, mcount = {}, {}
    for r in cur.fetchall():
        if not r["m"]:
            continue
        by_src.setdefault(r["s"], {})[r["m"]] = int(r["n"])
        mcount[r["m"]] = mcount.get(r["m"], 0) + int(r["n"])
    months_all = sorted(mcount.items())
    kept, omitted = _trim_months(months_all)
    months = [m for m, _ in kept]

    cur.execute("SELECT source s, MAX(review_date) d FROM v_ai_complaints GROUP BY source")
    src_max = {r["s"] or "": r["d"] for r in cur.fetchall()}

    # The rate's numerator and denominator, per month: records in SKU-months the
    # finance feed covers (a month that sold 0 orders has no denominator), over
    # the product scope's orders. Exactly /reviews' exposure(), at month grain.
    ords = month_orders(cur, fq, months) if months else {}
    rates, cov_by_month, outside = {}, {}, 0
    if months:
        cur.execute(f"""SELECT DATE_FORMAT(v.review_date,'%%Y-%%m') m, COUNT(*) n,
                               SUM(s.orders IS NULL) nosales, SUM(s.orders = 0) zero
                          FROM v_ai_complaints v
                          LEFT JOIN sales_by_sku_month s
                                 ON s.sku = v.sku
                                AND s.month = DATE_FORMAT(v.review_date,'%%Y-%%m')
                         {w_and}v.sku IS NOT NULL AND v.review_date IS NOT NULL
                           AND v.final_complaint IS NOT NULL AND v.final_complaint <> %s
                           AND (v.final_category IS NULL
                                OR v.final_category NOT IN ('NEW','NO_TEXT','ERR'))
                         GROUP BY m""", args + [PROCESS_NOISE])
        rate_rows = {r["m"]: r for r in cur.fetchall()}
        for m, _ in months_all:
            if m not in months:
                r = rate_rows.get(m)
                outside += int(r["n"]) if r else 0
        for m in months:
            r = rate_rows.get(m)
            cov = int(r["n"]) - int(r["nosales"] or 0) - int(r["zero"] or 0) if r else 0
            o = int((ords.get(m) or {}).get("orders") or 0)
            cov_by_month[m] = cov
            rates[m] = (100.0 * cov / o) if o > 0 else None
    orders = sum(int((ords.get(m) or {}).get("orders") or 0) for m in months)
    units = sum(int((ords.get(m) or {}).get("units") or 0) for m in months)
    recs_cov = sum(cov_by_month.values())
    rate = (100.0 * recs_cov / orders) if (orders >= RATE_FLOOR and recs_cov) else None

    # ---- header ---------------------------------------------------------
    if lo and hi:
        span_txt = (f"{_dash_day(lo)} – {_dash_day(hi)}" if str(lo) != str(hi)
                    else _dash_day(lo))
    else:
        span_txt = "no records match these filters"

    families = options(cur, "SELECT product_family k, COUNT(*) n FROM v_ai_complaints "
                            "WHERE product_family IS NOT NULL GROUP BY product_family "
                            "ORDER BY k")
    sources = options(cur, "SELECT source k, COUNT(*) n FROM v_ai_complaints "
                           "WHERE source IS NOT NULL GROUP BY source ORDER BY k")

    def _sel(name, vals, cur_val, any_label):
        opts = [f'<option value="">{any_label}</option>']
        opts += [f'<option value="{esc(k)}"{" selected" if cur_val == k else ""}>{esc(k)}</option>'
                 for k, _ in vals]
        return f'<select name="{name}" id="{name}sel">' + "".join(opts) + "</select>"

    ranges = [("", "all"), ("3", "last 3 months"), ("6", "last 6 months"),
              ("12", "last 12 months")]
    range_sel = ('<select name="range">' + "".join(
        f'<option value="{v}"{" selected" if win == v else ""}>{lbl}</option>'
        for v, lbl in ranges) + "</select>")

    # ---- cards ----------------------------------------------------------
    rec_qs = esc(qstr(fq))
    rec_href = f"/reviews?{rec_qs}" if rec_qs else "/reviews"
    cards = [
        (total, "complaint records", "all sources and families", rec_href),
        # Unclassified, in the position the review queue used to hold: a row only
        # waits for a person when the AI produced no label for it.
        (unlab, "unclassified", "no label — the AI could not read a complaint",
         f"/reviews?{esc(qstr(fq, status='nolabel'))}"),
        (pend, "codes awaiting a decision", "not in the complaint list yet", "/codes"),
        (corr, "decisions recorded", "confirmed, corrected or removed",
         f"/reviews?{esc(qstr(fq, status='decided'))}"),
        # Appended, never inserted: the first four keep their order and their
        # numbers. This one answers a different question — whose label is being
        # reported — with the share it holds of the rows on screen.
        (human_n, "Human classified", f"{human_pct_txt} of all records",
         f"/reviews?{esc(qstr(fq, status='human'))}"),
    ]
    cards_html = "".join(
        f'<a class="dcard" href="{href}"><div class="n">{n:,}</div>'
        f'<div class="l">{lbl}</div><div class="s">{sub}</div></a>'
        for n, lbl, sub, href in cards)

    # ---- mix -------------------------------------------------------------
    mx = bars[0][1] if bars else 1

    def _mrow(c, k, cls="", title=""):
        t = f' title="{esc(title)}"' if title else ""
        return (f'<div class="drow {cls}" data-code="{esc(c)}" data-n="{k}"{t}>'
                f'<span class="dname">{esc(display_label(c, cur) if c != NO_CODE else "not assigned")}</span>'
                f'<span class="dnum">{k:,}</span>'
                f'<span class="dbar"><i style="width:{max(2, round(k * 100 / mx))}%"></i></span></div>')

    mix_rows = "".join(_mrow(c, k) for c, k in top)
    if rest:
        mix_rows += (f'<button class="drow more" id="mixmore" aria-expanded="false" '
                     f'data-codes="{len(rest)}"><span class="dname">everything else '
                     f'({len(rest)} code{"s" if len(rest) != 1 else ""})'
                     f'<span class="darrow">▸</span></span><span class="dnum">{rest_n:,}</span>'
                     f'<span class="dbar"><i style="width:{max(2, round(rest_n * 100 / mx))}%">'
                     f'</i></span></button>')
    if unassigned:
        mix_rows += (f'<div class="drow muted" data-code="{esc(NO_CODE)}" data-n="{unassigned}" '
                     f'title="{blank:,} with no complaint code, {noise:,} Review Deletion '
                     f'(process noise)"><span class="dname">not assigned</span>'
                     f'<span class="dnum">{unassigned:,}</span>'
                     f'<span class="dbar"><i style="width:{max(2, round(unassigned * 100 / mx))}%">'
                     f'</i></span></div>')
    mix_all = "".join(_mrow(c, k, "muted", c) for c, k in rest)
    if not total:
        mix_rows = '<div class="dcap">Nothing matches those filters.</div>'
        mix_all = ""

    # ---- trend ----------------------------------------------------------
    if kept:
        trend_html = (f'<div class="dtrend">{_dash_bars_svg(kept, rates)}</div>'
                      f'<div class="dlegend"><span><i class="sw"></i>complaint records</span>'
                      f'<span><i class="swl"></i>records per 100 orders</span></div>')
        trend_head = (f"{_dash_month(months[0])} – {_dash_month(months[-1])}"
                      if len(months) > 1 else _dash_month(months[0]))
        cap = _dash_coverage(months, by_src, src_max, omitted)
    else:
        trend_html = '<div class="dcap">No dated complaint records in this selection.</div>'
        trend_head = ""
        cap = ""

    # ---- rate strip ------------------------------------------------------
    sep = '<span class="sep">·</span>'
    bits = [f'<span><b>{orders:,}</b> orders</span>', f'<span><b>{units:,}</b> units</span>']
    if not recs_cov:
        bits.append("<span>no rate — no complaint records</span>")
    elif not orders:
        bits.append("<span>no rate — 0 orders in scope</span>")
    elif orders < RATE_FLOOR:
        bits.append(f"<span>no rate — under {RATE_FLOOR} orders</span>")
    else:
        bits.append(f'<span><b>{rate:.2f}</b> complaints per 100 orders</span>')
    bits.append(f'<span><b>{corr:,}</b> decisions recorded</span>')
    cur.execute("""SELECT COALESCE(source,'') s, COUNT(*) n FROM v_ai_complaints v """
                f"""{w} GROUP BY s""", args)
    src_count = {r["s"]: int(r["n"]) for r in cur.fetchall()}
    end = "".join(f'<span>{s} <b>{src_count.get(s, 0):,}</b></span>'
                  for s in ("fba", "ncx", "amazon"))
    if src_count.get(""):
        end += f'<span>unlabelled <b>{src_count[""]:,}</b></span>'
    strip = sep.join(bits) + f'<span class="end">{end}</span>'

    # ---- the disclosure --------------------------------------------------
    # Everything the old page said inline, moved here verbatim in substance.
    if rate is not None:
        rate_txt = (f"<b>{orders:,}</b> orders and <b>{units:,}</b> units across the months shown — "
                    f"<b>{rate:.2f}</b> complaint records per 100 orders. Orders are the seller's own "
                    f"finance feed for the scope, all marketplaces; complaints are review records, "
                    f"so this is a record rate, not a return rate.")
    elif not recs_cov:
        rate_txt = "No complaint records in this filter, so there is no rate to quote."
    elif not orders:
        rate_txt = ("The product scope sold <b>0 orders</b> in these months, so the rate is "
                    "undefined — it is not 0.00, which would claim no complaints per order.")
    else:
        rate_txt = (f"<b>{orders:,}</b> orders in scope — under the {RATE_FLOOR}-order floor, so no "
                    f"rate is quoted above. The counts and shares are unaffected.")
    if outside:
        rate_txt += (f" {outside:,} record{'s' if outside != 1 else ''} fall outside "
                     f"{_dash_month(months[0])} to {_dash_month(months[-1])} and are not in "
                     f"this rate.")
    feed_txt = (f"Period {_dash_month(months[0])} to {_dash_day(hi)}. " if months else "")
    feed_txt += ("No month-on-month change is shown: a change figure needs the same feeds on both "
                 "sides, because a feed that starts or stops inside a window moves the total without "
                 "any customer changing their behaviour. The counts and shares are unaffected.")
    if hi and str(hi) >= str(date.today()):
        feed_txt += (f" Today ({_dash_day(str(hi))}) is incomplete and is not counted in the rates.")

    # Only present when something is excluded, so the split stays one line.
    excl_txt = (f" and <b>{excl_n:,}</b> {'is' if excl_n == 1 else 'are'} excluded from reporting"
                if excl_n else "")
    human_word = "record carries" if human_n == 1 else "records carry"

    about = f"""<details class="dabout">
<summary>About this data <span>reconciliation · coverage · what the rate is</span></summary>
<div class="dbody">
<p><b>Reconciliation.</b> <b>{total:,}</b> rows match these filters · <b>{bars_total:,}</b> carry a
real complaint · <b>{blank:,}</b> carry no complaint · <b>{noise:,}</b> are process noise (Review
Deletion). Those three add to <b>{bars_total + blank + noise:,}</b>. The mix shows the six largest
complaint types; every other code is under “everything else”, and the records with no complaint are
under “not assigned”. <b>{len(bars):,}</b> distinct complaint code{"" if len(bars) == 1 else "s"} are
in the mix{"; Review Deletion is a further distinct label, counted only as process noise" if noise else ""}.</p>
<p><b>Feed coverage.</b> {feed_txt}</p>
<p><b>Picklist-only records.</b> <b>{no_prose:,}</b> of <b>{total:,}</b> rows carry no customer
comment; they hold the return-reason picklist only, so no label on them can be checked against what
a customer wrote (<b>{nr_co:,}</b> of them carry a low-confidence label).</p>
<p><b>What the rate is.</b> {rate_txt}</p>
<p><b>Validation status.</b> <b>{validated:,}</b> of <b>{total:,}</b> records have a decision
recorded in this app so far — each record counts once, however many times its decision was revised. The labels themselves were validated before launch: the CX team has
553,055 human-labelled review×complaint pairs in the live module, and Chadia’s own blind 220-row
check of this AI put agreement at 83.6%. Figures and the mix use the corrected label where one
exists, and the AI’s own label stays on each row for audit.</p>
<p><b>Label source.</b> <b>{human_n:,}</b> {human_word} a human label, <b>{ai_n:,}</b> carry the
AI’s{excl_txt}. The badge on each row says which of those is being reported; the status tag, where
one appears, marks a row with no label at all — a different question.</p>
<p><b>Process noise.</b> <b>{noise:,}</b> records are Review Deletion — a rule verdict on empty or
non-complaint text, not a complaint. They stay in the records table and the CSV; the mix counts them
only so the totals reconcile.</p>
<p><b>Status labels.</b> The classifier labels every row it can read, and a labelled row carries no
status tag — the label is finished work, not a flag. <b>unclassified</b> — no label at all, so that
is the only row waiting for a person · <b>new code</b> — it used a complaint type that isn't in your
list yet · <b>confirmed / corrected / removed</b> — someone has already decided. Low confidence is
not a queue: those rows are labelled, and the signal stays reachable through the 'AI was unsure'
filter for anyone auditing it.</p>
</div></details>"""

    body = f"""<div class="dash">
<div class="dhead">
  <div><h1>Complaint validation</h1>
    <div class="dsub">{span_txt} · resolved labels: the team’s correction where one exists, the
    AI’s label otherwise.</div></div>
  <div class="dacts">
    <span class="ro" title="this page cannot write to the database">Read-only</span>
    <a class="dbtn" href="/export.csv?{rec_qs}">Export CSV</a>
    <a class="dbtn" href="/export.csv?{esc(qstr(fq, text='1'))}" title="same rows plus the review text">Export with text</a>
    <a class="dbtn" href="https://uep-ai-complaints.pages.dev/index.html" target="_blank" rel="noopener">Read-only page ↗</a>
  </div>
</div>
<form class="dfilters" method="get" action="/">
  <div class="df"><label for="famsel">Family</label>{_sel('family', families, q.get('family'), 'All product families')}</div>
  <div class="df"><label for="datesel">Date</label>
    <input id="datesel" name="month" value="{esc(q.get('month', ''))}" placeholder="all dates · YYYY-MM"></div>
  <div class="df"><label for="winsel">Range</label>{range_sel}</div>
  <div class="df"><label for="srcsel">Source</label>{_sel('source', sources, q.get('source'), 'All sources')}</div>
  <div class="df grow"><label for="qsel">Search</label>
    <input id="qsel" name="q" value="{esc(q.get('q', ''))}" placeholder="search the review text"></div>
  <button class="primary dapply" type="submit">Apply</button>
  {f'<a class="dclear" href="/">clear</a>' if (fq or win) else ''}
</form>
<div class="dcards">{cards_html}</div>
<div class="duo">
  <section class="dpanel">
    <div class="dphead"><h2>What people complained about</h2>
      <span class="dpcount">{total:,} records</span></div>
    {mix_rows}
    <div class="dall" id="mixall" hidden>{mix_all}</div>
  </section>
  <section class="dpanel">
    <div class="dphead"><h2>Over time</h2><span class="dpcount">{trend_head}</span></div>
    {trend_html}
    <div class="dcap">{cap}</div>
  </section>
</div>
<div class="dstrip">{strip}</div>
<a class="dopen" href="{rec_href}">Open the complaint records →</a>
{about}
</div>"""
    ctx = dict(ctx or {})
    ctx["css"] = DASH_CSS
    ctx["scripts"] = DASH_MIX_JS
    return page("Complaint validation", body, "/", msg, ctx=ctx)


# ---------------------------------------------------------------- reviews list

# ---------------------------------------------------------------- provenance
PROV_AI, PROV_HUMAN, PROV_EXCL = "AI Classified", "Human Classified", "Excluded"


def provenance(dec):
    """WHO produced the label this record reports as.

    Deliberately not the same question as the status pill beside it: that one
    appears only when a row has no label at all, this one says whose label is
    the one being counted. 'rejected' is its own value because those records
    leave complaint reporting altogether — neither label is being reported.
    """
    if dec in ("accepted", "corrected"):
        return PROV_HUMAN
    if dec == "rejected":
        return PROV_EXCL
    return PROV_AI


def prov_badge(dec):
    p = provenance(dec)
    cls = {PROV_HUMAN: "human", PROV_EXCL: "excl"}.get(p, "ai")
    return f'<span class="pill prov {cls}" data-prov="{cls}">{p}</span>'


def p_reviews(cur, q, msg="", ctx=None):
    w, args = build_where(q)
    # F11: these are RETURN RECORDS, not reviews: 22,188 of 22,228 carry an Amazon
    # order_id and 8,516 are return-reason picklists; only the 456-row amazon feed
    # carries anything that reads like a review. One query gives the split the
    # header prints, so the three numbers cannot drift apart.
    cur.execute(f"""SELECT COUNT(*) c, COALESCE(SUM(v.has_prose=1),0) pr,
                           COALESCE(SUM(v.has_prose=0),0) co,
                           COALESCE(SUM(v.source='amazon'),0) rv
                      FROM v_ai_complaints v {w}""", args)
    h = cur.fetchone()
    n, n_pr, n_co, n_rv = int(h["c"]), int(h["pr"]), int(h["co"]), int(h["rv"])
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
        rid = r["review_id"]
        pill = ""
        if r["human_decision"] == "accepted":
            pill = '<span class="pill ok" data-pill>confirmed</span>'
        elif r["human_decision"] == "corrected":
            pill = '<span class="pill ok" data-pill>corrected</span>'
        elif r["human_decision"] == "rejected":
            pill = '<span class="pill bad" data-pill>not a complaint</span>'
        elif r["is_new_code"]:
            pill = '<span class="pill new" data-pill>new code</span>'
        elif not (r["final_complaint"] or "").strip():
            # The only row that is waiting for a person: nothing was labelled.
            pill = '<span class="pill bad" data-pill>unclassified</span>'
        else:
            # Labelled by the classifier — including its low-confidence calls.
            # No status tag: the work is done, so there is nothing to flag.
            pill = ""
        # Both sides of a correction go through the taxonomy spelling, so the row
        # never shows two casings of one code side by side.
        if r["human_decision"] == "corrected" and r["ai_complaint"]:
            label = (f'<b>{esc(display_label(r["final_complaint"], cur) or "")}</b> '
                     f'<span class="meta">(was {esc(display_label(r["ai_complaint"], cur))})</span>')
        else:
            label = esc(display_label(r["final_complaint"], cur) or "—")
        # The row itself is the control (see DRAWER_JS); the anchor is the
        # no-JS path to the same review, never a second tab for a JS user.
        trs.append(f"""<tr class="rowlink" data-review-id="{rid}" tabindex="0">
          <td class="mono">{esc(r["review_date"])}</td>
          <td class="mono">{amz_asin(r["asin"], r["title"])}</td>
          <td class="mono">{esc(r["parent"] or "—")}</td>
          <td>{amz_sku(r["sku"])}</td>
          <td>{label}<br>{pill} {prov_badge(r["human_decision"])}</td>
          <td class="rv" title="Click to read the full comment">{text_clip(r["txt"])}</td>
          <td class="open-cell"><a class="rowopen" href="/review/{rid}">open<span class="chev">›</span></a></td></tr>""")

    products = options(cur, "SELECT product k, COUNT(*) n FROM v_ai_complaints WHERE product IS NOT NULL GROUP BY product ORDER BY n DESC")
    parents = options(cur, "SELECT parent k, COUNT(*) n FROM v_ai_complaints WHERE parent IS NOT NULL GROUP BY parent ORDER BY n DESC")
    families = options(cur, "SELECT product_family k, COUNT(*) n FROM v_ai_complaints WHERE product_family IS NOT NULL GROUP BY product_family ORDER BY n DESC")
    sources = options(cur, "SELECT source k, COUNT(*) n FROM v_ai_complaints WHERE source IS NOT NULL GROUP BY source ORDER BY n DESC")
    countries = options(cur, "SELECT country k, COUNT(*) n FROM v_ai_complaints WHERE country IS NOT NULL GROUP BY country ORDER BY n DESC")
    # Grouped by the taxonomy label, not the stored string: 'Not Needed/unwanted'
    # and 'Not Needed/Unwanted' are one complaint and must be one option with one
    # count, or the filter list shows the same code twice.
    raw = options(cur, """SELECT final_complaint k, COUNT(*) n FROM v_ai_complaints
                          WHERE final_complaint IS NOT NULL GROUP BY final_complaint""")
    merged = {}
    for k, cnt in raw:
        lbl = display_label(k, cur)
        merged[lbl] = merged.get(lbl, 0) + cnt
    complaints = sorted(merged.items(), key=lambda kv: -kv[1])

    # The counts on the two status options are GLOBAL, not scoped to the current
    # filter: 'No label (41)' must read as a fact about the corpus, not drift
    # every time someone narrows a date range.
    cur.execute("""SELECT COALESCE(SUM(final_complaint IS NULL),0) nl,
                          COALESCE(SUM(needs_review=1),0) un
                     FROM v_ai_complaints""")
    _sc = cur.fetchone()
    nl_n, un_n = int(_sc["nl"]), int(_sc["un"])
    statuses = "".join(f'<option value="{v}"{" selected" if q.get("status")==v else ""}>{lbl}</option>'
                       for v, lbl in [("nolabel", f"No label ({nl_n:,})"),
                                      ("decided", "Decided"), ("newcode", "New code"),
                                      # Provenance rides in the same control: one dropdown,
                                      # because a second one beside it would filter the same
                                      # column and the two could contradict each other.
                                      ("ai", "AI Classified"), ("human", "Human Classified"),
                                      ("excluded", "Excluded"),
                                      # Last, deliberately: confidence is a question an
                                      # auditor asks, not a queue anyone is expected to
                                      # work. The rows behind it all carry a label.
                                      ("unsure", f"AI was unsure ({un_n:,})")])
    # The complaint dropdown keeps the stored value (it is a filter, so it must
    # match the column) and only changes what it reads as.
    complaint_select = select('complaint', [(k, display_label(k, cur), n) for k, n in complaints],
                              q.get("complaint") or "")
    # F8: echo which SKU values the filter actually matched. "sku=6" used to mean
    # "anything containing 6" (3,655 rows) when the SKU really called 6 has 2,613;
    # a filter that widens silently is indistinguishable from a wrong one.
    sku_echo = ""
    if q.get("sku"):
        cur.execute(f"SELECT v.sku k, COUNT(*) n FROM v_ai_complaints v {w} GROUP BY v.sku ORDER BY n DESC", args)
        hit = cur.fetchall()
        vals = ", ".join(f"{x['k']} ({x['n']:,})" for x in hit[:8])
        more = f" … +{len(hit) - 8} more" if len(hit) > 8 else ""
        sku_echo = (f' · SKU {"containing" if q.get("skulike") == "1" else "exactly"} '
                    f'“{esc(q["sku"])}” matched {len(hit)} value'
                    f'{"s" if len(hit) != 1 else ""}: <span class="mono">{esc(vals)}{more}</span>')
    body = f"""<h1>Complaint records</h1>
<p class="sub">{n:,} complaint records match — <b>{n_pr:,}</b> with a customer comment
  · <b>{n_co:,}</b> return-reason only · <b>{n_rv:,}</b> review text (the amazon feed){sku_echo}<br>
  page {pg} of {max(1, (n + PAGE_SIZE - 1)//PAGE_SIZE)} · showing {len(rows)}
  · <a href="/export.csv?{esc(qstr(q))}">export CSV</a>
  · <a href="/export.csv?{esc(qstr(q, text='1'))}">export with text</a>
  <br>Click any row to open that record here; SKU and ASIN open Amazon in a new tab.</p>
<form class="filters" method="get">
  <div><label>Search text or title</label><input name="q" value="{esc(q.get('q',''))}" placeholder="seat cushion"></div>
  <div><label>Product family</label>{select('family', families, q.get('family'))}</div>
  <div><label>Product / title</label><input name="product" value="{esc(q.get('product',''))}" placeholder="SC-B or sitzkissen"></div>
  <div><label>Parent</label>{select('parent', parents, q.get('parent'))}</div>
  <div><label>SKU</label><input name="sku" value="{esc(q.get('sku',''))}" placeholder="6 or SC-B-IR">
    <label style="text-transform:none;letter-spacing:0;font-weight:500;margin-top:4px">
      <input type="checkbox" name="skulike" value="1" style="min-width:0;margin:0 4px 0 0"
        {"checked" if q.get("skulike")=="1" else ""}>contains (default: exact)</label></div>
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
<div class="card"><table><thead><tr><th>Date</th><th>ASIN</th><th>Parent</th><th>SKU</th>
<th>Complaint</th><th>Comment</th><th></th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="7" class="empty">Nothing matches those filters.</td></tr>'}</tbody></table></div>
<div class="pager">{f'<a href="/reviews?{esc(qstr(q, page=pg-1))}">← previous</a>' if pg > 1 else ''}
  <span class="sp">page {pg}</span>
  {f'<a href="/reviews?{esc(qstr(q, page=pg+1))}">next →</a>' if off + PAGE_SIZE < n else ''}</div>
<div class="drw-bd" id="drwBd" hidden></div>
<aside class="drw" id="drw" role="dialog" aria-modal="true" aria-label="Review detail" hidden>
  <div class="drw-panel" id="drwPanel">
    <button class="drw-x" id="drwClose" type="button" aria-label="Close review">×</button>
    <div class="drw-body" id="drwBody"></div>
  </div>
</aside>"""
    ctx = dict(ctx or {})
    ctx["scripts"] = DRAWER_JS
    return page("Complaint records", body, "/reviews", msg, ctx=ctx)


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


def p_review(cur, rid, q, msg="", err=False, ctx=None, partial=False):
    """One record.

    partial=True returns ONLY the drawer's inner HTML (no <html>, nav or footer):
    /reviews fetches it into the slide-over panel, so a decision never costs the
    reviewer their filters, their page or their scroll position. The same function
    renders the standalone page for a direct link, so the two cannot drift.
    """
    cur.execute("SELECT * FROM v_ai_complaints v WHERE v.review_id=%s", (rid,))
    r = cur.fetchone()
    if not r:
        if partial:
            return ('<div class="drw-head"><div class="drw-kicker">Review</div>'
                    '<div class="drw-title">No such review</div></div>')
        return page("Not found", '<div class="card"><div class="empty">No such review.</div></div>',
                    "/reviews", msg or "No such review.", True, ctx)
    cur.execute("""SELECT id, decision, new_complaint, reviewed_by, reviewed_by_name, note, created_at
                     FROM corrections WHERE review_id=%s ORDER BY id DESC""", (rid,))
    prior = cur.fetchall()
    state = prior[0] if prior else None

    # Every label on this panel is the taxonomy spelling, resolved case-insensitively
    # against complaint_codes. The AI writes 'Not Needed/unwanted'; the taxonomy says
    # 'Not Needed/Unwanted', and printing the AI's casing beside the taxonomy's is
    # what made the page look like it had two of everything.
    ai = canonical(cur, r["ai_complaint"])
    rep = canonical(cur, r["final_complaint"])
    second = canonical(cur, r["second_complaint"])

    by_cat = defaultdict(list)
    for c in approved_codes(cur):
        by_cat[c["category"] or "Uncategorised"].append(c["code"])
    groups = "".join(
        f'<optgroup label="{esc(cat)}">' + "".join(
            # No preselection: "Correct" must be an explicit choice, or a stray
            # click on Save would record a decision nobody made. The Label section
            # above already states what the record reports as today.
            f'<option value="{esc(c)}">{esc(c)}</option>'
            for c in codes) + "</optgroup>"
        for cat, codes in sorted(by_cat.items()))

    hist = []
    for p in prior:
        cur_flag = " cur" if p is state else ""
        badge = ' <span class="pill ok">current</span>' if p is state else ""
        hist.append(
            f'<li class="{cur_flag.strip()}">'
            f'{decision_text(p["decision"], canonical(cur, p["new_complaint"]))}{badge}'
            f'<div class="m">{esc(p["created_at"])} · {reviewer_label(p["reviewed_by"], p["reviewed_by_name"])}'
            f'{" · " + esc(p["note"]) if p["note"] else ""}</div></li>')
    prior_html = ('<ul class="hist">' + "".join(hist) + "</ul>") if hist \
        else '<div class="drw-meta">No decisions recorded yet.</div>'

    if state:
        decision_box = (
            f'<div class="dec"><b>Decided:</b> '
            f'{decision_text(state["decision"], canonical(cur, state["new_complaint"]))}'
            f'<div class="m">by {reviewer_label(state["reviewed_by"], state["reviewed_by_name"])}'
            f' · {esc(state["created_at"])}'
            f'{"<br>note: " + esc(state["note"]) if state["note"] else ""}</div>'
            f'<div class="m">Recording another decision below <b>supersedes</b> this one: the row '
            f'above stays in the history and every figure switches to the new call.</div></div>')
    else:
        decision_box = (
            '<div class="dec"><b>No decision yet.</b>'
            '<div class="m">Whatever you record below is what the dashboard, the monthly rollup '
            'and the export use for this record.</div></div>')

    warn = ""
    if not r["has_prose"]:
        warn = ('<div class="syncbar bad" style="margin:0 0 4px"><b>Code only.</b> This row has no '
                'customer comment — the return reason is the whole text, so there is little to check '
                'the label against. You can still decide it.</div>')

    conf_card = ""
    if ai:
        conf_card = f"""<form class="act" method="post" action="/review/{rid}">
    <h4>Confirm</h4>
    <p>The AI's label is right for this record. One click, recorded against your login.</p>
    <div class="btnrow"><button class="primary" name="action" value="accept">Confirm “{esc(ai)}”</button></div>
  </form>"""

    title_tag = "h1" if not partial else "div"
    flash = (f'<div class="flash{" err" if err else ""}" style="margin:0 0 16px">{esc(msg)}</div>'
             if msg else "")
    who_name = esc((ctx or {}).get("who", {}).get("name", "unknown"))

    # Whose label is on this record, said in words. The badge on the label row
    # answers it at a glance; this sentence names the person and the date, since
    # a badge reading 'Human' without saying which human is not auditable.
    prov = prov_badge(state["decision"] if state else None)
    if state:
        _when = state["created_at"].strftime("%-d %b %Y") if state["created_at"] else "an unknown date"
        _who = (esc(state["reviewed_by_name"]) if state["reviewed_by_name"]
                else (f"user #{state['reviewed_by']}" if state["reviewed_by"] else "an unattributed user"))
        if state["decision"] == "corrected":
            prov_sentence = f"Corrected to <b>{esc(rep or '—')}</b> by {_who} on {_when}."
        elif state["decision"] == "accepted":
            prov_sentence = f"Confirmed by {_who} on {_when} — the AI’s label is the one being reported."
        else:
            prov_sentence = (f"Excluded by {_who} on {_when} — marked not a complaint, so it is out of "
                             f"complaint reporting entirely.")
    else:
        prov_sentence = ("Labelled by the classifier — no human decision, so the AI’s label is the one "
                         "being reported.")

    inner = f"""<div class="drw-head">
  <div class="drw-kicker">Review {esc(rid)} · {esc(r["review_date"])} · {esc(r["source"] or "")} {esc(r["country"] or "")}</div>
  <{title_tag} class="drw-title">{esc(r["product"] or "Unknown product")}{" — " + esc(short_title(r["title"])) if r["title"] else ""}</{title_tag}>
  <div class="drw-sub">parent {esc(r["parent"] or "—")}{(" · order " + esc(r["order_id"])) if r["order_id"] else ""}</div>
</div>
{flash}
{warn}
<div class="drw-sec">
  <h3>Customer comment</h3>
  <div class="drw-quote">{text_html(r["review_text"])}</div>
</div>
<div class="drw-sec">
  <h3>Label</h3>
  <div class="dec">
    <div class="lblrow"><span class="k">AI assigned</span>
      <span><b>{esc(ai or "—")}</b>{(" · " + esc(r["ai_category"])) if r["ai_category"] else ""}</span></div>
    {f'<div class="lblrow"><span class="k">Second choice</span><span class="drw-meta">{esc(second)}</span></div>' if second else ""}
    <div class="lblrow"><span class="k">Reporting as</span>
      <span><b>{esc(rep or "—")}</b>{(" · " + esc(r["final_category"])) if r["final_category"] else ""} {prov}</span></div>
    <div class="m">{prov_sentence}</div>
  </div>
</div>
<div class="drw-sec">
  <h3>Decision</h3>
  {decision_box}
</div>
<div class="drw-sec">
  <h3>Record a decision</h3>
  <p class="lead">Three separate actions. Each note is optional and is stored with the decision it belongs to.</p>
  {conf_card}
  <form class="act" method="post" action="/review/{rid}">
    <h4>Correct</h4>
    <p>{("Pick the code this record should report as." if ai else "The AI assigned no code — pick the one this record should report as, or mark it not a complaint below.")}
      The current label stays in the history.</p>
    <div><label for="pick-{rid}">Complaint code</label>
      <select id="pick-{rid}" name="pick"><option value="">choose a code…</option>{groups}</select></div>
    <div><label for="note-{rid}">Note (optional)</label>
      <input id="note-{rid}" name="note" placeholder="Why you changed it"></div>
    <div class="btnrow"><button name="action" value="correct">Save correction</button></div>
  </form>
  <form class="act danger" method="post" action="/review/{rid}">
    <h4>Not a complaint</h4>
    <p>Removes this record from complaint reporting — the dashboard, the monthly rollup and the export.</p>
    <div><label for="rnote-{rid}">Reason (optional)</label>
      <input id="rnote-{rid}" name="note" placeholder="e.g. delivery status, not a product complaint"></div>
    <div class="btnrow"><button class="danger" name="action" value="reject">Mark not a complaint</button></div>
  </form>
  <p class="lead">Recorded against your login ({who_name}), not a shared account.</p>
</div>
<div class="drw-sec">
  <h3>History</h3>
  {prior_html}
</div>
<div class="drw-sec">
  <h3>Record details</h3>
  <dl class="kv">
    <dt>Review</dt><dd class="mono">{esc(rid)}</dd>
    <dt>Date</dt><dd>{esc(r["review_date"])}</dd>
    <dt>Source</dt><dd>{esc(r["source"] or "—")} {esc(r["country"] or "")}</dd>
    <dt>Order</dt><dd class="mono">{esc(r["order_id"] or "—")}</dd>
    <dt>Product</dt><dd>{esc(r["product"] or "—")}</dd>
    <dt>Parent</dt><dd class="mono">{esc(r["parent"] or "—")}</dd>
    <dt>SKU</dt><dd>{amz_sku(r["sku"])}</dd>
    <dt>ASIN</dt><dd>{amz_asin(r["asin"])}</dd>
  </dl>
</div>"""

    if partial:
        return inner
    body = (f'<p class="sub" style="margin:0 0 4px"><a href="/reviews">← back to complaint records</a></p>'
            f'<div style="max-width:820px">{inner}</div>')
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
                "pct_of_sku_month", "n_human_validated", "n_repeat",
                # Exposure, appended rather than inserted: every existing column keeps
                # its position, so a saved CSV and a fresh one still line up by index.
                "orders", "units", "complaints_per_100_orders", "complaints_per_1000_units",
                # R7: the refund figures, because "did returns rise" is her question and
                # a complaint count cannot answer it.
                "refunds_us", "orders_us", "refund_rate_us_pct",
                # R1/R12: the grain and the flags that make this file safe to pivot.
                # orders/units/refunds are carried on ONE row per SKU-month (see
                # monthly_csv); exposure_row says which, so a SUM cannot multiply them.
                "grain", "exposure_row", "rate_quotable", "refund_rate_quotable"]

# The sku-month export (R1): one row per SKU-month, nothing repeated, safe to SUM.
MONTHLY_SKU_COLS = ["month", "quarter", "product_family", "product", "parent", "sku", "asin",
                    "title_short", "n_complaints", "n_codes", "orders", "units",
                    "refunds_us", "orders_us", "refund_rate_us_pct",
                    "complaints_per_100_orders", "complaints_per_1000_units",
                    "rate_quotable", "refund_rate_quotable", "has_sales", "grain"]


def month_grain(v):
    """'2026-07-14' or '2026-07' or '2026-7' -> '2026-07'. '' when unusable.

    The monthly page is month-grain, so a date has to be resolved to a month
    ONCE, in one place, and the page has to say that is what it did. Comparing
    the string '2026-07' to a date only worked by accident of collation
    (LEFT('2026-07-14',7) = '2026-07' >= '2026-07'), and the same 7-day range
    meant all of July here and one week on Complaint records.
    """
    m = re.match(r"^\s*(\d{4})-(\d{1,2})", (v or ""))
    if not m:
        return ""
    mo = int(m.group(2))
    return f"{int(m.group(1)):04d}-{mo:02d}" if 1 <= mo <= 12 else ""


def reviews_url_for_month(q):
    """The Complaint-records link for the SAME period this page filtered on.

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
    if q.get("sku"):
        # F7: /monthly could not be filtered by SKU at all — build_where_monthly
        # had no sku branch, so /monthly?sku=6 returned the same 4,527 rows as
        # /monthly. Exact by default, with the same explicit 'contains' option as
        # the review-grain page.
        if q.get("skulike") == "1":
            where.append("sku LIKE %s"); args.append(f'%{q["sku"]}%')
        else:
            where.append("sku=%s"); args.append(q["sku"].strip())
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
    cur.execute(f"""SELECT COUNT(*) c, COALESCE(SUM(n_repeat),0) r
                      FROM v_complaint_rates {w}""", args)
    hh = cur.fetchone()
    n, n_rep = int(hh["c"]), int(hh["r"])
    try:
        pg = max(1, int(q.get("page") or 1))
    except ValueError:
        pg = 1
    pages = max(1, (n + PAGE_ROLLUP - 1) // PAGE_ROLLUP)
    pg = min(pg, pages)
    off = (pg - 1) * PAGE_ROLLUP
    # v_complaint_rates is v_ai_complaints_monthly LEFT JOINed on
    # sales_by_sku_month (sku, month). The complaint counts are the same numbers —
    # apply_views asserts both the row count and SUM(n) are identical — and the
    # exposure columns are additive: nothing already on this page changed.
    cur.execute(f"""SELECT month, quarter, product_family, product, parent, sku, asin,
                           complaint, category, n_complaints, pct_of_sku_month,
                           n_human_validated, n_repeat,
                           orders, units, refunds, has_sales,
                           complaints_per_100_orders, complaints_per_1000_units,
                           refunds_us, orders_us, refund_rate_us_pct,
                           rate_quotable, refund_rate_quotable,
                           LEFT(COALESCE(title,''),70) title, title_short
                      FROM v_complaint_rates {w}
                     ORDER BY month DESC, n_complaints DESC, sku, complaint
                     LIMIT {PAGE_ROLLUP} OFFSET {off}""", args)
    rows = cur.fetchall()

    def _rate(v, quotable, denom, why):
        """An undefined rate is "—" with the reason in the tooltip — never 0.00.

        NULL from the view means either "no sales row for this SKU-month" or "that
        SKU-month sold 0 orders". Both make the ratio undefined, and 0.00 would
        read as "no complaints per order", which is a different and false claim.

        Below RATE_FLOOR orders the arithmetic exists but is not a rate (R3): the
        cell keeps the em dash and prints the denominator, so a 1-order SKU-month
        can never read "200.00 per 100 orders".
        """
        if v is None:
            return f'<td class="mono num" title="{esc(why)}">—</td>'
        if quotable is not None and not quotable:
            return (f'<td class="mono num" title="{esc(why)}">—'
                    f'<div class="meta">{int(denom or 0):,} orders — too few to quote a rate</div></td>')
        return f'<td class="mono num">{float(v):,.2f}</td>'

    def _num(v, why="no sales row for this SKU-month"):
        return (f'<td class="mono num">{int(v):,}</td>' if v is not None
                else f'<td class="mono num" title="{esc(why)}">—</td>')

    trs = "".join(
        f"""<tr><td class="mono">{esc(r["month"])}</td>
        <td>{esc(r["product"] or "—")}<div class="title">{esc(r["title"] or "")}</div></td>
        <td>{esc(r["product_family"] or "—")}</td>
        <td class="mono">{esc(r["parent"] or "—")}</td>
        <td>{amz_sku(r["sku"])}{f'<div class="title">{esc(r["title_short"])}</div>' if r["title_short"] else ""}</td>
        <td>{esc(display_label(r["complaint"], cur))}</td><td class="meta">{esc(r["category"] or "")}</td>
        <td class="mono num">{r["n_complaints"]:,}</td><td class="mono num">{r["pct_of_sku_month"]}%</td>
        <td class="mono num">{int(r["n_human_validated"] or 0):,}</td>
        <td class="mono num">{int(r["n_repeat"] or 0):,}</td>
        {_num(r["orders"])}{_num(r["units"])}
        {_rate(r["complaints_per_100_orders"], r["rate_quotable"], r["orders"],
               "no sales row for this SKU-month" if not r["has_sales"]
               else f'{int(r["orders"] or 0):,} orders — too few to quote a rate (floor {RATE_FLOOR})')}
        {_rate(r["complaints_per_1000_units"], None, r["units"],
               "no sales row for this SKU-month" if not r["has_sales"]
               else "0 units in this SKU-month: the rate is undefined, not zero")}
        {_rate(r["refund_rate_us_pct"], r["refund_rate_quotable"], r["orders_us"],
               "no US orders in this SKU-month" if not r["orders_us"]
               else f'{int(r["orders_us"] or 0):,} US orders — too few to quote a rate (floor {RATE_FLOOR})')}
        </tr>""" for r in rows)

    months = options(cur, """SELECT month k, SUM(n) n FROM v_ai_complaints_monthly
                              WHERE month IS NOT NULL GROUP BY month ORDER BY month DESC""")
    parents = options(cur, "SELECT parent k, SUM(n) n FROM v_ai_complaints_monthly WHERE parent IS NOT NULL GROUP BY parent ORDER BY n DESC")
    families = options(cur, "SELECT product_family k, SUM(n) n FROM v_ai_complaints_monthly WHERE product_family IS NOT NULL GROUP BY product_family ORDER BY n DESC")
    complaints = options(cur, "SELECT complaint k, SUM(n) n FROM v_ai_complaints_monthly GROUP BY complaint ORDER BY n DESC")
    complaint_sql = select('complaint', [(k, display_label(k, cur), v) for k, v in complaints], q.get("complaint") or "")
    mf, mt = month_grain(q.get("from")), month_grain(q.get("to"))
    # The months THIS page describes. With an explicit range it is that range; with
    # none it is the contiguous run that holds the corpus (the same rule the chart
    # uses) — 18 straggling rows back to 2023 would otherwise divide a five-month
    # selection by four years of orders and print the corpus's coverage as a rate.
    cur.execute(f"""SELECT month k, SUM(n_complaints) n FROM v_complaint_rates {w}
                     GROUP BY month ORDER BY month""", args)
    mm = [(r["k"], int(r["n"])) for r in cur.fetchall() if r["k"]]
    mwin = [m for m, _ in _trim_months(mm)[0]] if mm else None
    exp_line = exposure_line(cur, q, monthly=True, months=mwin)
    src_tbl = source_rate_table(cur, q)
    grain_note = ""
    if mf or mt:
        grain_note = (f' <b>Months {mf or "first"} to {mt or "last"}</b> — this page is month grain, so a date '
                      f'like the 14th means that whole month (the Complaint records link above carries month=).')
    body = f"""<h1>Monthly</h1>
<p class="sub">The grain the team reports on: SKU × month × complaint, with the share of that SKU-month and how
many rows a human validated. <b>Orders, units and the rate columns come from the seller's own finance
feed for the same SKU-month</b> (all marketplaces); a row with no sales row, or with zero orders, shows
“—” because the rate is undefined there — never 0. A rate is quoted only from {RATE_FLOOR:,} orders
upwards: below that the cell keeps the dash and prints how many orders it has, because 54 refunds on 2
orders is 2700% and means nothing. {n:,} rows match{'' if n == 1 else ''} · page {pg} of {pages}
(rows {off + 1:,}–{off + len(rows):,}) · <a href="/monthly.csv?{esc(qstr(q))}">export CSV</a>
· <a href="/monthly_sku.csv?{esc(qstr(q))}"><b>export at SKU-month grain</b></a>
(<b>use this one for pivots</b> — <code>/monthly.csv</code> carries a SKU-month's orders/units/refunds
on ONE row and blanks them on that SKU-month's other complaint rows, so SUM(orders) is correct but a
SUM by complaint code is not).
Process noise (Review Deletion) and unclassified categories are not in this rollup; the record-grain rows
behind any filter are on <a href="{esc(reviews_url_for_month(q))}">Complaint records</a>,
where the same filters also show the complaint mix and the per-feed rate series.{grain_note}<br>
<b>{n_rep:,}</b> rows in this rollup repeat a comment already recorded on the same order — the
<code>n_repeat</code> column carries them, they are counted in <code>n</code>, and nothing is removed;
the “distinct complaints” view on Complaint records is the only place they are dropped, on request.
(order-level detail is not in this grain: the record grain and both CSVs carry <code>order_id</code>
and <code>dup_rank</code>.)</p>
<form class="filters" method="get">
  <div><label>Month</label>{select('month', months, q.get('month'))}</div>
  <div><label>From month</label><input type="month" name="from" value="{esc(month_grain(q.get('from')))}"></div>
  <div><label>To month</label><input type="month" name="to" value="{esc(month_grain(q.get('to')))}"></div>
  <div><label>Product family</label>{select('family', families, q.get('family'))}</div>
  <div><label>Product / title</label><input name="product" value="{esc(q.get('product',''))}" placeholder="seat cushion"></div>
  <div><label>Parent</label>{select('parent', parents, q.get('parent'))}</div>
  <div><label>SKU</label><input name="sku" value="{esc(q.get('sku',''))}" placeholder="6 or SC-B-IR">
    <label style="text-transform:none;letter-spacing:0;font-weight:500;margin-top:4px">
      <input type="checkbox" name="skulike" value="1" style="min-width:0;margin:0 4px 0 0"
        {"checked" if q.get("skulike")=="1" else ""}>contains (default: exact)</label></div>
  <div><label>Complaint</label>{complaint_sql}</div>
  <div><button class="primary">Filter</button></div>
  <div><a href="/monthly">clear</a></div>
</form>
{exp_line}
{src_tbl}
<div class="card"><table><thead><tr><th>Month</th><th>Product</th><th>Family</th><th>Parent</th><th>SKU</th>
<th>Complaint</th><th>Category</th><th class="num">Records</th><th class="num">% of SKU-month</th>
<th class="num">Human validated</th><th class="num">Repeats</th>
<th class="num">Orders</th><th class="num">Units</th>
<th class="num">per 100 orders</th><th class="num">per 1,000 units</th>
<th class="num">US return rate</th></tr></thead>
<tbody>{trs or '<tr><td colspan="16" class="empty">Nothing matches those filters.</td></tr>'}</tbody></table></div>
<div class="pager">{f'<a href="/monthly?{esc(qstr(q, page=pg-1))}">← previous</a>' if pg > 1 else ''}
  <span class="sp">page {pg} of {pages}</span>
  {f'<a href="/monthly?{esc(qstr(q, page=pg+1))}">next →</a>' if pg < pages else ''}</div>"""
    return page("Monthly", body, "/monthly", msg, ctx=ctx)


def monthly_csv(cur, q):
    """The complaint-code grain, with ONE exposure row per SKU-month (R1).

    The defect this fixes: orders/units used to be repeated on every complaint-code
    row of a SKU-month, so a pivot that summed them multiplied the denominator by
    the number of codes — seat cushions, 2026-07 summed to 552,965 orders against a
    true 32,848 (16.83x).

    Now the denominator is written on the FIRST row of each (sku, month) and left
    EMPTY on the rest, and `exposure_row` says which row carries it. SUM(orders)
    over the whole file is then the true total, and `grain` names the grain on
    every row so nobody has to infer it. For a pivot by SKU or month use
    /monthly_sku.csv, which is one row per SKU-month with nothing repeated.
    """
    w, args = build_where_monthly(q)
    cur.execute(f"""SELECT month, quarter, product_family, product, parent, sku, asin, title,
                           title_short, complaint, category, n_complaints, pct_of_sku_month,
                           n_human_validated, n_repeat,
                           orders, units, refunds_us, orders_us,
                           complaints_per_100_orders, complaints_per_1000_units,
                           refund_rate_us_pct, rate_quotable, refund_rate_quotable
                      FROM v_complaint_rates {w}
                     ORDER BY month DESC, n_complaints DESC, sku, complaint""", args)
    buf = io.StringIO()
    wtr = csv.writer(buf)
    wtr.writerow(MONTHLY_COLS)
    seen = set()
    for r in cur.fetchall():
        row = dict(r)
        # `n` keeps its old name in the export so a saved CSV and this one still
        # line up column-for-column; the view calls it n_complaints because the
        # rate columns made a bare "n" ambiguous.
        row["n"] = row.pop("n_complaints")
        # The STORED code in `complaint` — the same value /export.csv writes — so
        # the two exports join. The readable label is a separate column, added
        # here and in export_csv, never a rename of one and not the other.
        row["complaint_display"] = display_label(row.get("complaint"), cur) or ""
        row["grain"] = "sku_month_complaint"
        key = (row.get("sku"), row.get("month"))
        first = key not in seen
        seen.add(key)
        row["exposure_row"] = 1 if first else 0
        if not first:
            # Blank, not 0: an empty cell is "not carried here", and a pivot that
            # averages a fake 0 would report a denominator nobody measured.
            for c in ("orders", "units", "refunds_us", "orders_us",
                      "complaints_per_100_orders", "complaints_per_1000_units",
                      "refund_rate_us_pct"):
                row[c] = ""
        # An undefined rate is written as an EMPTY CSV cell, not 0.
        wtr.writerow([row.get(c) for c in MONTHLY_COLS])
    return (buf.getvalue().encode("utf-8-sig"), "text/csv",
            f"attachment; filename=complaint-records-monthly-{date.today().isoformat()}.csv")


def monthly_sku_csv(cur, q):
    """One row per SKU-month — the pivot file (R1, R7, R12).

    Rows are the SKU-months of the PRODUCT SCOPE (the product filters, with the
    complaint filter removed) in the months the filter touches, taken from the
    sales table and LEFT JOINed to the filtered complaint counts. So:

      * every SKU-month appears ONCE, and orders/units/refunds appear once with it;
      * a SKU-month that recorded nothing under the current filter appears with
        n_complaints = 0 and its orders — which is what makes the sum of the file
        equal to the exposure line's denominator (a family's August denominator is
        39,390, not the 38,513 of the SKU-months that happened to complain);
      * a SKU-month with complaints but no sales row still appears, with empty
        orders and an empty rate.

    Sum n_complaints, orders, units, refunds_us and orders_us down any slice; do
    NOT sum complaints_per_100_orders — recompute it from the totals, because an
    average of rates is not a rate.
    """
    w, args = build_where_monthly(q)
    cur.execute(f"""SELECT sku, month, SUM(n_complaints) n_complaints, COUNT(*) n_codes,
                           MAX(quarter) quarter
                      FROM v_complaint_rates {w} GROUP BY sku, month""", args)
    comp = {(r["sku"], r["month"]): r for r in cur.fetchall()}
    months = sorted({m for (_, m) in comp if m})
    if not months:
        # No matching complaint anywhere: still export the scope's sales, so the
        # file is a true exposure file rather than an empty one.
        mf, mt = month_grain(q.get("from")), month_grain(q.get("to"))
        months = [mf or "2026-06", mt or mf or "2026-10"]
    skus = scope_skus(cur, q)
    dims = {}
    if skus:
        ph = ",".join(["%s"] * len(skus))
        cur.execute(f"""SELECT sku, MAX(product_family) product_family, MAX(parent) parent,
                               MAX(product) product, MAX(title_short) title_short, MAX(asin) asin
                          FROM v_ai_complaints_monthly WHERE sku IN ({ph}) GROUP BY sku""", skus)
        dims = {r["sku"]: r for r in cur.fetchall()}
        cur.execute(f"""SELECT sku, month, orders, units, refunds_us, orders_us
                          FROM sales_by_sku_month
                         WHERE sku IN ({ph}) AND month BETWEEN %s AND %s""",
                    skus + [months[0], months[-1]])
        sales = {(r["sku"], r["month"]): r for r in cur.fetchall()}
    else:
        sales = {}

    buf = io.StringIO()
    wtr = csv.writer(buf)
    wtr.writerow(MONTHLY_SKU_COLS)
    keys = sorted({(s, m) for (s, m) in sales} | {(s, m) for (s, m) in comp},
                  key=lambda k: (k[1], k[0] or ""), reverse=True)
    for sku, month in keys:
        s, c, d = sales.get((sku, month)), comp.get((sku, month)), dims.get(sku, {})
        n = int(c["n_complaints"]) if c else 0
        o = int(s["orders"]) if s and s["orders"] is not None else None
        u = int(s["units"]) if s and s["units"] is not None else None
        rfu = int(s["refunds_us"]) if s and s["refunds_us"] is not None else None
        ou = int(s["orders_us"]) if s and s["orders_us"] is not None else None
        wtr.writerow([
            month, (c or {}).get("quarter") or "", d.get("product_family") or "",
            d.get("product") or "", d.get("parent") or "", sku, d.get("asin") or "",
            d.get("title_short") or "", n, int(c["n_codes"]) if c else 0,
            "" if o is None else o, "" if u is None else u,
            "" if rfu is None else rfu, "" if ou is None else ou,
            # R12: below the floor the ratio is EMPTY, not 2700%.
            (round(100.0 * rfu / ou, 2) if ou and ou >= RATE_FLOOR else ""),
            (round(100.0 * n / o, 2) if o else ""),
            (round(1000.0 * n / u, 2) if u else ""),
            1 if (o and o >= RATE_FLOOR) else 0,
            1 if (ou and ou >= RATE_FLOOR) else 0,
            1 if s else 0,
            "sku_month",
        ])
    return (buf.getvalue().encode("utf-8-sig"), "text/csv",
            f"attachment; filename=complaint-records-by-sku-month-{date.today().isoformat()}.csv")


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
vocabulary with a category — its records stop being “new”, and the code is written to the vocabulary file the
classifier loads, so the next classification batch can emit it. <b>Deny</b> suppresses the code and remaps its
records to the one you choose; a deny without a replacement is refused, because it would silently drop those
records out of every rollup. Where a replacement was prescribed for the code, it is selected in the dropdown and
named on the button — nothing is applied behind your back.</p>
<div class="card"><table><thead><tr><th>Code</th><th class="num">Records</th><th>Example</th><th>Decision</th></tr></thead>
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
        return ("A deny needs a replacement code: without one those records keep a label you rejected, "
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

    The pool is rows with a CUSTOMER COMMENT (`has_prose=1`) — 8,516 of 22,228
    rows carry only the return-reason picklist, so a sample drawn from everything
    was 38% uncheckable: the checker is asked to grade a label against 'Too big'.

    F9: that change takes effect from PROSE_POOL_FROM. A sample already drawn for
    an earlier week is NOT re-drawn (the checker may have started it), so those
    weeks keep their code-only rows and the page marks them N/A instead of asking
    for a call that would be discarded.
    """
    wk = iso_week()
    cur.execute("SELECT COUNT(*) n FROM weekly_samples WHERE sample_week=%s", (wk,))
    if cur.fetchone()["n"]:
        return wk
    prose_only = wk >= PROSE_POOL_FROM
    cur.execute("""SELECT review_id, COALESCE(final_category,'Uncategorised') cat
                     FROM v_ai_complaints
                    WHERE review_date IS NOT NULL AND final_complaint IS NOT NULL"""
                + (" AND has_prose=1" if prose_only else ""))
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
        if int(r.get("has_prose") or 0) != 1:
            # F9: a picklist-only row has nothing to grade a label against. Asking
            # for a call that sample_stats() then throws away produces a number
            # nobody used and a checker who thinks they were ignored.
            sel = ('<span class="pill">N/A — no customer comment</span>')
        else:
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
<p class="sub">The weekly random check: {SAMPLE_N} complaint records drawn at random from the classified corpus,
stratified by complaint category so a small but important category is not swamped by FBA returns.
Record the call for each row — the agreement is computed from your calls, there is no percentage to type.
“Not a complaint” leaves the denominator, exactly as in the sheet workflow. <b>The sample is drawn only
from records that carry a customer comment from {esc(PROSE_POOL_FROM)}</b>; before that the pool was the
whole corpus, so this week ({esc(wk)}) still carries {skipped} picklist-only
row{' ' if skipped == 1 else 's '}— marked <b>N/A</b> here, excluded from the agreement, and not asked for.
The rule that labels those rows is published on <a href="/rules">Rules</a>, so the 8,516 rows no sample can
reach are still auditable.</p>
<div class="grid">
  <div class="kpi"><div class="n">{head}</div><div class="l">This week ({esc(wk)})</div><div class="d">{note}</div></div>
  <div class="kpi"><div class="n">{len(checks)}</div><div class="l">Weekly checks logged</div><div class="d">taper record</div></div>
  <div class="kpi"><div class="n">{coverage:.1f}%</div><div class="l">Queue coverage</div>
    <div class="d">not accuracy — share of complaint records a human has decided</div></div>
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


# ---------------------------------------------------------------- rules

def p_rules(cur, q, msg="", ctx=None):
    """F9: publish the picklist -> code mapping the sample can never check.

    8,516 rows (38%) carry only the return-reason picklist, so the weekly
    prose-only sample cannot reach them — including 4,993 of the 7,861
    Not Needed/Unwanted rows (63% of the top bar). Their labels come from a
    deterministic mapping over 2,290 distinct picklist strings. Auditing 2,290
    strings once is possible; auditing 8,516 unreadable rows is not. So the
    mapping is published here, ranked.

    The default view is the top 40 strings. The review estimated that would cover
    ~80% of the rows; measured, it covers 62% and 80% needs ~595, because the tail
    is long. The page prints both numbers instead of the flattering one.
    """
    raw = (q.get("n") or "").strip()
    try:
        limit = min(5000, int(raw)) if raw else 40
    except ValueError:
        limit = 40
    if limit < 1:
        limit = 40
    cur.execute("""SELECT TRIM(review_text) t, COUNT(*) n,
                          COUNT(DISTINCT final_complaint) n_codes,
                          GROUP_CONCAT(DISTINCT final_complaint ORDER BY final_complaint
                                      SEPARATOR ' | ') codes,
                          MAX(final_category) cat
                     FROM v_ai_complaints
                    WHERE has_prose=0 AND review_text IS NOT NULL AND TRIM(review_text) <> ''
                    GROUP BY TRIM(review_text)
                    ORDER BY n DESC, t""")
    all_rows = cur.fetchall()
    cur.execute("""SELECT COUNT(*) n, COUNT(DISTINCT TRIM(review_text)) k
                     FROM v_ai_complaints
                    WHERE has_prose=0 AND review_text IS NOT NULL AND TRIM(review_text) <> ''""")
    tot = cur.fetchone()
    cur.execute("SELECT COUNT(*) n FROM v_ai_complaints WHERE has_prose=0")
    no_prose = int(cur.fetchone()["n"] or 0)
    total = int(tot["n"] or 0) or 1
    multi = [r for r in all_rows if int(r["n_codes"] or 0) > 1]

    # How many strings it actually takes to reach 80%. The review's "top ~40 cover
    # ~80%" is the SHAPE of the distribution, not a fact about it: measured, 40
    # strings hold 62% and 80% needs ~595, because the tail is long. The page says
    # both numbers rather than choosing the flattering one.
    n80, acc80 = None, 0
    for i, r in enumerate(all_rows, 1):
        acc80 += int(r["n"])
        if acc80 >= total * 0.8:
            n80 = i
            break

    shown, acc = all_rows[:limit], 0
    trs = []
    for i, r in enumerate(shown, 1):
        acc += int(r["n"])
        flag = (' <span class="pill bad">maps to %d codes</span>' % r["n_codes"]
                if int(r["n_codes"] or 0) > 1 else "")
        trs.append(
            f'<tr><td class="mono num">{i}</td>'
            f'<td class="mono">{esc(r["t"])}</td>'
            f'<td><b>{esc(r["codes"] or "(no code)")}</b>{flag}</td>'
            f'<td class="meta">{esc(r["cat"] or "")}</td>'
            f'<td class="mono num">{int(r["n"]):,}</td>'
            f'<td class="mono num">{int(r["n"]) * 100.0 / total:.1f}%</td>'
            f'<td class="mono num">{acc * 100.0 / total:.1f}%</td></tr>')

    more = ""
    if len(all_rows) > limit:
        more = (f' · <a href="/rules?n={min(len(all_rows), limit * 4)}">show more</a>'
                f' · <a href="/rules?n={len(all_rows)}">show all {len(all_rows):,} strings</a>')
    body = f"""<h1>Label rules</h1>
<p class="sub">The rule path, published so it can be reviewed once. <b>{total:,}</b> complaint records carry a
return-reason picklist as their whole text ({no_prose - total:,} more have no text at all), and
<b>{int(tot["k"]):,}</b> distinct picklist strings produce their label through a deterministic mapping — these
rows can never enter the prose sample, which is why the mapping is the thing to audit. <b>{len(all_rows):,}</b>
strings, of which <b>{len(multi)}</b>
{'maps' if len(multi) == 1 else 'map'} to more than one code (flagged below). The top {len(shown)} strings shown
here hold <b>{acc * 100.0 / total:.1f}%</b> of the rows; the distribution is long-tailed, so covering 80% takes
{n80:,} of the {len(all_rows):,} strings — “show all” lists them, ranked.{more}</p>
<div class="card"><table><thead><tr><th class="num">#</th><th>Picklist string (as recorded)</th>
<th>Resolved code</th><th>Category</th><th class="num">Records</th><th class="num">Share</th>
<th class="num">Cumulative</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="7" class="empty">No picklist-only rows.</td></tr>'}</tbody></table></div>
<p class="mixnote">A string that maps to two codes is a real defect in the rule, not a display quirk: the same
return reason is counted under two complaints, so each looks smaller than it is. Prose rows are graded by the
weekly sample; these are graded here.</p>"""
    return page("Label rules", body, "/rules", msg, ctx=ctx)


# ---------------------------------------------------------------- export

EXPORT_COLS = ["review_id", "review_date", "month", "product_family", "product", "parent", "sku",
               "asin", "title", "order_id", "source", "country", "ai_complaint", "ai_category",
               "final_complaint", "complaint_display", "final_category", "human_decision",
               "human_complaint", "needs_review", "is_new_code", "has_prose", "dup_rank", "dup_group"]


def export_csv(cur, q):
    w, args = build_where(q)
    cols = EXPORT_COLS + (["review_text"] if q.get("text") == "1" else [])
    cur.execute(f"""SELECT v.review_id, v.review_date, DATE_FORMAT(v.review_date,'%%Y-%%m') month,
                           v.product_family, v.product, v.parent, v.sku, v.asin, v.title, v.order_id,
                           v.source, v.country,
                           v.ai_complaint, v.ai_category, v.final_complaint, v.final_category,
                           v.human_decision, v.human_complaint, v.needs_review, v.is_new_code,
                           v.has_prose, v.dup_rank, v.dup_group, v.review_text
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
        row["complaint_display"] = display_label(row.get("final_complaint"), cur) or ""
        wtr.writerow([row.get(c) for c in cols])
    return (buf.getvalue().encode("utf-8-sig"), "text/csv",
            f"attachment; filename=complaint-records-{date.today().isoformat()}.csv")


# ---------------------------------------------------------------- sign-in

LOGIN_CSS = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:#f1f2f4;color:#1f2430;
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
.brandmark{display:flex;justify-content:center;margin:0 0 28px}
.brandmark img{display:block;width:100%;max-width:200px;height:auto;border-radius:8px}
.wrap{display:flex;justify-content:center;padding:6vh 18px 60px}
.card{background:#fff;border:1px solid #e2e5ea;border-radius:10px;width:100%;max-width:420px;
  padding:30px 32px 26px;box-shadow:0 1px 2px rgba(16,24,40,.04),0 14px 34px rgba(16,24,40,.07)}
.card h1{margin:0 0 20px;font-size:23px;font-weight:700;letter-spacing:-.01em;color:#1f2430}
.field{margin-bottom:16px}
.field label{display:block;font-size:12.5px;font-weight:600;color:#3c4453;margin-bottom:6px}
.field input{width:100%;padding:10px 12px;border:1px solid #d4d8df;border-radius:6px;background:#fff;
  color:#1f2430;font:inherit;font-size:14.5px}
.field input:focus{outline:none;border-color:#1a56db;box-shadow:0 0 0 3px rgba(26,86,219,.13)}
.err{background:#fdf2f2;border:1px solid #f4c1bf;color:#a3221b;border-radius:6px;
  padding:9px 12px;font-size:13px;margin-bottom:16px}
.actions{display:flex;justify-content:flex-end;margin-top:22px}
button.login{background:#1a56db;border:1px solid #1a56db;color:#fff;font:inherit;font-weight:600;
  padding:10px 30px;border-radius:6px;cursor:pointer}
button.login:hover{background:#1748b6}
@media(max-width:520px){
  .brandmark img{max-width:170px}
  .wrap{padding:2vh 14px 40px}.card{padding:24px 20px}
}
"""

LOGIN_ERROR = "That email and password do not match our records"


def login_page(error="", email="", next_path="/"):
    """The sign-in card. Self-contained CSS, no JS, no CDN — it is the first thing a
    user sees, so it must not depend on the app's data. The one external request is
    the logo from this app's own /static route (no other host, no database)."""
    err = f'<div class="err" role="alert">{esc(error)}</div>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login · AI Complaint Validation</title><style>{LOGIN_CSS}</style></head>
<body>
<div class="wrap"><div class="card">
<div class="brandmark"><img src="/static/logo-full.png" alt="Upper Echelon Products"
     width="200" height="60"></div>
<h1>Login</h1>
{err}
<form method="post" action="/login" novalidate>
<input type="hidden" name="next" value="{esc(next_path)}">
<div class="field"><label for="email">Email</label>
<input id="email" name="email" type="email" inputmode="email" autocomplete="username"
       spellcheck="false" value="{esc(email)}" autofocus required></div>
<div class="field"><label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required></div>
<div class="actions"><button class="login" type="submit">Login</button></div>
</form>
</div></div></body></html>"""


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(txt):
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def sign_session(username):
    """'<payload>.<hmac>' where payload is base64url('name|exp_unix').

    The username is the only identity in the cookie; the password is never in it,
    and the HMAC makes the pair unforgeable without APP_SECRET.
    """
    if not APP_SECRET:
        raise RuntimeError("APP_SECRET is not set — cannot sign a session")
    exp = int(time.time()) + SESSION_HOURS * 3600
    payload = _b64e(f"{username}|{exp}".encode("utf-8"))
    sig = hmac.new(APP_SECRET.encode("utf-8"), payload.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session(token):
    """-> username, or None if the signature is wrong, the cookie is malformed or
    the 12 hours are up. Never raises: a broken cookie is just 'not signed in'."""
    if not token or not APP_SECRET or "." not in token:
        return None
    payload, _, sig = token.rpartition(".")
    want = hmac.new(APP_SECRET.encode("utf-8"), payload.encode("ascii", "ignore"),
                    hashlib.sha256).hexdigest()
    if not secrets.compare_digest(sig, want):
        return None
    try:
        name, _, exp = _b64d(payload).decode("utf-8").rpartition("|")
        if int(exp) < int(time.time()):
            return None
    except Exception:                                          # noqa: BLE001
        return None
    return name or None


def cookie_value(environ, name):
    for part in (environ.get("HTTP_COOKIE") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return urllib.parse.unquote(v.strip())
    return ""


def secure_request(environ):
    """The proxy terminates TLS, so wsgi.url_scheme is http here; the only honest
    signal is X-Forwarded-Proto. Secure is set when (and only when) that says https."""
    proto = (environ.get("HTTP_X_FORWARDED_PROTO") or "").split(",")[0].strip().lower()
    return proto == "https" or environ.get("wsgi.url_scheme") == "https"


def safe_next(target, default="/"):
    """Same-origin paths only. '//evil.com' is a protocol-relative ABSOLUTE url, so
    'starts with /' is not enough — it must start with exactly one '/'."""
    t = (target or "").strip()
    if (not t.startswith("/") or t.startswith("//") or "\\" in t
            or "\r" in t or "\n" in t):
        return default
    # Never bounce straight into the auth endpoints themselves: next=/logout would
    # sign the user out the instant they signed in.
    if t in ("/login", "/logout") or t.startswith("/login?") or t.startswith("/logout?"):
        return default
    return t


def check_login(identifier, password):
    """-> the user dict, or None. The Email field takes a username OR an address:
    the part before '@' is the username (chadia@… signs in as chadia)."""
    ident = (identifier or "").strip()
    if not ident or not password:
        return None
    local = ident.split("@", 1)[0].strip()
    rec = USERS.get(local)
    if rec is None:
        low = local.lower()
        rec = next((u for n, u in USERS.items() if n.lower() == low), None)
    if not rec or not rec.get("pw"):
        return None
    if not secrets.compare_digest(password.encode("utf-8"), rec["pw"].encode("utf-8")):
        return None
    return rec


def start_session(start, username, next_path, secure):
    cookie = (f"{SESSION_COOKIE}={sign_session(username)}; Path=/; "
              f"Max-Age={SESSION_HOURS * 3600}; HttpOnly; SameSite=Lax")
    if secure:
        cookie += "; Secure"
    start("302 Found", [("Location", next_path), ("Set-Cookie", cookie),
                        ("Cache-Control", "no-store"), ("Content-Length", "0")])
    return [b""]


def end_session(start, secure):
    cookie = (f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
    if secure:
        cookie += "; Secure"
    start("302 Found", [("Location", "/login"), ("Set-Cookie", cookie),
                        ("Cache-Control", "no-store"), ("Content-Length", "0")])
    return [b""]


def auth_user(environ):
    """The signed-in user as {pw, uid, name}, or None.

    Returning the person (not a boolean) is the whole point: every write stamps
    reviewed_by from this dict, so the audit trail names whoever actually decided.
    """
    rec = USERS.get(verify_session(cookie_value(environ, SESSION_COOKIE)) or "")
    if rec:
        return rec
    # Automation fallback (scripts, uptime checks): accept Basic, but NEVER
    # advertise it — no WWW-Authenticate — so a browser never opens its grey
    # dialog. People sign in at /login.
    auth = environ.get("HTTP_AUTHORIZATION", "")
    if auth.startswith("Basic ") and cookie_value(environ, SESSION_COOKIE) == "":
        try:
            u, _, p = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
        except Exception:                                      # noqa: BLE001
            return None
        rec = USERS.get(u)
        if rec and rec["pw"] and secrets.compare_digest(p.encode("utf-8"),
                                                        rec["pw"].encode("utf-8")):
            return rec
    return None


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

    secure = secure_request(environ)

    # Brand assets: unauthenticated on purpose — the sign-in card shows the logo
    # before anyone is signed in. Whitelisted names only (see STATIC_FILES).
    if path.startswith("/static/"):
        name = path[len("/static/"):]
        ctype = STATIC_FILES.get(name)
        data = None
        if ctype:
            try:
                with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                    data = fh.read()
            except OSError:
                data = None
        if data is None:
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8"),
                                             ("Content-Length", "9")])
            return [b"not found"]
        start_response("200 OK", [("Content-Type", ctype),
                                  ("Content-Length", str(len(data))),
                                  ("Cache-Control", "public, max-age=86400")])
        return [data]

    if path == "/logout":
        return end_session(start_response, secure)

    if path == "/login":
        qs_next = safe_next(parse_qs(environ).get("next", "/"))
        if method == "POST":
            f = parse_form(environ)
            nxt = safe_next(f.get("next"), qs_next)
            ident = (f.get("email") or "").strip()
            rec = check_login(ident, f.get("password") or "")
            if rec:
                return start_session(start_response, rec["name"], nxt, secure)
            # 401 with the card re-rendered in place: no redirect, so a mistyped
            # password cannot bounce the user through a loop.
            body = login_page(LOGIN_ERROR, ident, nxt).encode("utf-8")
            start_response("401 Unauthorized",
                           [("Content-Type", "text/html; charset=utf-8"),
                            ("Cache-Control", "no-store"),
                            ("Content-Length", str(len(body)))])
            return [body]
        who = auth_user(environ)
        if who:                       # already signed in: no reason to show the form
            nxt = safe_next(parse_qs(environ).get("next", "/"))
            start_response("302 Found", [("Location", nxt), ("Content-Length", "0")])
            return [b""]
        body = login_page("", "", qs_next).encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Cache-Control", "no-store"),
                                  ("Content-Length", str(len(body)))])
        return [body]

    who = auth_user(environ)
    if not who:
        target = path + (("?" + environ["QUERY_STRING"]) if environ.get("QUERY_STRING") else "")
        start_response("302 Found",
                       [("Location", "/login?next=" + urllib.parse.quote(target, safe="")),
                        ("Cache-Control", "no-store"), ("Content-Length", "0")])
        return [b""]

    q = parse_qs(environ)
    msg, err = q.get("msg", ""), q.get("err") == "1"
    ctx = {}
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
            f = parse_form(environ)
            # The drawer posts the same form fields as the standalone page; the only
            # difference is that it gets the panel back instead of a 302, so the
            # reviewer does not lose the filtered table behind it.
            drawer = f.get("drawer") == "1" or environ.get("HTTP_X_DRAWER") == "1"
            m, e = do_review(cur, rid, f, who)
            if not drawer:
                return redirect(start_response, f"/review/{rid}", m, e)
            cur.execute("""SELECT decision FROM corrections WHERE review_id=%s
                            ORDER BY id DESC LIMIT 1""", (rid,))
            now = cur.fetchone()
            html = p_review(cur, rid, q, m, e, ctx, partial=True)
            start_response("200 OK", [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-store"),
                ("X-Row-State", "" if e else (now["decision"] if now else "")),
                ("Content-Length", str(len(html.encode("utf-8"))))])
            return [html.encode("utf-8")]

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

        if path == "/monthly_sku.csv":
            body, ctype, disp = monthly_sku_csv(cur, q)
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
            html = p_review(cur, rid, q, msg, err, ctx,
                            partial=(q.get("partial") == "1" or environ.get("HTTP_X_DRAWER") == "1"))
        elif path == "/codes":
            html = p_codes(cur, q, msg, ctx)
        elif path == "/quality":
            html = p_quality(cur, q, msg, ctx)
        elif path == "/monthly":
            html = p_monthly(cur, q, msg, ctx)
        elif path == "/rules":
            html = p_rules(cur, q, msg, ctx)
        else:
            html = p_dashboard(cur, q, msg, ctx)

        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]
    except pymysql.err.OperationalError as e:
        # F14: a query can still hit max_statement_time on a busy box. That must be
        # one broken page with a reason, not a traceback and not a hung server.
        code = e.args[0] if e.args else 0
        if code not in (1969, 1205, 1213, 2006, 2013):
            raise
        body = page("Slow query",
                    '<div class="card"><div class="empty"><b>That page took too long.</b><br>'
                    f'The database stopped the query after '
                    f'{esc(os.environ.get("APP_STMT_TIMEOUT", "30"))}s ({esc(str(e)[:160])}). '
                    'Nothing was changed — reload to try again, or narrow the filter.</div></div>',
                    "", "", True, ctx)
        start_response("503 Service Unavailable", [("Content-Type", "text/html; charset=utf-8")])
        return [body.encode("utf-8")]
    finally:
        conn.close()


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """One thread per request (F14).

    wsgiref's default server handles one request at a time, so one slow query
    froze every page AND held the connection open for ~18 minutes, which also
    blocked the nightly RENAME that swaps in review_duplicates. daemon_threads so
    a stuck page cannot keep the process alive on shutdown.
    """
    daemon_threads = True
    request_queue_size = 32


if __name__ == "__main__":
    if not any(u["pw"] for u in USERS.values()):
        sys.exit("APP_PASS not set — refusing to serve without a password")
    # No default key, ever: a built-in APP_SECRET would let anyone who can read
    # this file forge a session for any username. Fail before binding the port.
    if not APP_SECRET:
        sys.exit("APP_SECRET is not set — refusing to serve: sessions cannot be signed "
                 "without it.\n  Generate one with:  openssl rand -hex 32\n"
                 "  Add the resulting value to app.env as a line starting APP_SECRET= ,\n"
                 "  mode 600, readable by the service user. deploy_on_box.sh writes it on a\n"
                 "  fresh install and backfills it on re-run.")
    if len(APP_SECRET) < 16:
        sys.exit("APP_SECRET is too short (<16 chars) — it signs every session cookie.\n"
                 "  Generate one with: openssl rand -hex 32")
    port = int(os.environ.get("APP_PORT", "8080"))
    host = os.environ.get("APP_HOST", "127.0.0.1")
    print(f"serving on {host}:{port} as {', '.join(sorted(USERS))} "
          f"(cookie sessions, {SESSION_HOURS}h, threaded, statement timeout "
          f"{os.environ.get('APP_STMT_TIMEOUT', '30')}s)")
    make_server(host, port, app, server_class=ThreadingWSGIServer).serve_forever()
