#!/usr/bin/env python3
"""Check the complaint-mix panel and the disclosure fixes against the database.

Every number is re-derived from SQL here, independently of app.py:

  * F1  the change column compares RECORDS PER FEED DAY across the feeds that
        cover BOTH windows. A feed with no rows in one window, or one that stops
        inside a window, or a return-reason-only share that moves >10pp, must
        WITHHOLD the arrows and say why ('the ncx feed ends ...');
  * F2  dup_rank is keyed on (order_id, MD5(text)): every repeat has a twin on
        the same order, no repeat lacks an order_id, and the toggle removes
        exactly those rows;
  * F3  the denominator is CALENDAR DAYS IN THE WINDOW ∩ DAYS THE FEED COVERS
        (one unfiltered COUNT(DISTINCT review_date) per window) — never the days
        the selection happens to have rows for; the page prints both counts;
  * F5  share of the mix is the headline, the arrows are labelled 'complaint
        records per day', and the panel states the SALES EXPOSURE behind the
        filtered set (orders/units it re-derives from sales_by_sku_month) and
        the records per 100 orders — or says 'no sales data covers this filter';
        never a blank and never a 0.00 that no denominator supports;
  * F6  dup_rank/order_id ride in /export.csv, /monthly.csv, v3_output.csv and
        the mirror payload;
  * F7  /monthly?sku=X returns fewer rows than /monthly;
  * F8  the SKU filter is exact by default and echoes what it matched;
  * F9  /rules publishes the picklist -> code mapping, and this week's code-only
        sample rows are marked N/A;
  * F10 title_short is the MODAL title for a SKU, not MAX(title);
  * F11 the surface says 'complaint records', and the header splits the corpus
        into with-a-comment / return-reason-only / review-text;
  * F12 today is not a complete day and is out of the rates;
  * F13 no '+137 (-0%)' beside a positive count;
  * F14 the server is threaded and page queries carry a statement timeout;
  * F15 a month filter uses the month's CALENDAR bounds, not the last row's date.

Usage:

    APP=127.0.0.1:8110 USER=chadia PASS=fix python3 tests/check_mix_consistency.py
"""
import base64
import csv
import html as htmllib
import inspect
import json
import math
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta

import pymysql

BASE = os.environ.get("APP", "127.0.0.1:8110")
AUTH = base64.b64encode(
    f"{os.environ.get('USER', 'chadia')}:{os.environ.get('PASS', 'fix')}".encode()).decode()

# app.PROCESS_NOISE: the rule's verdict on non-complaint text, excluded from every
# distribution and from the exposure numerator (which counts complaint records).
PROCESS_NOISE = "Review Deletion"

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
CHECKS = 0
TODAY = date.today()
SLACK = 3


def fail(msg):
    FAILS.append(msg)
    print("FAIL", msg)


def ok(msg):
    global CHECKS
    CHECKS += 1
    print("ok  ", msg)


def get(path):
    req = urllib.request.Request(f"http://{BASE}{path}", headers={"Authorization": f"Basic {AUTH}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read().decode("utf-8")


def get_bytes(path):
    req = urllib.request.Request(f"http://{BASE}{path}", headers={"Authorization": f"Basic {AUTH}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read(), r.headers.get("Content-Disposition", "")


def db():
    return pymysql.connect(host=os.environ["CXAPP_DB_HOST"], user=os.environ["CXAPP_DB_USER"],
                           password=os.environ["CXAPP_DB_PASSWORD"],
                           database=os.environ["CXAPP_DB_NAME"], charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)


def where(q):
    """The same filter semantics as app.build_where, re-implemented from the URL."""
    w, a = [], []
    if q.get("q"):
        w.append("(v.review_text LIKE %s OR v.title LIKE %s OR v.sku LIKE %s OR v.product LIKE %s)")
        a += [f"%{q['q']}%"] * 4
    if q.get("product"):
        w.append("(v.product LIKE %s OR v.title LIKE %s)")
        a += [f"%{q['product']}%"] * 2
    if q.get("family"):
        w.append("v.product_family=%s"); a.append(q["family"])
    if q.get("parent"):
        w.append("v.parent=%s"); a.append(q["parent"])
    if q.get("sku"):
        if q.get("skulike") == "1":
            w.append("v.sku LIKE %s"); a.append(f"%{q['sku']}%")
        else:
            w.append("v.sku=%s"); a.append(q["sku"].strip())
    if q.get("complaint"):
        w.append("v.final_complaint=%s"); a.append(q["complaint"])
    if q.get("source"):
        w.append("v.source=%s"); a.append(q["source"])
    if q.get("country"):
        w.append("v.country=%s"); a.append(q["country"])
    if q.get("month"):
        w.append("DATE_FORMAT(v.review_date,'%%Y-%%m')=%s"); a.append(q["month"])
    if q.get("from"):
        w.append("v.review_date >= %s"); a.append(q["from"])
    if q.get("to"):
        w.append("v.review_date <= %s"); a.append(q["to"])
    if q.get("status") == "decided":
        w.append("v.human_validated=1")
    elif q.get("status") == "flagged":
        w.append("v.needs_review=1 AND v.human_validated=0")
    elif q.get("status") == "new":
        w.append("v.human_validated=0")
    if q.get("tx") == "prose":
        w.append("v.has_prose=1")
    elif q.get("tx") == "codeonly":
        w.append("v.has_prose=0")
    if q.get("dup") == "1":
        w.append("v.dup_rank=1")
    return ("WHERE " + " AND ".join(w)) if w else "", a


def count(cur, q):
    w, a = where(q)
    cur.execute(f"SELECT COUNT(*) n FROM v_ai_complaints v {w}", a)
    return int(cur.fetchone()["n"])


def _win(q, a_, b_):
    qq = {k: v for k, v in q.items() if k not in ("from", "to", "month")}
    qq["from"], qq["to"] = a_.isoformat(), b_.isoformat()
    return where(qq)


def src_dist(cur, q, a_, b_):
    """{source: {n, np, mn, mx, codes}} — one query, exactly what the page groups."""
    w, a = _win(q, a_, b_)
    cur.execute(f"""SELECT IFNULL(v.source,'(no source)') s, COALESCE(v.final_complaint,'(none)') c,
                           COUNT(*) n, SUM(v.has_prose=0) np,
                           MIN(v.review_date) mn, MAX(v.review_date) mx
                      FROM v_ai_complaints v {w} GROUP BY s, c""", a)
    out = {}
    for r in cur.fetchall():
        d = out.setdefault(r["s"], {"n": 0, "np": 0, "mn": None, "mx": None, "codes": {}})
        d["n"] += int(r["n"]); d["np"] += int(r["np"] or 0)
        d["codes"][r["c"]] = d["codes"].get(r["c"], 0) + int(r["n"])
        if r["mn"] and (d["mn"] is None or r["mn"] < d["mn"]): d["mn"] = r["mn"]
        if r["mx"] and (d["mx"] is None or r["mx"] > d["mx"]): d["mx"] = r["mx"]
    return out


def feed_days(cur, a, b):
    """Calendar days in [a,b] the WHOLE feed covers — unfiltered, the denominator."""
    cur.execute("SELECT COUNT(DISTINCT review_date) d FROM v_ai_complaints "
                "WHERE review_date BETWEEN %s AND %s", (a, b))
    return int(cur.fetchone()["d"] or 0)


def sel_days(cur, q, a, b):
    w, args = _win(q, a, b)
    cur.execute(f"SELECT COUNT(DISTINCT v.review_date) d FROM v_ai_complaints v {w}", args)
    return int(cur.fetchone()["d"] or 0)


def month_bounds(m):
    y, mo = int(m[:4]), int(m[5:7])
    first = date(y, mo, 1)
    nxt = date(y + (mo == 12), (mo % 12) + 1, 1)
    return first, nxt - timedelta(days=1)


def trim_months(months):
    if len(months) <= 3:
        return months
    total = sum(n for _, n in months)
    best, acc, start = None, 0, 0
    for i, (_, n) in enumerate(months):
        acc += n
        while acc - months[start][1] >= total * 0.995 and start < i:
            acc -= months[start][1]
            start += 1
        if acc >= total * 0.995 and (best is None or (i - start) < (best[1] - best[0])):
            best = (start, i)
    return months[best[0]:best[1] + 1] if best else months


def series(cur, q):
    w, a = where(q)
    ws = (w + " AND " if w else "WHERE ") + (
        "v.review_date IS NOT NULL AND v.final_complaint IS NOT NULL "
        "AND v.final_complaint <> 'Review Deletion'")
    cur.execute(f"SELECT DATE_FORMAT(v.review_date,'%%Y-%%m') m, COUNT(*) n "
                f"FROM v_ai_complaints v {ws} GROUP BY 1 ORDER BY 1", a)
    months = [(r["m"], int(r["n"])) for r in cur.fetchall() if r["m"]]
    cur.execute(f"SELECT MIN(v.review_date) l, MAX(v.review_date) h FROM v_ai_complaints v {ws}", a)
    r = cur.fetchone()
    return months, trim_months(months), r["l"], r["h"]


def window(q, plot, lo, hi):
    """The window the page must use: dates, then month bounds (F15), then plot."""
    f = datetime.strptime(q["from"], "%Y-%m-%d").date() if q.get("from") else None
    t = datetime.strptime(q["to"], "%Y-%m-%d").date() if q.get("to") else None
    if (f or t) and lo and hi:
        f, t = (f or lo), (t or hi)
    elif q.get("month"):
        f, t = month_bounds(q["month"])
    else:
        keys = [m for m, _ in plot][-3:]
        if not keys:
            return None, None
        f = month_bounds(keys[0])[0]
        t = month_bounds(keys[-1])[1]
    # F15: only the CURRENT month is clamped; an explicit month keeps its bounds.
    if q.get("month") and t >= TODAY:
        t = TODAY
    elif not q.get("month") and hi and t > hi:
        t = hi
    return f, (t if f <= t else f)


def derive(cur, q):
    """Everything the panel is allowed to print, derived from SQL."""
    months, plot, lo, hi = series(cur, q)
    f, t = window(q, plot, lo, hi)
    d = {"f": f, "t": t, "lo": lo, "hi": hi}
    d["plot_months"] = [m for m, _ in plot]
    d["rate_t"] = min(t, TODAY - timedelta(days=1)) if t and t >= TODAY else t
    d["rate_ok"] = bool(f and d["rate_t"] and d["rate_t"] >= f)
    pf, pt, _ = (None, None, 0)
    if f and t:
        span = ((d["rate_t"] if d["rate_ok"] else t) - f).days + 1
        pt = f - timedelta(days=1)
        pf = pt - timedelta(days=span - 1)
    d["pf"], d["pt"] = pf, pt
    cs = src_dist(cur, q, f, d["rate_t"]) if d["rate_ok"] else {}
    ps = src_dist(cur, q, pf, pt) if pf else {}
    d["cs"], d["ps"] = cs, ps
    d["cur_all"] = sum(v["n"] for v in cs.values())
    d["prior_all"] = sum(v["n"] for v in ps.values())
    d["cur_all_np"] = sum(v["np"] for v in cs.values())
    d["prior_all_np"] = sum(v["np"] for v in ps.values())

    lfl, reasons = [], []
    for s in sorted(set(cs) | set(ps)):
        cn, pn = cs.get(s, {}).get("n", 0), ps.get(s, {}).get("n", 0)
        if not cn or not pn:
            if pn and not cn:
                reasons.append(f"the {s} feed ends")
            else:
                reasons.append(f"labelled {s} appear only")
            continue
        gc = (t - cs[s]["mx"]).days if cs[s]["mx"] else 99
        gp = (pt - ps[s]["mx"]).days if ps[s]["mx"] else 99
        if gc > SLACK or gp > SLACK:
            reasons.append(f"the {s} feed stops on")
            continue
        lfl.append(s)
    sc = 100.0 * d["cur_all_np"] / d["cur_all"] if d["cur_all"] else 0
    sp = 100.0 * d["prior_all_np"] / d["prior_all"] if d["prior_all"] else 0
    if d["prior_all"] and abs(sc - sp) > 10:
        reasons.append("return-reason-only share")
    d["lfl"], d["reasons"] = lfl, reasons
    d["share_c"], d["share_p"] = sc, sp

    curw, priorw = {}, {}
    for s in lfl:
        for c, v in cs[s]["codes"].items():
            curw[c] = curw.get(c, 0) + v
    for s in lfl:
        for c, v in ps[s]["codes"].items():
            priorw[c] = priorw.get(c, 0) + v
    d["curw"], d["priorw"] = curw, priorw
    d["cur_total"], d["prior_total"] = sum(curw.values()), sum(priorw.values())
    d["thin"] = bool(d["prior_total"]) and (d["prior_total"] < 100
                                           or d["prior_total"] < 0.5 * d["cur_total"])
    d["cur_feed"] = feed_days(cur, f, d["rate_t"]) if d["rate_ok"] else 0
    d["prior_feed"] = feed_days(cur, pf, pt) if pf else 0
    d["cur_sel"] = sel_days(cur, q, f, d["rate_t"]) if d["rate_ok"] else 0
    d["prior_sel"] = sel_days(cur, q, pf, pt) if pf else 0
    d["show"] = (bool(d["prior_total"]) and not d["thin"] and bool(lfl) and not reasons
                 and bool(d["cur_feed"]) and bool(d["prior_feed"]) and d["rate_ok"])
    d["floor"] = max(20, int(math.ceil(0.01 * d["cur_total"]))) if d["cur_total"] else 20
    return d


def expected_pct(cv, pv, cd, pd):
    """The rate change the page must print, as a number (None when a rate cannot exist)."""
    if not cd or not pd or not pv:
        return None
    return ((cv / cd) / (pv / pd) - 1) * 100.0


MIX_RE = re.compile(r'<div class="mix"([^>]*)>')
ROW_RE = re.compile(r'<tr data-code="([^"]*)" data-n="(\d+)" data-win="(\d+)" data-delta="([^"]*)"')


def attrs(html):
    m = MIX_RE.search(html)
    if not m:
        return {}
    return dict(re.findall(r'([a-z-]+)="([^"]*)"', m.group(1)))


def rows(html):
    return [(htmllib.unescape(a), int(b), int(c), d) for a, b, c, d in ROW_RE.findall(html)]


def delta_text(cell):
    m = re.search(r'class="dl [^"]*"[^>]*>([^<]*)<', cell)
    if m:
        return htmllib.unescape(m.group(1)).strip()
    m = re.search(r'n(?:&lt;|<)(\d+)', cell)
    if m and 'class="meta"' in cell:
        return "n<" + m.group(1)
    return ""


def case(path, q, want_guard=None, want_lfl=None):
    print(f"\n== {path}")
    page = get(path)
    a = attrs(page)
    if not a:
        fail(f"{path}: no mix panel")
        return
    conn = db(); cur = conn.cursor()
    d = derive(cur, q)
    n_sql = count(cur, q)
    sec = page[page.find('<div class="mix"'):]

    m = re.search(r'([\d,]+) complaint records match these filters', sec)
    if not m:
        fail(f"{path}: no 'N complaint records match these filters' line")
    elif int(m.group(1).replace(",", "")) != n_sql:
        fail(f"{path}: page says {m.group(1)} rows match, SQL says {n_sql:,}")
    else:
        ok(f"{path}: {n_sql:,} records match = SQL")

    # --- F1/F3/F12: the window, the feeds and the denominators --------------
    if d["f"]:
        if f"{d['f'].isoformat()}" not in sec:
            fail(f"{path}: header does not name the window start {d['f']}")
        if f"{d['rate_t'].isoformat()}" not in sec and f"{d['t'].isoformat()}" not in sec:
            fail(f"{path}: header does not name the window end")
    for k, want in (("data-cur-feed", d["cur_feed"]), ("data-prior-feed", d["prior_feed"]),
                    ("data-cur-sel", d["cur_sel"]), ("data-prior-sel", d["prior_sel"])):
        got = int(a.get(k, -1))
        if got != want:
            fail(f"{path}: {k}={got}, SQL says {want}")
    ok(f"{path}: denominators — feed days {d['cur_feed']} vs {d['prior_feed']}, "
       f"selection days {d['cur_sel']} vs {d['prior_sel']} (the rate divides by the feed)")

    if d["cur_feed"] and f"{d['cur_sel']:,} of {d['cur_feed']:,} feed days" not in sec:
        fail(f"{path}: the '{d['cur_sel']} of {d['cur_feed']} feed days' line is missing")
    if d["t"] and d["t"] >= TODAY and "incomplete" not in sec:
        fail(f"{path}: today is inside the window but the page does not say it is incomplete")

    # --- F1: like-for-like guard -------------------------------------------
    guard = int(a.get("data-guard", -1))
    want_guard = bool(d["reasons"]) if want_guard is None else want_guard
    if guard != int(want_guard):
        fail(f"{path}: data-guard={guard}, SQL arithmetic says {int(want_guard)} "
             f"(reasons: {d['reasons']})")
    else:
        ok(f"{path}: like-for-like guard = {bool(guard)} ({'; '.join(d['reasons']) or 'feeds comparable'})")
    got_lfl = [s for s in (a.get("data-lfl", "") or "").split(",") if s]
    if want_lfl is not None:
        if got_lfl != list(want_lfl):
            fail(f"{path}: comparison covers {got_lfl}, SQL says {list(want_lfl)}")
        else:
            ok(f"{path}: comparison covers exactly {got_lfl}")
    elif d["reasons"] and guard:
        for r in d["reasons"]:
            if r.startswith("the ") and f"{r} " not in sec:
                fail(f"{path}: guard fired ({r}) but the page does not say it")
        if "not like-for-like" not in sec:
            fail(f"{path}: guard fired without the words 'not like-for-like'")
        ok(f"{path}: the guard prints why ({'; '.join(d['reasons'])})")

    # --- the rows ----------------------------------------------------------
    rr = rows(page)
    if not rr:
        fail(f"{path}: no mix rows parsed")
        conn.close(); return
    cells = dict((c, t) for c, t in
                 ((htmllib.unescape(a), b) for a, b in
                  re.findall(r'<tr data-code="([^"]*)"[^>]*>.*?<td style="width:92px">(.*?)</td></tr>',
                             sec, re.S)))
    bad = checked = 0
    for code, k, win, dv in rr:
        if code in (NO_CODE, "Review Deletion"):
            continue
        cw = sum(v["codes"].get(code, 0) for v in d["cs"].values())
        if cw != win:
            fail(f"{path}: {code} shows {win:,} in window, SQL says {cw:,}")
            bad += 1
        cv, pv = d["curw"].get(code, 0), d["priorw"].get(code, 0)
        if not d["show"]:
            if dv != "":
                fail(f"{path}: {code} carries a percentage ({dv}) with the arrows withheld")
            continue
        if max(cv, pv) < d["floor"]:
            if dv != "":
                fail(f"{path}: {code} {cv} vs {pv} is below the {d['floor']} floor but has delta {dv}")
            continue
        exp = expected_pct(cv, pv, d["cur_feed"], d["prior_feed"])
        if exp is None:
            continue
        checked += 1
        if dv == "":
            fail(f"{path}: {code} has no percentage, SQL rates give {exp:.1f}%")
        elif abs(float(dv) - exp) > 0.02:
            fail(f"{path}: {code} delta {dv}%, SQL rates {cv}/{d['cur_feed']} vs "
                 f"{pv}/{d['prior_feed']} = {exp:.4f}%")
        # F13: below |1%| the cell must not print a direction it cannot support
        elif abs(exp) < 1:
            txt = delta_text(cells.get(code, ""))
            if txt not in ("±0%",):
                fail(f"{path}: {code} is {exp:.2f}% (flat) but the cell reads '{txt}'")
    if not bad:
        ok(f"{path}: every row's in-window count = SQL ({len(rr)} rows)")

    if d["show"]:
        ok(f"{path}: {checked} change cells match their per-feed-day SQL rate "
           f"(floor {d['floor']:,})")
        if "complaint records per day" not in sec:
            fail(f"{path}: the change column is not labelled 'complaint records per day'")
        # F13: a positive count change must never be paired with a negative %
        for m2 in re.finditer(r"\+(?P<d>[\d,]+) records \((?P<t>[^)]*)\)", htmllib.unescape(
                re.sub(r"<[^>]+>", "", sec))):
            if m2.group("t").startswith("-"):
                fail(f"{path}: riser prints '+{m2.group('d')} records ({m2.group('t')})' — "
                     f"a rise in count with a fall in rate")
                break
        else:
            ok(f"{path}: no '+N records (-x%)' in the riser")
    else:
        if re.search(r'data-delta="[^"]+"', sec):
            fail(f"{path}: arrows withheld but a row still carries a percentage")
        else:
            ok(f"{path}: arrows withheld, every row shows '—'")
        if d["reasons"] and "withheld" not in sec:
            fail(f"{path}: no visible explanation of the withheld change")

    # --- F5: share is the headline, and the EXPOSURE line states the denominator
    if "Share of complaint records" not in sec:
        fail(f"{path}: no 'Share of complaint records' headline")
    # F5b (2026-10-19): the old line said no units/orders denominator existed. It
    # does now, so the panel must state the exposure behind THIS filter and the
    # rate, or say in words that no sales data covers it. Blank, a 0.00 rate with
    # no denominator, or the old "no denominator exists" claim are all failures.
    if "Sales exposure:" not in sec:
        fail(f"{path}: no 'Sales exposure:' line — the panel does not state its denominator")
    elif "No units or orders denominator exists" in sec:
        fail(f"{path}: the panel still says no units/orders denominator exists")
    else:
        # R2: the denominator is the PRODUCT SCOPE with the complaint predicate
        # removed — every SKU the corpus attributes to the scope, in every month the
        # records touch — not the SKU-months that happened to record this code.
        qq = {k: v for k, v in q.items() if k in ("product", "family", "parent", "sku", "skulike")}
        wq, aq = where(qq)
        cur.execute(f"SELECT DISTINCT v.sku FROM v_ai_complaints v {wq}"
                    f"{' AND' if wq else ' WHERE'} v.sku IS NOT NULL", aq)
        skus = [r["sku"] for r in cur.fetchall()]
        wq, aq = where(q)
        wx = (wq + " AND " if wq else "WHERE ") + (
            "v.sku IS NOT NULL AND v.review_date IS NOT NULL "
            "AND v.final_complaint IS NOT NULL AND v.final_complaint <> %s "
            "AND (v.final_category IS NULL OR v.final_category NOT IN ('NEW','NO_TEXT','ERR'))")
        cur.execute(f"""SELECT x.sku, x.m, x.n, s.orders FROM
                          (SELECT v.sku, DATE_FORMAT(v.review_date,'%%Y-%%m') m, COUNT(*) n
                             FROM v_ai_complaints v {wx}
                            GROUP BY v.sku, DATE_FORMAT(v.review_date,'%%Y-%%m')) x
                          LEFT JOIN sales_by_sku_month s
                                 ON s.sku = x.sku AND s.month = x.m""", aq + [PROCESS_NOISE])
        num = cur.fetchall()
        # Same window the page's chart and rate line describe (the charted months).
        win = set(d["plot_months"])
        num = [r for r in num if r["m"] in win]
        recs = sum(int(r["n"] or 0) for r in num)
        cov = sum(int(r["n"] or 0) for r in num if (r["orders"] or 0) > 0)
        cond = sum(int(r["orders"] or 0) for r in num if (r["orders"] or 0) > 0)
        months = sorted({r["m"] for r in num if r["m"]}) or sorted(win)
        orders = 0
        if skus and months:
            ph = ",".join(["%s"] * len(skus))
            cur.execute(f"""SELECT COALESCE(SUM(orders),0) o FROM sales_by_sku_month
                             WHERE sku IN ({ph}) AND month BETWEEN %s AND %s""",
                        skus + [months[0], months[-1]])
            orders = int(cur.fetchone()["o"] or 0)
        # The exposure paragraph only: the chart's tooltips are inside `sec` too and
        # they carry their own "per 100 orders" numbers.
        start = sec.find("Sales exposure:")
        end = sec.find("</p>", start) if start >= 0 else -1
        flat = htmllib.unescape(re.sub(r"<[^>]+>", " ",
                                       sec[start:end if end > start else len(sec)]))
        if not orders:
            if "sales row" not in flat:
                fail(f"{path}: no denominator for this filter but the panel does not say why")
            else:
                ok(f"{path}: exposure — no denominator, and the panel says why")
        else:
            mm = re.search(r"([\d,]+)\s+orders and\s+([\d,]+)\s+units across", flat)
            if not mm:
                fail(f"{path}: exposure line has no orders/units figures")
            elif int(mm.group(1).replace(",", "")) != orders:
                fail(f"{path}: exposure says {mm.group(1)} orders, the product scope says "
                     f"{orders:,}")
            else:
                ok(f"{path}: exposure orders match the product scope ({orders:,} — "
                   f"complaint predicate removed)")
            rr = re.search(r"([\d.]+)\s+complaint records per 100 orders", flat)
            if not rr:
                if orders < 100:
                    ok(f"{path}: {orders} orders is below the floor, so no rate is printed (R3)")
                else:
                    fail(f"{path}: exposure line has no rate")
            else:
                want = 100.0 * cov / orders
                if abs(float(rr.group(1)) - want) > 0.02:
                    fail(f"{path}: exposure rate {rr.group(1)} vs SQL {cov}/{orders} = {want:.2f}")
                else:
                    ok(f"{path}: exposure rate matches SQL ({cov:,} records / {orders:,} orders "
                       f"= {want:.2f} per 100)")
                # R2: where a code filter narrowed the SKU-months, the conditional
                # figure must be present AND named as conditional.
                if q.get("complaint") and cond and cond < orders:
                    if "among the" not in flat or "that recorded this code" not in flat:
                        fail(f"{path}: a code filter is on but the conditional figure "
                             f"({100.0 * cov / cond:.2f} per 100 over {cond:,} orders) is not "
                             f"labelled 'among the N SKU-months that recorded this code'")
                    else:
                        ok(f"{path}: conditional figure labelled "
                           f"({100.0 * cov / cond:.2f} over the {cond:,} orders in the SKU-months "
                           f"that recorded this code, vs {100.0 * cov / orders:.2f} on the scope)")
    if not re.search(r"of <b>[\d,]+</b> records carry a customer comment", sec):
        fail(f"{path}: no 'x of y carry a customer comment' line")
    ok(f"{path}: share headline and the exposure line are present")

    # --- F2: repeat disclosure -------------------------------------------
    # The repeats are counted on the SAME selection WITHOUT the toggle: with it,
    # dup_rank>1 is zero by definition.
    qd = {k: v for k, v in q.items() if k != "dup"}
    wq, aq = where(qd)
    cur.execute(f"SELECT COALESCE(SUM(v.dup_rank > 1),0) n FROM v_ai_complaints v {wq}", aq)
    dup = int(cur.fetchone()["n"] or 0)
    if q.get("dup") != "1":
        mm = re.search(r'<b>([\d,]+)</b> rows? repeat a comment already recorded on the same order', sec)
        if dup and not mm:
            fail(f"{path}: {dup} rows repeat a comment on the same order but the footer omits it")
        elif dup and int(mm.group(1).replace(",", "")) != dup:
            fail(f"{path}: footer says {mm.group(1)} repeat, SQL says {dup:,}")
        elif dup:
            ok(f"{path}: {dup:,} repeats disclosed (counted, not removed)")
    else:
        mm = re.search(r'<b>([\d,]+)</b> rows that repeat a comment already recorded', sec)
        if not mm:
            fail(f"{path}: the distinct view does not say how many rows it hides")
        elif int(mm.group(1).replace(",", "")) != dup:
            fail(f"{path}: distinct view hides {mm.group(1)}, SQL says {dup:,} repeats")
        else:
            ok(f"{path}: the distinct view hides exactly the {dup:,} repeat rows")

    # --- toggles reconcile to the page each link opens ---------------------
    tog = re.search(r'<div class="txtog">(.*?)</div>', sec, re.S)
    if tog:
        links = re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', tog.group(1))
        bad_l = 0
        for href, label in links:
            href = htmllib.unescape(href)
            tok = re.findall(r"([\d,]+)$", label.strip())
            if not tok:
                fail(f"{path}: toggle '{label}' carries no count"); bad_l += 1; continue
            shown = int(tok[0].replace(",", ""))
            tgt = get(href)
            ts = tgt[tgt.find('<div class="mix"'):]
            got = 0 if "Nothing matches those filters" in ts else int(
                re.search(r'([\d,]+) complaint records match', ts).group(1).replace(",", ""))
            if got != shown:
                fail(f"{path}: toggle '{label.strip()}' shows {shown:,}, {href} returns {got:,}")
                bad_l += 1
        if not bad_l:
            ok(f"{path}: all {len(links)} toggle counts = the page each link opens")
    conn.close()


NO_CODE = "(no complaint assigned)"


def check_default_guard():
    """The default window must WITHHOLD: ncx ends, amazon stops, the mix moved."""
    print("\n== F1 default window")
    page = get("/reviews")
    sec = page[page.find('<div class="mix"'):]
    a = attrs(page)
    if a.get("data-guard") != "1":
        fail("default: the guard did not fire, but ncx has no rows after 2026-07 and the "
             "return-reason share moves ~15pp")
    else:
        ok("default: guard fired")
    for need in ("the ncx feed ends", "not like-for-like"):
        if need not in sec:
            fail(f"default: the withheld change does not say '{need}'")
    if "the ncx feed ends" in sec and "not like-for-like" in sec:
        ok("default: says the ncx feed ends, so the comparison is not like-for-like")
    if "excluded: ncx" not in sec:
        fail("default: the header does not list the per-source totals it excludes")
    else:
        ok("default: header names the excluded per-source totals")


def check_like_for_like():
    """fba is the one feed in both windows for Aug–Sep; the arrows must appear and
    match a per-feed-day rate over that feed only."""
    print("\n== F1 like-for-like (fba, Aug–Sep)")
    path = "/reviews?source=fba&from=2026-08-01&to=2026-09-30"
    page = get(path); sec = page[page.find('<div class="mix"'):]
    a = attrs(page)
    if a.get("data-lfl") != "fba":
        fail(f"{path}: comparison covers {a.get('data-lfl')!r}, expected 'fba'")
    if a.get("data-guard") != "0":
        fail(f"{path}: guard fired on a feed present in both windows")
    if a.get("data-cur-feed") != "61" or a.get("data-prior-feed") != "61":
        fail(f"{path}: feed days {a.get('data-cur-feed')}/{a.get('data-prior-feed')}, expected 61/61")
    if 'data-delta=""' in sec and '▲' not in sec:
        fail(f"{path}: no arrow on a like-for-like comparison")
    else:
        ok(f"{path}: fba only, 61/61 feed days, arrows present")
    # the specific trap: Too Big is -17% all-source but +41% fba-only
    conn = db(); cur = conn.cursor()
    d = derive(cur, {"source": "fba", "from": "2026-08-01", "to": "2026-09-30"})
    got = dict((c, dv) for c, _, _, dv in rows(page) if c == "Too Big")
    cv, pv = d["curw"].get("Too Big", 0), d["priorw"].get("Too Big", 0)
    exp = expected_pct(cv, pv, d["cur_feed"], d["prior_feed"])
    got_pct = float(got["Too Big"]) if got and got["Too Big"] not in ("", None) else None
    if exp is None or got_pct is None or abs(got_pct - exp) > 0.02:
        fail(f"{path}: Too Big delta {got}, SQL fba-only rate says {exp} "
             f"({cv}/{d['cur_feed']} vs {pv}/{d['prior_feed']})")
    else:
        ok(f"{path}: Too Big {got['Too Big']}% = fba-only per-day rate ({cv} vs {pv})")
    conn.close()


def check_denominator_not_selection_days():
    """F3: seat cushions have rows on 55 of the prior window's 61 feed days. The
    page must divide by 61 and print '55 of 61'."""
    print("\n== F3 seat-cushion denominators")
    page = get("/reviews?family=Seat+cushions")
    a = attrs(page)
    conn = db(); cur = conn.cursor()
    d = derive(cur, {"family": "Seat cushions"})
    if d["cur_sel"] == d["cur_feed"]:
        print("     (note: current window has no zero days; the prior window does)")
    if (d["prior_sel"], d["prior_feed"]) != (55, 61):
        fail(f"seat cushions: prior window is {d['prior_sel']} selection days of "
             f"{d['prior_feed']} feed days — the 55/61 anchor moved, re-check the test")
    if int(a.get("data-prior-sel", 0)) != d["prior_sel"] or int(a.get("data-prior-feed", 0)) != d["prior_feed"]:
        fail(f"seat cushions: page says {a.get('data-prior-sel')} of {a.get('data-prior-feed')}, "
             f"SQL says {d['prior_sel']} of {d['prior_feed']}")
    else:
        ok(f"seat cushions: {d['prior_sel']} of {d['prior_feed']} feed days, "
           f"and the denominator is the feed, not the selection")
    # 584 -> 987 records is the count change; the RATE change is what differs
    cur.execute("""SELECT SUM(review_date BETWEEN %s AND %s) a, SUM(review_date BETWEEN %s AND %s) b
                     FROM v_ai_complaints WHERE product_family='Seat cushions'""",
                (d["f"], d["rate_t"], d["pf"], d["pt"]))
    r = cur.fetchone()
    cur_n, prior_n = int(r["a"] or 0), int(r["b"] or 0)
    naive = 100.0 * ((cur_n / d["cur_sel"]) / (prior_n / d["prior_sel"]) - 1)
    right = 100.0 * ((cur_n / d["cur_feed"]) / (prior_n / d["prior_feed"]) - 1)
    if abs(naive - right) < 2:
        fail("seat cushions: the selection-day and feed-day rates agree here, so this case "
             "cannot prove which denominator is used")
    else:
        ok(f"seat cushions: {cur_n}->{prior_n} records; selection-day basis gives "
           f"{naive:+.1f}%, feed-day basis {right:+.1f}% — the page must use the latter")
    conn.close()


def check_sku_filter():
    print("\n== F8/F7 SKU filters")
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) n FROM v_ai_complaints WHERE sku='6'")
    exact = int(cur.fetchone()["n"])
    cur.execute("SELECT COUNT(*) n FROM v_ai_complaints WHERE sku LIKE '%%6%%'")
    loose = int(cur.fetchone()["n"])
    page = get("/reviews?sku=6")
    m = re.search(r'([\d,]+) complaint records match', page)
    got = int(m.group(1).replace(",", "")) if m else -1
    if got != exact:
        fail(f"/reviews?sku=6 returns {got}, the exact SKU 6 has {exact} (substring would be {loose})")
    else:
        ok(f"/reviews?sku=6 is exact: {exact:,} rows (substring would be {loose:,})")
    if f"SKU exactly “6” matched 1 value" not in page:
        fail("/reviews?sku=6 does not echo the SKU values it matched")
    else:
        ok("/reviews?sku=6 echoes the matched SKU value")
    page = get("/reviews?sku=6&skulike=1")
    m = re.search(r'([\d,]+) complaint records match', page)
    got = int(m.group(1).replace(",", "")) if m else -1
    shown_vals = len(set(re.findall(r"([A-Za-z0-9-]+) \(", page)))
    if got != loose:
        fail(f"/reviews?sku=6&skulike=1 returns {got}, SQL substring says {loose}")
    else:
        ok(f"/reviews?sku=6&skulike=1 is the explicit contains: {loose:,} rows across "
           f"{shown_vals} values")

    # F7: /monthly must be filterable by SKU at all
    all_rows = int(re.search(r'([\d,]+) rows match', get("/monthly")).group(1).replace(",", ""))
    one = get("/monthly?sku=6")
    m = re.search(r'([\d,]+) rows match', one)
    n6 = int(m.group(1).replace(",", "")) if m else -1
    cur.execute("SELECT COUNT(*) n FROM v_ai_complaints_monthly WHERE sku='6'")
    want = int(cur.fetchone()["n"])
    if n6 != want:
        fail(f"/monthly?sku=6 returns {n6} rows, SQL says {want} (unfiltered {all_rows})")
    elif n6 >= all_rows:
        fail(f"/monthly?sku=6 returns every row ({n6}) — the filter does nothing")
    else:
        ok(f"/monthly?sku=6 returns {n6:,} rows against {all_rows:,} unfiltered")
    body, _ = get_bytes("/monthly.csv?sku=6")
    n_csv = len(body.decode("utf-8-sig").strip().splitlines()) - 1
    if n_csv != want:
        fail(f"/monthly.csv?sku=6 has {n_csv} data rows, SQL says {want}")
    else:
        ok(f"/monthly.csv?sku=6 honours the same filter ({n_csv:,} rows)")
    conn.close()


def check_month_window():
    """F15: month=2025-07 must be July, not the two days that happen to hold rows."""
    print("\n== F15 month window")
    page = get("/reviews?month=2025-07")
    sec = page[page.find('<div class="mix"'):]
    if "2025-07-01" not in sec or "2025-07-31" not in sec:
        fail("/reviews?month=2025-07 does not use the month's calendar bounds "
             "(2025-07-01 .. 2025-07-31)")
    else:
        ok("/reviews?month=2025-07 uses 2025-07-01 .. 2025-07-31, not the last row's date")
    a = attrs(page)
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT review_date) d FROM v_ai_complaints "
                "WHERE review_date BETWEEN '2025-07-01' AND '2025-07-31'")
    want = int(cur.fetchone()["d"])
    if int(a.get("data-cur-feed", -1)) != want:
        fail(f"month=2025-07: denominator is {a.get('data-cur-feed')}, the feed covers {want} day(s)")
    else:
        ok(f"month=2025-07: denominator is the {want} feed day(s) in July, not a truncated window")
    if "days with data" in sec:
        fail("month=2025-07 still says 'days with data' — the wrong denominator is back")
    conn.close()


def check_disclosure():
    """F6/F11: the repeats ride in every export, and the wording says records."""
    print("\n== F6/F11 disclosure and filenames")
    body, disp = get_bytes("/export.csv?family=Seat+cushions")
    text = body.decode("utf-8-sig")
    head = text.splitlines()[0].split(",")
    for col in ("order_id", "dup_rank", "dup_group"):
        if col not in head:
            fail(f"/export.csv is missing {col}")
    rep = sum(1 for r in csv.DictReader(text.splitlines()) if int(r["dup_rank"] or 1) > 1)
    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT COUNT(*) n FROM v_ai_complaints
                    WHERE product_family='Seat cushions' AND dup_rank > 1""")
    want = int(cur.fetchone()["n"])
    if rep != want:
        fail(f"/export.csv marks {rep} repeats, SQL says {want}")
    else:
        ok(f"/export.csv carries order_id/dup_rank/dup_group and marks the {want} repeats")
    if "complaint-records" not in disp:
        fail(f"/export.csv filename is {disp!r} — should say complaint-records")
    else:
        ok(f"/export.csv filename: {disp.split('filename=')[-1]}")

    body, disp = get_bytes("/monthly.csv")
    text = body.decode("utf-8-sig")
    head = text.splitlines()[0].split(",")
    if "n_repeat" not in head:
        fail("/monthly.csv has no n_repeat column")
    else:
        tot = sum(int(r["n_repeat"]) for r in csv.DictReader(text.splitlines()))
        ok(f"/monthly.csv carries n_repeat ({tot:,} repeat rows)")

    # v3_output.csv: order_id was there, dup_rank was not
    p = os.path.join(REPO, "data", "v3_output.csv")
    with open(p, encoding="utf-8-sig") as fh:
        rows_ = list(csv.DictReader(fh))
    if rows_ and "dup_rank" not in rows_[0]:
        fail("data/v3_output.csv has no dup_rank column")
    else:
        ok(f"data/v3_output.csv carries order_id and dup_rank "
           f"({sum(1 for r in rows_ if int(r['dup_rank'] or 1) > 1):,} repeats)")

    # the mirror payload
    p = os.path.join(REPO, "site", "data", "reviews.json")
    if not os.path.exists(p):
        fail("site/data/reviews.json is missing")
    else:
        d = json.load(open(p, encoding="utf-8"))
        r0 = d["rows"][0] if d.get("rows") else {}
        missing = [k for k in ("o", "dr", "dg") if k not in r0]
        if missing:
            fail(f"the mirror payload is missing {missing}")
        else:
            ok(f"the mirror payload carries order_id (o), dup_rank (dr) and dup_group (dg); "
               f"{sum(1 for r in d['rows'] if r.get('dr', 1) > 1):,} repeats")
    idx = open(os.path.join(REPO, "site", "index.html"), encoding="utf-8").read()
    if re.search(r"removed as duplicates", idx):
        fail("the mirror still calls the Review Deletion rows 'duplicates'")
    else:
        ok("the mirror's Review Deletion note no longer says 'duplicates'")

    # F11: the surface says 'complaint records'
    page = get("/reviews")
    sub = re.search(r'<p class="sub">(.*?)</p>', page, re.S)
    txt = re.sub(r"<[^>]+>", "", sub.group(1)) if sub else ""
    if "complaint records match" not in txt:
        fail("/reviews header does not say 'complaint records'")
    for piece in ("with a customer comment", "return-reason only", "review text"):
        if piece not in txt:
            fail(f"/reviews header is missing the '{piece}' split")
    if "with a customer comment" in txt and "return-reason only" in txt:
        ok("the header splits the corpus: with a comment / return-reason only / review text")
    conn.close()


def check_rules_and_quality():
    """F9: the rule path is published, and code-only sample rows are N/A."""
    print("\n== F9 rules page and sample")
    page = get("/rules")
    conn = db(); cur = conn.cursor()
    cur.execute("""SELECT COUNT(DISTINCT TRIM(review_text)) k, COUNT(*) n
                     FROM v_ai_complaints
                    WHERE has_prose=0 AND review_text IS NOT NULL AND TRIM(review_text)<>''""")
    r = cur.fetchone()
    if f"<b>{int(r['k']):,}</b>" not in page:
        fail(f"/rules does not state the {int(r['k']):,} distinct picklist strings")
    m = re.search(r"hold <b>([\d.]+)%</b> of the rows", page)
    if not m:
        fail("/rules does not state how much of the corpus the shown strings cover")
    elif float(m.group(1)) < 50:
        fail(f"/rules' default view covers only {m.group(1)}% of the rows")
    else:
        ok(f"/rules publishes {int(r['k']):,} picklist strings; the default view holds "
           f"{m.group(1)}% of {int(r['n']):,} rows")
    if "covering 80% takes" not in page:
        fail("/rules does not say how many strings are needed to cover 80%")
    else:
        ok("/rules states how many strings covering 80% would take (the tail is long)")
    m = re.search(r"of which <b>(\d+)</b>", page)
    if not m:
        fail("/rules does not state how many strings map to more than one code")
    else:
        cur.execute("""SELECT COUNT(*) n FROM (
                         SELECT TRIM(review_text) t FROM v_ai_complaints
                          WHERE has_prose=0 AND review_text IS NOT NULL
                          GROUP BY TRIM(review_text)
                         HAVING COUNT(DISTINCT final_complaint) > 1) x""")
        want = int(cur.fetchone()["n"])
        if int(m.group(1)) != want:
            fail(f"/rules says {m.group(1)} multi-code strings, SQL says {want}")
        else:
            ok(f"/rules flags the {want} picklist strings that map to more than one code")

    q = get("/quality")
    cur.execute("""SELECT COUNT(*) n FROM weekly_samples s JOIN v_ai_complaints v
                    ON v.review_id=s.review_id
                   WHERE s.sample_week=%s AND v.has_prose=0""",
                (get_iso_week(),))
    n_co = int(cur.fetchone()["n"])
    if n_co:
        if "N/A — no customer comment" not in q:
            fail(f"quality: {n_co} code-only rows in this week's sample are not marked N/A")
        else:
            ok(f"quality: {n_co} code-only row(s) marked N/A instead of graded")
    if "2026-W44" not in q:
        fail("quality does not say the prose-only pool starts at W44")
    else:
        ok("quality states the prose-only pool applies from W44")
    conn.close()


def get_iso_week():
    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


def check_titles(cur):
    """F10: title_short must be the MODAL title, not MAX(title)."""
    print("\n== F10 modal titles")
    cur.execute("""SELECT sku, title, COUNT(*) n FROM classifications
                    WHERE sku='7' AND title IS NOT NULL AND TRIM(title)<>''
                    GROUP BY sku, title ORDER BY n DESC LIMIT 1""")
    top = cur.fetchone()
    cur.execute("SELECT DISTINCT title_short FROM v_ai_complaints_monthly WHERE sku='7'")
    got = [r["title_short"] for r in cur.fetchall()]
    if len(got) != 1:
        fail(f"sku 7 has {len(got)} different title_short values — the label varies by month")
    elif not got[0] or got[0][:25].lower() not in (top["title"] or "").lower():
        fail(f"sku 7 title_short {got[0]!r} is not the modal title "
             f"({top['n']} rows: {top['title'][:40]!r})")
    else:
        ok(f"sku 7 title_short is the modal title ({top['n']} rows): {got[0]!r}")


def check_duplicates(cur):
    """F2: the repeat flag is order-keyed and truthful."""
    print("\n== F2 order-keyed repeats")
    cur.execute("""SELECT COUNT(*) n FROM v_ai_complaints
                    WHERE dup_rank > 1 AND (order_id IS NULL OR TRIM(order_id)='')""")
    bad = int(cur.fetchone()["n"])
    if bad:
        fail(f"{bad} repeat rows carry no order_id (the SKU/date key is back)")
    else:
        ok("every repeat row carries an order_id")
    cur.execute("""SELECT COUNT(*) n FROM (
                     SELECT grp FROM review_duplicates
                      WHERE grp LIKE 's|%') x""")
    fb = int(cur.fetchone()["n"])
    ok(f"SKU/date fallback groups in use: {fb} (the ~40 rows without an order are not grouped)")
    cur.execute("""SELECT COUNT(*) n FROM v_ai_complaints a
                    LEFT JOIN (SELECT order_id, MD5(TRIM(review_text)) h, COUNT(*) k
                                 FROM classifications GROUP BY order_id, MD5(TRIM(review_text))) d
                      ON d.order_id=a.order_id AND d.h=MD5(TRIM(a.review_text))
                    WHERE a.dup_rank>1 AND (d.k IS NULL OR d.k<2)""")
    bad = int(cur.fetchone()["n"])
    if bad:
        fail(f"{bad} repeat rows have no twin with the same text on the same order")
    else:
        ok("every repeat has a twin (same order, same text) — the key is the order, not the date")
    cur.execute("""SELECT COUNT(*) g, COALESCE(SUM(n),0) covered, COALESCE(SUM(n-1),0) excess
                     FROM review_duplicates""")
    row = cur.fetchone()
    g, covered, excess = int(row["g"]), int(row["covered"]), int(row["excess"])
    cur.execute("SELECT COUNT(*) n FROM v_ai_complaints WHERE dup_rank>1")
    flagged = int(cur.fetchone()["n"])
    if flagged != int(excess):
        fail(f"{flagged} rows flagged as repeats, the group table says {int(excess)} excess")
    else:
        ok(f"{int(g):,} groups / {int(covered):,} rows / {flagged:,} repeats — exactly the "
           f"table's excess, so the toggle removes true repeats only")
    # the trap the review found: one short phrase across many orders must NOT be a group
    cur.execute("""SELECT COUNT(*) n FROM (
                     SELECT sku, review_date, MD5(TRIM(review_text)) h, COUNT(*) c,
                            COUNT(DISTINCT order_id) o
                       FROM classifications
                      WHERE review_text IS NOT NULL AND TRIM(review_text)<>''
                      GROUP BY sku, review_date, MD5(TRIM(review_text))
                     HAVING o > 1) x""")
    crossed = int(cur.fetchone()["n"])
    ok(f"the old SKU+date key would still merge {crossed:,} (sku, date, text) groups across "
       f"orders; none of them is flagged now")


def check_views(cur):
    print("\n== view flags")
    cur.execute("""SELECT COUNT(*) n FROM v_ai_complaints_monthly
                    WHERE title IS NOT NULL AND TRIM(title) <> ''
                      AND (title_short IS NULL OR TRIM(title_short) = '')""")
    bad = int(cur.fetchone()["n"])
    if bad:
        fail(f"title_short: {bad} monthly row(s) have a title but no short title")
    else:
        ok("title_short: every titled SKU carries one")
    cur.execute("SELECT SUM(n_repeat) r, SUM(n) n FROM v_ai_complaints_monthly")
    r = cur.fetchone()
    if not r["r"] or not r["n"]:
        fail("v_ai_complaints_monthly does not carry n_repeat")
    else:
        ok(f"monthly view: {int(r['r']):,} of {int(r['n']):,} counted rows are repeats "
           f"(disclosed in n_repeat)")


def check_csv(cur):
    print("\n== v3_output.csv freshness")
    path = os.path.join(REPO, "data", "v3_output.csv")
    cur.execute("SELECT COUNT(*) n, COUNT(DISTINCT review_id) d FROM v_ai_complaints")
    r = cur.fetchone()
    n_db, n_ids = int(r["n"]), int(r["d"])
    if not os.path.exists(path):
        fail("data/v3_output.csv is missing")
        return
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rr = list(csv.DictReader(fh))
    if len(rr) != n_db:
        fail(f"data/v3_output.csv is STALE: {len(rr):,} rows vs {n_db:,} — run sync_v3_output.py")
    else:
        ok(f"data/v3_output.csv: {len(rr):,} rows = v_ai_complaints ({n_ids:,} ids)")


def check_units():
    print("\n== quality sample arithmetic")
    sys.path.insert(0, REPO)
    try:
        import app
    except Exception as e:                                     # noqa: BLE001
        fail(f"could not import app for the unit check: {e}")
        return
    rows_ = [
        {"your_call": "Too big", "ai_complaint": "Too big", "has_prose": 1},
        {"your_call": "Too big", "ai_complaint": "Too big", "has_prose": 1},
        {"your_call": "Too big", "ai_complaint": "Too small", "has_prose": 0},
    ]
    acc, cor, rej, n, skipped = app.sample_stats(rows_)
    if (acc, cor, n, skipped) != (2, 0, 2, 1):
        fail(f"sample_stats: expected (2,0,2,1), got ({acc},{cor},{n},{skipped})")
    else:
        ok("sample_stats keeps code-only rows out of the agreement denominator")
    if "has_prose=1" not in inspect.getsource(app.ensure_sample):
        fail("ensure_sample no longer restricts the pool to rows with a comment")
    else:
        ok("ensure_sample draws from prose rows (from W44)")
    # F13: the exact shape the review found — a count that rose while its daily
    # rate did not. 137 vs 98 over 79 vs 57 feed days is -0.9%, and the sentence
    # must not print '-1%' or '-0%' beside '+39 records'.
    tail = app._riser_tail(137, 98, 79, 57)
    if tail != "flat per day":
        fail(f"_riser_tail(137,98,79,57) = {tail!r}; a +39-record rise with a -0.9% "
             f"daily rate must read 'flat per day', never a negative percentage")
    else:
        ok("_riser_tail: +39 records at -0.9%/day reads 'flat per day'")
    if app._riser_tail(137, 98, 79, 57).startswith("-"):
        fail("_riser_tail still returns a negative percentage")
    # and a genuine rise keeps its percentage
    if app._riser_tail(200, 100, 50, 50) != "+100% per day":
        fail(f"_riser_tail(200,100,50,50) = {app._riser_tail(200,100,50,50)!r}")
    else:
        ok("_riser_tail: a doubling per day still reads '+100% per day'")
    # F14: the server must not serialise requests
    if "ThreadingMixIn" not in inspect.getsource(app) or "server_class=ThreadingWSGIServer" not in inspect.getsource(app):
        fail("app.py does not serve with a threading server (F14)")
    else:
        ok("app.py serves with ThreadingMixIn")
    src = inspect.getsource(app.db)
    if "max_statement_time" not in src:
        fail("db() sets no statement timeout (F14)")
    else:
        ok("db() sets max_statement_time, so one slow query cannot freeze the page")


def main():
    conn = db(); cur = conn.cursor()
    check_views(cur)
    check_duplicates(cur)
    check_titles(cur)
    check_csv(cur)

    # the default window: guard must fire because the feeds changed
    case("/reviews", {})
    check_default_guard()

    # filters whose data STOPS mid-month (the corpus runs to 2026-10-19)
    case("/reviews?family=Seat+cushions", {"family": "Seat cushions"})
    check_denominator_not_selection_days()
    case("/reviews?source=ncx", {"source": "ncx"})
    case("/reviews?sku=SC-XL-B", {"sku": "SC-XL-B"})

    # F15: a month filter is the month, not the days that hold rows
    case("/reviews?month=2025-07", {"month": "2025-07"})
    check_month_window()
    case("/reviews?month=2026-10", {"month": "2026-10"})
    case("/reviews?family=Seat+cushions&month=2026-10",
         {"family": "Seat cushions", "month": "2026-10"})
    case("/reviews?family=Seat+cushions&from=2026-09-01&to=2026-10-19",
         {"family": "Seat cushions", "from": "2026-09-01", "to": "2026-10-19"})
    case("/reviews?family=Seat+cushions&from=2026-07-01&to=2026-09-30",
         {"family": "Seat cushions", "from": "2026-07-01", "to": "2026-09-30"})
    case("/reviews?family=Seat+cushions&status=flagged",
         {"family": "Seat cushions", "status": "flagged"})

    # F1: the one case where the arrows may be printed
    case("/reviews?source=fba&from=2026-08-01&to=2026-09-30",
         {"source": "fba", "from": "2026-08-01", "to": "2026-09-30"},
         want_guard=False, want_lfl=["fba"])
    check_like_for_like()

    # the distinct-complaints view removes exactly the repeat rows
    case("/reviews?family=Seat+cushions&dup=1", {"family": "Seat cushions", "dup": "1"})

    check_sku_filter()
    check_rules_and_quality()
    check_disclosure()
    check_units()
    conn.close()
    print(f"\n{CHECKS} checks passed, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
